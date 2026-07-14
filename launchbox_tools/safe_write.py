from __future__ import annotations

import hashlib
import io
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .models import MutationFileResult, MutationOutcome, MutationState, PlatformInfo
from .operation_lifecycle import OperationCancelled, OperationControl, OperationPhase
from .paths import (
    UnsafeDatabasePathError,
    ensure_trusted_direct_child,
    normalize_trust_anchor,
)
from .runtime_checks import MutationBlockedError, ensure_safe_to_mutate
from .xml_checkpoint_io import (
    IO_CHUNK_SIZE,
    XML_CHECKPOINT_INTERVAL,
    parse_xml_tree_with_checkpoints,
)


_IO_CHUNK_SIZE = IO_CHUNK_SIZE
_XML_CHECKPOINT_INTERVAL = XML_CHECKPOINT_INTERVAL
_TEXT_CHUNK_CHARACTERS = 128 * 1024
_MAX_XML_NAME_CHARACTERS = _TEXT_CHUNK_CHARACTERS
_TRANSACTION_WORKSPACE_NAME = ".launchbox-utils-work"


class _CheckpointingUtf8Writer:
    def __init__(self, control: OperationControl) -> None:
        self.buffer = io.BytesIO()
        self.control = control
        self.fragments_since_checkpoint = 0
        self.bytes_since_checkpoint = 0

    def _write_piece(self, text: str) -> None:
        self.control.checkpoint()
        payload = text.encode("utf-8")
        self.buffer.write(payload)
        self.fragments_since_checkpoint += 1
        self.bytes_since_checkpoint += len(payload)
        if (
            self.fragments_since_checkpoint >= _XML_CHECKPOINT_INTERVAL
            or self.bytes_since_checkpoint >= _IO_CHUNK_SIZE
        ):
            self.control.checkpoint()
            self.fragments_since_checkpoint = 0
            self.bytes_since_checkpoint = 0

    def _write_chunks(self, text: str, escape) -> None:
        if not isinstance(text, str):
            raise TypeError(f"cannot serialize {text!r} (type {type(text).__name__})")
        for offset in range(0, len(text), _TEXT_CHUNK_CHARACTERS):
            chunk = text[offset : offset + _TEXT_CHUNK_CHARACTERS]
            self.control.checkpoint()
            self._write_piece(escape(chunk) if escape is not None else chunk)

    def write_raw(self, text: str) -> None:
        self._write_chunks(text, None)

    def write_cdata(self, text: str) -> None:
        self._write_chunks(text, _escape_cdata_chunk)

    def write_attribute(self, text: str) -> None:
        self._write_chunks(text, _escape_attribute_chunk)

    def getvalue(self) -> bytes:
        self.control.checkpoint()
        return self.buffer.getvalue()


def _escape_cdata_chunk(text: str) -> str:
    if "&" in text:
        text = text.replace("&", "&amp;")
    if "<" in text:
        text = text.replace("<", "&lt;")
    if ">" in text:
        text = text.replace(">", "&gt;")
    return text


def _escape_attribute_chunk(text: str) -> str:
    text = _escape_cdata_chunk(text)
    if '"' in text:
        text = text.replace('"', "&quot;")
    if "\r" in text:
        text = text.replace("\r", "&#13;")
    if "\n" in text:
        text = text.replace("\n", "&#10;")
    if "\t" in text:
        text = text.replace("\t", "&#09;")
    return text


class _CheckpointCounter:
    def __init__(self, control: OperationControl) -> None:
        self.control = control
        self.count = 0

    def tick(self) -> None:
        self.count += 1
        if self.count % _XML_CHECKPOINT_INTERVAL == 0:
            self.control.checkpoint()


def _collect_xml_namespaces(
    root: ET.Element,
    control: OperationControl,
) -> tuple[dict[object, str | None], dict[str, str]]:
    """Cancellable equivalent of ElementTree's namespace collection."""

    qnames: dict[object, str | None] = {None: None}
    namespaces: dict[str, str] = {}
    namespace_map: dict[str, str] = getattr(ET, "_namespace_map", {})
    checkpoints = _CheckpointCounter(control)

    def validate_qname_length(qname: str) -> None:
        try:
            if len(qname) > _MAX_XML_NAME_CHARACTERS:
                raise ValueError(
                    "XML qualified name exceeds the cancellable serialization limit "
                    f"of {_MAX_XML_NAME_CHARACTERS} characters"
                )
        except (TypeError, AttributeError) as exc:
            raise TypeError(
                f"cannot serialize {qname!r} (type {type(qname).__name__})"
            ) from exc

    def add_qname(qname: str) -> None:
        try:
            validate_qname_length(qname)
            if qname[:1] == "{":
                uri, tag = qname[1:].rsplit("}", 1)
                prefix = namespaces.get(uri)
                if prefix is None:
                    prefix = namespace_map.get(uri)
                    if prefix is None:
                        prefix = f"ns{len(namespaces)}"
                    if len(prefix) > _MAX_XML_NAME_CHARACTERS:
                        raise ValueError(
                            "XML namespace prefix exceeds the cancellable "
                            f"serialization limit of {_MAX_XML_NAME_CHARACTERS} characters"
                        )
                    if prefix != "xml":
                        namespaces[uri] = prefix
                qnames[qname] = f"{prefix}:{tag}" if prefix else tag
            else:
                qnames[qname] = qname
        except (TypeError, AttributeError) as exc:
            raise TypeError(
                f"cannot serialize {qname!r} (type {type(qname).__name__})"
            ) from exc

    control.checkpoint()
    for element in root.iter():
        checkpoints.tick()
        tag = element.tag
        if isinstance(tag, ET.QName):
            validate_qname_length(tag.text)
            if tag.text not in qnames:
                add_qname(tag.text)
        elif isinstance(tag, str):
            validate_qname_length(tag)
            if tag not in qnames:
                add_qname(tag)
        elif tag is not None and tag is not ET.Comment and tag is not ET.ProcessingInstruction:
            raise TypeError(f"cannot serialize {tag!r} (type {type(tag).__name__})")

        for key, value in element.attrib.items():
            checkpoints.tick()
            key_text = key.text if isinstance(key, ET.QName) else key
            validate_qname_length(key_text)
            if key_text not in qnames:
                add_qname(key_text)
            if isinstance(value, ET.QName):
                if value.text is not None:
                    validate_qname_length(value.text)
                    if value.text not in qnames:
                        add_qname(value.text)
        text = element.text
        if isinstance(text, ET.QName):
            validate_qname_length(text.text)
            if text.text not in qnames:
                add_qname(text.text)
    control.checkpoint()
    return qnames, namespaces


def _sort_namespaces_with_checkpoints(
    namespaces: dict[str, str],
    control: OperationControl,
) -> list[tuple[str, str]]:
    """Stable merge sort by prefix without an uninterruptible TimSort call."""

    checkpoints = _CheckpointCounter(control)
    items: list[tuple[str, str]] = []
    for item in namespaces.items():
        checkpoints.tick()
        items.append(item)

    width = 1
    while width < len(items):
        merged: list[tuple[str, str]] = []
        for start in range(0, len(items), width * 2):
            middle = min(start + width, len(items))
            end = min(start + width * 2, len(items))
            left = start
            right = middle
            while left < middle and right < end:
                control.checkpoint()
                if items[left][1] <= items[right][1]:
                    merged.append(items[left])
                    left += 1
                else:
                    merged.append(items[right])
                    right += 1
            while left < middle:
                checkpoints.tick()
                merged.append(items[left])
                left += 1
            while right < end:
                checkpoints.tick()
                merged.append(items[right])
                right += 1
        items = merged
        width *= 2
    control.checkpoint()
    return items


def _serialize_xml_element(
    writer: _CheckpointingUtf8Writer,
    element: ET.Element,
    qnames: dict[object, str | None],
    namespaces: dict[str, str] | None,
    checkpoints: _CheckpointCounter,
) -> None:
    checkpoints.tick()
    tag = element.tag
    text = element.text
    if tag is ET.Comment:
        writer.write_raw("<!--")
        writer.write_raw(str(text))
        writer.write_raw("-->")
    elif tag is ET.ProcessingInstruction:
        writer.write_raw("<?")
        writer.write_raw(str(text))
        writer.write_raw("?>")
    else:
        serialized_tag = qnames[tag]
        if serialized_tag is None:
            if text:
                writer.write_cdata(text)
            for child in element:
                _serialize_xml_element(writer, child, qnames, None, checkpoints)
        else:
            writer.write_raw("<")
            writer.write_raw(serialized_tag)
            if namespaces:
                for uri, prefix in _sort_namespaces_with_checkpoints(
                    namespaces,
                    writer.control,
                ):
                    writer.write_raw(" xmlns")
                    if prefix:
                        writer.write_raw(":")
                        writer.write_raw(prefix)
                    writer.write_raw('="')
                    writer.write_attribute(uri)
                    writer.write_raw('"')
            for key, value in element.attrib.items():
                checkpoints.tick()
                key_text = key.text if isinstance(key, ET.QName) else key
                writer.write_raw(" ")
                writer.write_raw(qnames[key_text])
                writer.write_raw('="')
                if isinstance(value, ET.QName):
                    qname_value = qnames[value.text]
                    writer.write_raw("None" if qname_value is None else qname_value)
                else:
                    writer.write_attribute(value)
                writer.write_raw('"')
            if text or len(element):
                writer.write_raw(">")
                if text:
                    writer.write_cdata(text)
                for child in element:
                    _serialize_xml_element(writer, child, qnames, None, checkpoints)
                writer.write_raw("</")
                writer.write_raw(serialized_tag)
                writer.write_raw(">")
            else:
                writer.write_raw(" />")
    if element.tail:
        writer.write_cdata(element.tail)


def _serialize_xml_tree(
    tree: ET.ElementTree,
    *,
    control: OperationControl | None = None,
) -> bytes:
    if control is None:
        buffer = io.BytesIO()
        tree.write(buffer, encoding="utf-8", xml_declaration=True)
        return buffer.getvalue()

    control.checkpoint()
    root = tree.getroot()
    qnames, namespaces = _collect_xml_namespaces(root, control)
    writer = _CheckpointingUtf8Writer(control)
    writer.write_raw("<?xml version='1.0' encoding='utf-8'?>\n")
    _serialize_xml_element(
        writer,
        root,
        qnames,
        namespaces,
        _CheckpointCounter(control),
    )
    return writer.getvalue()


def _write_bytes_with_checkpoints(
    path: Path,
    payload: bytes,
    *,
    control: OperationControl | None = None,
    exclusive: bool = False,
) -> None:
    if control is not None:
        control.checkpoint()
    with path.open("xb" if exclusive else "wb") as file:
        for offset in range(0, len(payload), _IO_CHUNK_SIZE):
            if control is not None:
                control.checkpoint()
            file.write(payload[offset : offset + _IO_CHUNK_SIZE])


def _validate_xml_file(
    path: Path,
    *,
    control: OperationControl | None = None,
) -> None:
    if control is None:
        ET.parse(path)
        return

    parse_xml_tree_with_checkpoints(path, control)


def _validate_xml_payload(
    payload: bytes,
    *,
    control: OperationControl | None = None,
) -> None:
    if control is None:
        ET.fromstring(payload)
        return

    control.checkpoint()
    parser = ET.XMLPullParser(events=("end",))
    event_count = 0
    for offset in range(0, len(payload), _IO_CHUNK_SIZE):
        control.checkpoint()
        parser.feed(payload[offset : offset + _IO_CHUNK_SIZE])
        for _event, _element in parser.read_events():
            event_count += 1
            if event_count % _XML_CHECKPOINT_INTERVAL == 0:
                control.checkpoint()
        control.checkpoint()
    parser.close()
    control.checkpoint()


def backup_xml_file(
    xml_path: Path,
    backup_root: Path,
    *,
    control: OperationControl | None = None,
    trusted_parent: Path | None = None,
    trust_anchor: Path | None = None,
    backup_trust_anchor: Path | None = None,
) -> Path:
    if control is not None:
        control.checkpoint()
    _ensure_trusted_path(trust_anchor, trusted_parent, xml_path)
    ensure_safe_to_mutate([xml_path])
    if backup_trust_anchor is not None:
        ensure_trusted_direct_child(
            backup_trust_anchor,
            backup_root.parent,
            backup_root,
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        ensure_trusted_direct_child(
            backup_trust_anchor,
            backup_root.parent,
            backup_root,
        )
    else:
        backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / xml_path.name
    created = False
    try:
        _ensure_trusted_path(trust_anchor, trusted_parent, xml_path)
        if backup_trust_anchor is not None:
            ensure_trusted_direct_child(
                backup_trust_anchor,
                backup_root,
                backup_path,
            )
        with xml_path.open("rb") as source, backup_path.open("xb") as destination:
            created = True
            while chunk := source.read(_IO_CHUNK_SIZE):
                if control is not None:
                    control.checkpoint()
                destination.write(chunk)
        _ensure_trusted_path(trust_anchor, trusted_parent, xml_path)
        if backup_trust_anchor is not None:
            ensure_trusted_direct_child(
                backup_trust_anchor,
                backup_root,
                backup_path,
            )
        shutil.copystat(xml_path, backup_path)
    except Exception:
        if created:
            try:
                if backup_trust_anchor is not None:
                    ensure_trusted_direct_child(
                        backup_trust_anchor,
                        backup_root,
                        backup_path,
                    )
                backup_path.unlink(missing_ok=True)
            except (OSError, RuntimeError, ValueError):
                pass
        raise
    return backup_path


def backup_platform_xml(
    platform: PlatformInfo,
    backup_root: Path,
    *,
    control: OperationControl | None = None,
) -> Path:
    return backup_xml_file(platform.database_xml, backup_root, control=control)


def reserve_unique_backup_root(
    backup_parent: Path,
    name: str,
    *,
    trusted_parent: Path | None = None,
    trust_anchor: Path | None = None,
) -> Path:
    _ensure_trusted_path(trust_anchor, trusted_parent, backup_parent)
    if backup_parent.exists() and not backup_parent.is_dir():
        raise NotADirectoryError(f"Backup parent is not a directory: {backup_parent}")
    backup_parent.mkdir(parents=True, exist_ok=True)
    _ensure_trusted_path(trust_anchor, trusted_parent, backup_parent)

    candidate = backup_parent / name
    created = False
    try:
        _ensure_trusted_path(
            trust_anchor,
            backup_parent if trust_anchor is not None else None,
            candidate,
        )
        candidate.mkdir(exist_ok=False)
        created = True
        _ensure_trusted_path(
            trust_anchor,
            backup_parent if trust_anchor is not None else None,
            candidate,
        )
        return candidate
    except Exception:
        if created:
            try:
                _ensure_trusted_path(
                    trust_anchor,
                    backup_parent if trust_anchor is not None else None,
                    candidate,
                )
                candidate.rmdir()
            except (OSError, RuntimeError, ValueError):
                pass
        raise


def sha256_file(
    path: Path,
    *,
    control: OperationControl | None = None,
) -> str:
    if control is not None:
        control.checkpoint()
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(_IO_CHUNK_SIZE), b""):
            if control is not None:
                control.checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


def write_xml_tree_safely(tree: ET.ElementTree, destination: Path) -> None:
    ensure_safe_to_mutate([destination])
    temp_path = destination.with_name(f".{destination.name}.{uuid4()}.tmp")
    try:
        tree.write(temp_path, encoding="utf-8", xml_declaration=True)
        _validate_xml_file(temp_path)
        os.replace(temp_path, destination)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass(frozen=True)
class XmlMutation:
    destination: Path
    tree: ET.ElementTree
    trusted_parent: Path | None = None
    trust_anchor: Path | None = None


def _ensure_trusted_path(
    trust_anchor: Path | None,
    trusted_parent: Path | None,
    path: Path,
) -> None:
    if (trusted_parent is None) != (trust_anchor is None):
        raise ValueError("trusted_parent and trust_anchor must be provided together")
    if trusted_parent is not None and trust_anchor is not None:
        ensure_trusted_direct_child(trust_anchor, trusted_parent, path)


def _ensure_mutation_destination_trusted(
    mutation: XmlMutation,
    destination: Path,
) -> None:
    _ensure_trusted_path(
        mutation.trust_anchor,
        mutation.trusted_parent,
        destination,
    )


def _trusted_transaction_anchor(mutations: list[XmlMutation]) -> Path | None:
    anchors: list[Path] = []
    for mutation in mutations:
        if (mutation.trusted_parent is None) != (mutation.trust_anchor is None):
            raise ValueError("XmlMutation trusted_parent and trust_anchor must be provided together")
        if mutation.trust_anchor is not None:
            anchors.append(normalize_trust_anchor(mutation.trust_anchor))

    if not anchors:
        return None
    if len(anchors) != len(mutations):
        raise ValueError("Trusted and untrusted XmlMutation entries cannot share a transaction")
    anchor = anchors[0]
    if any(candidate != anchor for candidate in anchors[1:]):
        raise ValueError("All trusted XmlMutation entries must share one trust anchor")
    return anchor


def _reserve_transaction_workspace(anchor: Path) -> tuple[Path, Path]:
    workspace_parent = anchor / _TRANSACTION_WORKSPACE_NAME
    workspace_root = workspace_parent / str(uuid4())
    try:
        ensure_trusted_direct_child(anchor, anchor, workspace_parent)
        workspace_parent.mkdir(exist_ok=True)
        ensure_trusted_direct_child(anchor, anchor, workspace_parent)
        ensure_trusted_direct_child(anchor, workspace_parent, workspace_root)
        workspace_root.mkdir(exist_ok=False)
        ensure_trusted_direct_child(anchor, workspace_parent, workspace_root)
        return workspace_parent, workspace_root
    except Exception:
        _cleanup_transaction_workspace(
            anchor,
            workspace_parent,
            workspace_root,
            (),
        )
        raise


def _cleanup_transaction_workspace(
    anchor: Path,
    workspace_parent: Path,
    workspace_root: Path,
    paths: tuple[Path, ...] | list[Path],
) -> None:
    for path in paths:
        try:
            ensure_trusted_direct_child(anchor, workspace_root, path)
            path.unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError):
            pass
    try:
        ensure_trusted_direct_child(anchor, workspace_parent, workspace_root)
        workspace_root.rmdir()
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        ensure_trusted_direct_child(anchor, anchor, workspace_parent)
        workspace_parent.rmdir()
    except (OSError, RuntimeError, ValueError):
        pass


def _workspace_path(
    anchor: Path | None,
    workspace_root: Path | None,
    destination: Path,
    transaction_id: str,
    index: int,
    kind: str,
) -> Path:
    if anchor is None or workspace_root is None:
        return destination.with_name(f".{destination.name}.{transaction_id}.{kind}.tmp")
    path = workspace_root / f"{index:04d}.{kind}.xml"
    ensure_trusted_direct_child(anchor, workspace_root, path)
    return path


def _unsafe_path_error(exc: Exception) -> UnsafeDatabasePathError | None:
    return exc if isinstance(exc, UnsafeDatabasePathError) else None


def _blocked_reason(exc: Exception) -> str | None:
    return exc.reason if isinstance(exc, MutationBlockedError) else None


def _blocked_details(exc: Exception) -> str | None:
    if not isinstance(exc, MutationBlockedError):
        return None
    return exc.details or str(exc)


@dataclass
class XmlTransactionResult:
    outcome: MutationOutcome
    backup_paths: dict[Path, Path] = field(default_factory=dict)
    error: str | None = None
    rollback_errors: list[str] = field(default_factory=list)
    files: list[MutationFileResult] = field(default_factory=list)
    unsafe_path_error: UnsafeDatabasePathError | None = None
    blocked_reason: str | None = None
    blocked_details: str | None = None


def _commit_staged_file(stage_path: Path, destination: Path) -> None:
    os.replace(stage_path, destination)


def _restore_backup(
    backup_path: Path,
    destination: Path,
    rollback_path: Path,
    *,
    before_copy: Callable[[], None] | None = None,
    before_replace: Callable[[], None] | None = None,
) -> None:
    if before_copy is not None:
        before_copy()
    with backup_path.open("rb") as source, rollback_path.open("xb") as target:
        while chunk := source.read(_IO_CHUNK_SIZE):
            target.write(chunk)
    shutil.copystat(backup_path, rollback_path)
    ET.parse(rollback_path)
    if before_replace is not None:
        before_replace()
    os.replace(rollback_path, destination)


def execute_xml_transaction(
    mutations: list[XmlMutation],
    backup_root: Path,
    run_id: str | None = None,
    *,
    control: OperationControl | None = None,
) -> XmlTransactionResult:
    if not mutations:
        if control is not None:
            try:
                control.checkpoint()
            except OperationCancelled as exc:
                return XmlTransactionResult(MutationOutcome.CANCELLED, error=str(exc))
        return XmlTransactionResult(outcome=MutationOutcome.SUCCESS)

    backups: dict[Path, Path] = {}
    stage_paths: dict[Path, Path] = {}
    rollback_paths: set[Path] = set()
    committed: list[Path] = []
    workspace_parent: Path | None = None
    workspace_root: Path | None = None
    transaction_anchor: Path | None = None
    transaction_id = run_id or str(uuid4())
    files = [MutationFileResult(mutation.destination.absolute()) for mutation in mutations]
    files_by_path = {result.path: result for result in files}
    mutations_by_path = {
        file_result.path: mutation
        for mutation, file_result in zip(mutations, files)
    }
    destination_indexes = {
        file_result.path: index
        for index, file_result in enumerate(files, start=1)
    }

    try:
        if control is not None:
            control.set_phase(OperationPhase.STAGE)
            control.checkpoint()
        transaction_anchor = _trusted_transaction_anchor(mutations)

        serialized: dict[Path, bytes] = {}
        seen: set[Path] = set()
        for mutation, file_result in zip(mutations, files):
            if control is not None:
                control.checkpoint()
            destination = file_result.path
            try:
                _ensure_mutation_destination_trusted(mutation, destination)
                canonical_destination = destination.resolve(strict=False)
                if canonical_destination in seen:
                    raise ValueError(f"Duplicate XML transaction destination: {destination}")
                if not destination.is_file():
                    raise FileNotFoundError(f"XML transaction destination not found: {destination}")
                seen.add(canonical_destination)
                payload = _serialize_xml_tree(mutation.tree, control=control)
                _validate_xml_payload(payload, control=control)
                serialized[destination] = payload
            except OperationCancelled:
                raise
            except Exception as exc:
                file_result.state = MutationState.FAILED
                file_result.error = str(exc)
                return XmlTransactionResult(
                    MutationOutcome.FAILED,
                    backups,
                    str(exc),
                    files=files,
                    unsafe_path_error=_unsafe_path_error(exc),
                    blocked_reason=_blocked_reason(exc),
                    blocked_details=_blocked_details(exc),
                )

        destinations = list(serialized)
        backup_roots = {
            destination: backup_root / f"{index:04d}"
            for index, destination in enumerate(destinations, start=1)
        }
        ensure_safe_to_mutate(destinations)

        try:
            for mutation, destination in zip(mutations, destinations):
                if control is not None:
                    control.checkpoint()
                _ensure_mutation_destination_trusted(mutation, destination)
                source_sha256 = sha256_file(destination, control=control)
                _ensure_mutation_destination_trusted(mutation, destination)
                backups[destination] = backup_xml_file(
                    destination,
                    backup_roots[destination],
                    control=control,
                    trusted_parent=mutation.trusted_parent,
                    trust_anchor=mutation.trust_anchor,
                    backup_trust_anchor=transaction_anchor,
                )
                files_by_path[destination].backup_path = backups[destination]
                if transaction_anchor is not None:
                    ensure_trusted_direct_child(
                        transaction_anchor,
                        backup_roots[destination],
                        backups[destination],
                    )
                backup_sha256 = sha256_file(backups[destination], control=control)
                if transaction_anchor is not None:
                    ensure_trusted_direct_child(
                        transaction_anchor,
                        backup_roots[destination],
                        backups[destination],
                    )
                if backup_sha256 != source_sha256:
                    raise OSError(f"Backup verification failed for {destination}")
                files_by_path[destination].source_sha256 = source_sha256
        except OperationCancelled:
            raise
        except Exception as exc:
            file_result = files_by_path[destination]
            file_result.state = MutationState.FAILED
            file_result.error = str(exc)
            return XmlTransactionResult(
                MutationOutcome.FAILED,
                backups,
                str(exc),
                files=files,
                unsafe_path_error=_unsafe_path_error(exc),
                blocked_reason=_blocked_reason(exc),
                blocked_details=_blocked_details(exc),
            )

        try:
            if transaction_anchor is not None:
                workspace_parent, workspace_root = _reserve_transaction_workspace(
                    transaction_anchor
                )
            for mutation, (destination, payload) in zip(mutations, serialized.items()):
                if control is not None:
                    control.checkpoint()
                _ensure_mutation_destination_trusted(mutation, destination)
                stage_path = _workspace_path(
                    transaction_anchor,
                    workspace_root,
                    destination,
                    transaction_id,
                    destination_indexes[destination],
                    "stage",
                )
                stage_paths[destination] = stage_path
                _write_bytes_with_checkpoints(
                    stage_path,
                    payload,
                    control=control,
                    exclusive=True,
                )
                _validate_xml_file(stage_path, control=control)
                files_by_path[destination].state = MutationState.PREPARED
        except OperationCancelled:
            raise
        except Exception as exc:
            file_result = files_by_path[destination]
            file_result.state = MutationState.FAILED
            file_result.error = str(exc)
            return XmlTransactionResult(
                MutationOutcome.FAILED,
                backups,
                str(exc),
                files=files,
                unsafe_path_error=_unsafe_path_error(exc),
                blocked_reason=_blocked_reason(exc),
                blocked_details=_blocked_details(exc),
            )

        if control is not None:
            control.checkpoint()

        try:
            for mutation, destination in zip(mutations, destinations):
                _ensure_mutation_destination_trusted(mutation, destination)
            ensure_safe_to_mutate(destinations)
        except Exception as exc:
            files_by_path[destination].error = str(exc)
            return XmlTransactionResult(
                MutationOutcome.FAILED,
                backups,
                str(exc),
                files=files,
                unsafe_path_error=_unsafe_path_error(exc),
                blocked_reason=_blocked_reason(exc),
                blocked_details=_blocked_details(exc),
            )

        if control is not None:
            control.begin_commit()

        try:
            for mutation, destination in zip(mutations, destinations):
                _ensure_mutation_destination_trusted(mutation, destination)
                if transaction_anchor is not None and workspace_root is not None:
                    ensure_trusted_direct_child(
                        transaction_anchor,
                        workspace_root,
                        stage_paths[destination],
                    )
                _commit_staged_file(stage_paths[destination], destination)
                committed.append(destination)
                files_by_path[destination].state = MutationState.COMMITTED
        except Exception as exc:
            failed_result = files_by_path[destination]
            failed_result.state = MutationState.FAILED
            failed_result.error = str(exc)
            rollback_errors: list[str] = []
            unsafe_path_error = _unsafe_path_error(exc)
            if committed and control is not None:
                control.set_phase(OperationPhase.ROLLBACK)
            for destination in reversed(committed):
                try:
                    rollback_path = _workspace_path(
                        transaction_anchor,
                        workspace_root,
                        destination,
                        transaction_id,
                        destination_indexes[destination],
                        "rollback",
                    )
                    rollback_paths.add(rollback_path)
                    mutation = mutations_by_path[destination]

                    def validate_rollback_paths() -> None:
                        _ensure_mutation_destination_trusted(mutation, destination)
                        if transaction_anchor is not None and workspace_root is not None:
                            ensure_trusted_direct_child(
                                transaction_anchor,
                                backups[destination].parent,
                                backups[destination],
                            )
                            ensure_trusted_direct_child(
                                transaction_anchor,
                                workspace_root,
                                rollback_path,
                            )

                    _restore_backup(
                        backups[destination],
                        destination,
                        rollback_path,
                        before_copy=validate_rollback_paths,
                        before_replace=validate_rollback_paths,
                    )
                    files_by_path[destination].state = MutationState.ROLLED_BACK
                except Exception as rollback_exc:
                    if unsafe_path_error is None:
                        unsafe_path_error = _unsafe_path_error(rollback_exc)
                    message = f"{destination}: {rollback_exc}"
                    rollback_errors.append(message)
                    files_by_path[destination].state = MutationState.FAILED
                    files_by_path[destination].rollback_error = str(rollback_exc)
            outcome = MutationOutcome.FAILED if rollback_errors or not committed else MutationOutcome.ROLLED_BACK
            return XmlTransactionResult(
                outcome,
                backups,
                str(exc),
                rollback_errors,
                files,
                unsafe_path_error,
            )

        return XmlTransactionResult(MutationOutcome.SUCCESS, backups, files=files)
    except OperationCancelled as exc:
        return XmlTransactionResult(
            MutationOutcome.CANCELLED,
            backups,
            str(exc),
            files=files,
        )
    except Exception as exc:
        return XmlTransactionResult(
            MutationOutcome.FAILED,
            backups,
            str(exc),
            files=files,
            unsafe_path_error=_unsafe_path_error(exc),
            blocked_reason=_blocked_reason(exc),
            blocked_details=_blocked_details(exc),
        )
    finally:
        cleanup_paths = [*stage_paths.values(), *rollback_paths]
        if (
            transaction_anchor is not None
            and workspace_parent is not None
            and workspace_root is not None
        ):
            _cleanup_transaction_workspace(
                transaction_anchor,
                workspace_parent,
                workspace_root,
                cleanup_paths,
            )
        else:
            for path in cleanup_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

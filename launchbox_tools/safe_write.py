from __future__ import annotations

import hashlib
import io
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .models import MutationFileResult, MutationOutcome, MutationState, PlatformInfo
from .operation_lifecycle import OperationCancelled, OperationControl, OperationPhase
from .runtime_checks import ensure_safe_to_mutate
from .xml_checkpoint_io import (
    IO_CHUNK_SIZE,
    XML_CHECKPOINT_INTERVAL,
    parse_xml_tree_with_checkpoints,
)


_IO_CHUNK_SIZE = IO_CHUNK_SIZE
_XML_CHECKPOINT_INTERVAL = XML_CHECKPOINT_INTERVAL
_TEXT_CHUNK_CHARACTERS = 128 * 1024
_MAX_XML_NAME_CHARACTERS = _TEXT_CHUNK_CHARACTERS


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
) -> None:
    if control is not None:
        control.checkpoint()
    with path.open("wb") as file:
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
) -> Path:
    if control is not None:
        control.checkpoint()
    ensure_safe_to_mutate([xml_path])
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / xml_path.name
    created = False
    try:
        with xml_path.open("rb") as source, backup_path.open("xb") as destination:
            created = True
            while chunk := source.read(_IO_CHUNK_SIZE):
                if control is not None:
                    control.checkpoint()
                destination.write(chunk)
        shutil.copystat(xml_path, backup_path)
    except Exception:
        if created:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
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


def reserve_unique_backup_root(backup_parent: Path, name: str) -> Path:
    if backup_parent.exists() and not backup_parent.is_dir():
        raise NotADirectoryError(f"Backup parent is not a directory: {backup_parent}")
    backup_parent.mkdir(parents=True, exist_ok=True)

    candidate = backup_parent / name
    candidate.mkdir(exist_ok=False)
    return candidate


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


@dataclass
class XmlTransactionResult:
    outcome: MutationOutcome
    backup_paths: dict[Path, Path] = field(default_factory=dict)
    error: str | None = None
    rollback_errors: list[str] = field(default_factory=list)
    files: list[MutationFileResult] = field(default_factory=list)


def _commit_staged_file(stage_path: Path, destination: Path) -> None:
    os.replace(stage_path, destination)


def _restore_backup(backup_path: Path, destination: Path, rollback_path: Path) -> None:
    shutil.copy2(backup_path, rollback_path)
    ET.parse(rollback_path)
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
    transaction_id = run_id or str(uuid4())
    files = [MutationFileResult(mutation.destination.absolute()) for mutation in mutations]
    files_by_path = {result.path: result for result in files}

    try:
        if control is not None:
            control.set_phase(OperationPhase.STAGE)
            control.checkpoint()

        serialized: dict[Path, bytes] = {}
        seen: set[Path] = set()
        for mutation, file_result in zip(mutations, files):
            if control is not None:
                control.checkpoint()
            destination = file_result.path
            try:
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
                return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)

        destinations = list(serialized)
        backup_roots = {
            destination: backup_root / f"{index:04d}"
            for index, destination in enumerate(destinations, start=1)
        }
        ensure_safe_to_mutate(destinations)

        try:
            for destination in destinations:
                if control is not None:
                    control.checkpoint()
                source_sha256 = sha256_file(destination, control=control)
                backups[destination] = backup_xml_file(
                    destination,
                    backup_roots[destination],
                    control=control,
                )
                files_by_path[destination].backup_path = backups[destination]
                backup_sha256 = sha256_file(backups[destination], control=control)
                if backup_sha256 != source_sha256:
                    raise OSError(f"Backup verification failed for {destination}")
                files_by_path[destination].source_sha256 = source_sha256
        except OperationCancelled:
            raise
        except Exception as exc:
            file_result = files_by_path[destination]
            file_result.state = MutationState.FAILED
            file_result.error = str(exc)
            return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)

        try:
            for destination, payload in serialized.items():
                if control is not None:
                    control.checkpoint()
                stage_path = destination.with_name(
                    f".{destination.name}.{transaction_id}.stage.tmp"
                )
                stage_paths[destination] = stage_path
                _write_bytes_with_checkpoints(stage_path, payload, control=control)
                _validate_xml_file(stage_path, control=control)
                files_by_path[destination].state = MutationState.PREPARED
        except OperationCancelled:
            raise
        except Exception as exc:
            file_result = files_by_path[destination]
            file_result.state = MutationState.FAILED
            file_result.error = str(exc)
            return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)

        try:
            ensure_safe_to_mutate(destinations)
        except Exception as exc:
            return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)

        if control is not None:
            control.checkpoint()
            control.begin_commit()

        try:
            for destination in destinations:
                _commit_staged_file(stage_paths[destination], destination)
                committed.append(destination)
                files_by_path[destination].state = MutationState.COMMITTED
        except Exception as exc:
            failed_result = files_by_path[destination]
            failed_result.state = MutationState.FAILED
            failed_result.error = str(exc)
            rollback_errors: list[str] = []
            if committed and control is not None:
                control.set_phase(OperationPhase.ROLLBACK)
            for destination in reversed(committed):
                rollback_path = destination.with_name(
                    f".{destination.name}.{transaction_id}.rollback.tmp"
                )
                rollback_paths.add(rollback_path)
                try:
                    _restore_backup(backups[destination], destination, rollback_path)
                    files_by_path[destination].state = MutationState.ROLLED_BACK
                except Exception as rollback_exc:
                    message = f"{destination}: {rollback_exc}"
                    rollback_errors.append(message)
                    files_by_path[destination].state = MutationState.FAILED
                    files_by_path[destination].rollback_error = str(rollback_exc)
            outcome = MutationOutcome.FAILED if rollback_errors or not committed else MutationOutcome.ROLLED_BACK
            return XmlTransactionResult(outcome, backups, str(exc), rollback_errors, files)

        return XmlTransactionResult(MutationOutcome.SUCCESS, backups, files=files)
    except OperationCancelled as exc:
        return XmlTransactionResult(
            MutationOutcome.CANCELLED,
            backups,
            str(exc),
            files=files,
        )
    except Exception as exc:
        return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)
    finally:
        for path in [*stage_paths.values(), *rollback_paths]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

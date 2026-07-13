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


_IO_CHUNK_SIZE = 1024 * 1024
_XML_CHECKPOINT_INTERVAL = 256


class _CheckpointingTextSink:
    def __init__(self, control: OperationControl) -> None:
        self.buffer = io.BytesIO()
        self.control = control
        self.fragments_since_checkpoint = 0
        self.bytes_since_checkpoint = 0

    def write(self, text: str) -> int:
        payload = text.encode("utf-8")
        self.fragments_since_checkpoint += 1
        self.bytes_since_checkpoint += len(payload)
        if (
            self.fragments_since_checkpoint >= _XML_CHECKPOINT_INTERVAL
            or self.bytes_since_checkpoint >= _IO_CHUNK_SIZE
        ):
            self.control.checkpoint()
            self.fragments_since_checkpoint = 0
            self.bytes_since_checkpoint = 0
        return self.buffer.write(payload)

    def getvalue(self) -> bytes:
        self.control.checkpoint()
        return self.buffer.getvalue()


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
    sink = _CheckpointingTextSink(control)
    tree.write(sink, encoding="unicode", xml_declaration=True)
    return sink.getvalue()


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

    control.checkpoint()
    iterator = ET.iterparse(path, events=("end",))
    for index, _event in enumerate(iterator, start=1):
        if index % _XML_CHECKPOINT_INTERVAL == 0:
            control.checkpoint()
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
        payload = _serialize_xml_tree(tree)
        _write_bytes_with_checkpoints(temp_path, payload)
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
                ET.fromstring(payload)
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

from __future__ import annotations

import io
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .models import MutationFileResult, MutationOutcome, MutationState, PlatformInfo
from .runtime_checks import ensure_safe_to_mutate


def backup_xml_file(xml_path: Path, backup_root: Path) -> Path:
    ensure_safe_to_mutate([xml_path])
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / xml_path.name
    shutil.copy2(xml_path, backup_path)
    return backup_path


def backup_platform_xml(platform: PlatformInfo, backup_root: Path) -> Path:
    return backup_xml_file(platform.database_xml, backup_root)


def reserve_unique_backup_root(backup_parent: Path, name: str) -> Path:
    if backup_parent.exists() and not backup_parent.is_dir():
        raise NotADirectoryError(f"Backup parent is not a directory: {backup_parent}")
    backup_parent.mkdir(parents=True, exist_ok=True)

    attempt = 1
    while True:
        suffix = "" if attempt == 1 else f"-{attempt}"
        candidate = backup_parent / f"{name}{suffix}"
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            if not candidate.exists():
                raise
            attempt += 1


def write_xml_tree_safely(tree: ET.ElementTree, destination: Path) -> None:
    ensure_safe_to_mutate([destination])
    temp_path = destination.with_name(f"{destination.name}.tmp")
    tree.write(temp_path, encoding="utf-8", xml_declaration=True)
    ET.parse(temp_path)
    os.replace(temp_path, destination)


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


def execute_xml_transaction(mutations: list[XmlMutation], backup_root: Path) -> XmlTransactionResult:
    if not mutations:
        return XmlTransactionResult(outcome=MutationOutcome.SUCCESS)

    backups: dict[Path, Path] = {}
    stage_paths: dict[Path, Path] = {}
    rollback_paths: set[Path] = set()
    committed: list[Path] = []
    files = [MutationFileResult(mutation.destination.absolute()) for mutation in mutations]
    files_by_path = {result.path: result for result in files}

    try:
        serialized: dict[Path, bytes] = {}
        seen: set[Path] = set()
        for mutation, file_result in zip(mutations, files):
            destination = file_result.path
            try:
                canonical_destination = destination.resolve(strict=False)
                if canonical_destination in seen:
                    raise ValueError(f"Duplicate XML transaction destination: {destination}")
                if not destination.is_file():
                    raise FileNotFoundError(f"XML transaction destination not found: {destination}")
                seen.add(canonical_destination)
                buffer = io.BytesIO()
                mutation.tree.write(buffer, encoding="utf-8", xml_declaration=True)
                payload = buffer.getvalue()
                ET.fromstring(payload)
                serialized[destination] = payload
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
                backups[destination] = backup_xml_file(destination, backup_roots[destination])
                files_by_path[destination].backup_path = backups[destination]
        except Exception as exc:
            file_result = files_by_path[destination]
            file_result.state = MutationState.FAILED
            file_result.error = str(exc)
            return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)

        try:
            for destination, payload in serialized.items():
                stage_path = destination.with_name(f"{destination.name}.stage.tmp")
                stage_paths[destination] = stage_path
                stage_path.write_bytes(payload)
                ET.parse(stage_path)
                files_by_path[destination].state = MutationState.PREPARED
        except Exception as exc:
            file_result = files_by_path[destination]
            file_result.state = MutationState.FAILED
            file_result.error = str(exc)
            return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)

        try:
            ensure_safe_to_mutate(destinations)
        except Exception as exc:
            return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)

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
            for destination in reversed(committed):
                rollback_path = destination.with_name(f"{destination.name}.rollback.tmp")
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
    except Exception as exc:
        return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc), files=files)
    finally:
        for path in [*stage_paths.values(), *rollback_paths]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

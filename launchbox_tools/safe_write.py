from __future__ import annotations

import io
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .models import MutationOutcome, PlatformInfo
from .runtime_checks import ensure_safe_to_mutate


def backup_xml_file(xml_path: Path, backup_root: Path) -> Path:
    ensure_safe_to_mutate([xml_path])
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / xml_path.name
    shutil.copy2(xml_path, backup_path)
    return backup_path


def backup_platform_xml(platform: PlatformInfo, backup_root: Path) -> Path:
    return backup_xml_file(platform.database_xml, backup_root)


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


def _serialize_and_validate(mutations: list[XmlMutation]) -> dict[Path, bytes]:
    serialized: dict[Path, bytes] = {}
    seen: set[Path] = set()
    for mutation in mutations:
        destination = mutation.destination.resolve(strict=False)
        if destination in seen:
            raise ValueError(f"Duplicate XML transaction destination: {destination}")
        if not destination.is_file():
            raise FileNotFoundError(f"XML transaction destination not found: {destination}")
        seen.add(destination)
        buffer = io.BytesIO()
        mutation.tree.write(buffer, encoding="utf-8", xml_declaration=True)
        payload = buffer.getvalue()
        ET.fromstring(payload)
        serialized[destination] = payload
    return serialized


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

    try:
        serialized = _serialize_and_validate(mutations)
        destinations = list(serialized)
        ensure_safe_to_mutate(destinations)

        try:
            for destination in destinations:
                backups[destination] = backup_xml_file(destination, backup_root)
        except Exception as exc:
            return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc))

        try:
            for destination, payload in serialized.items():
                stage_path = destination.with_name(f"{destination.name}.stage.tmp")
                stage_paths[destination] = stage_path
                stage_path.write_bytes(payload)
                ET.parse(stage_path)
        except Exception as exc:
            return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc))

        ensure_safe_to_mutate(destinations)
        try:
            for destination in destinations:
                _commit_staged_file(stage_paths[destination], destination)
                committed.append(destination)
        except Exception as exc:
            rollback_errors: list[str] = []
            for destination in reversed(committed):
                rollback_path = destination.with_name(f"{destination.name}.rollback.tmp")
                rollback_paths.add(rollback_path)
                try:
                    _restore_backup(backups[destination], destination, rollback_path)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{destination}: {rollback_exc}")
            outcome = MutationOutcome.FAILED if rollback_errors or not committed else MutationOutcome.ROLLED_BACK
            return XmlTransactionResult(outcome, backups, str(exc), rollback_errors)

        return XmlTransactionResult(MutationOutcome.SUCCESS, backups)
    except Exception as exc:
        return XmlTransactionResult(MutationOutcome.FAILED, backups, str(exc))
    finally:
        for path in [*stage_paths.values(), *rollback_paths]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

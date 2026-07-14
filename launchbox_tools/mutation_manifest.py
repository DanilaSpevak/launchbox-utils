from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from .diagnostics import describe_exception
from .models import MutationRunResult
from .paths import UnsafeDatabasePathError, ensure_trusted_direct_child


def _write_manifest_temp(path: Path, serialized: str) -> None:
    with path.open("x", encoding="utf-8") as temp_file:
        temp_file.write(serialized)


def write_mutation_manifest(
    run_result: MutationRunResult[Any],
    backup_root: Path,
    operation: str,
    changes: list[dict[str, object]],
    *,
    trust_anchor: Path | None = None,
) -> None:
    if run_result.run_id is None:
        run_result.run_id = str(uuid4())
    manifest_path = backup_root / "manifest.json"
    payload = {
        "schema_version": 2,
        "run_id": run_result.run_id,
        "operation": operation,
        "mode": "apply",
        "outcome": run_result.outcome.value,
        "error": run_result.error,
        "rollback_errors": run_result.rollback_errors,
        "files": [
            {
                "path": str(file_result.path),
                "state": file_result.state.value,
                "backup_path": str(file_result.backup_path) if file_result.backup_path else None,
                "error": file_result.error,
                "rollback_error": file_result.rollback_error,
                "source_sha256": file_result.source_sha256,
            }
            for file_result in run_result.files
        ],
        "changes": changes,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_path = manifest_path.with_name(f".{manifest_path.name}.{run_result.run_id}.tmp")

    try:
        if trust_anchor is not None:
            ensure_trusted_direct_child(trust_anchor, backup_root.parent, backup_root)
        backup_root.mkdir(parents=True, exist_ok=True)
        if trust_anchor is not None:
            ensure_trusted_direct_child(trust_anchor, backup_root.parent, backup_root)
            ensure_trusted_direct_child(trust_anchor, backup_root, temp_path)
        _write_manifest_temp(temp_path, serialized)
        if trust_anchor is not None:
            ensure_trusted_direct_child(trust_anchor, backup_root, temp_path)
            ensure_trusted_direct_child(trust_anchor, backup_root, manifest_path)
        os.replace(temp_path, manifest_path)
        run_result.manifest_path = manifest_path
    except (OSError, UnsafeDatabasePathError) as exc:
        run_result.manifest_error = describe_exception(exc)
    finally:
        try:
            if trust_anchor is not None:
                ensure_trusted_direct_child(trust_anchor, backup_root, temp_path)
            temp_path.unlink(missing_ok=True)
        except (OSError, UnsafeDatabasePathError):
            pass

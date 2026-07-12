from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .diagnostics import describe_exception
from .models import MutationRunResult


def write_mutation_manifest(
    run_result: MutationRunResult[Any],
    backup_root: Path,
    operation: str,
    changes: list[dict[str, object]],
) -> None:
    manifest_path = backup_root / "manifest.json"
    payload = {
        "schema_version": 1,
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
            }
            for file_result in run_result.files
        ],
        "changes": changes,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_path = manifest_path.with_name(f"{manifest_path.name}.tmp")

    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(serialized, encoding="utf-8")
        os.replace(temp_path, manifest_path)
        run_result.manifest_path = manifest_path
    except OSError as exc:
        run_result.manifest_error = describe_exception(exc)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

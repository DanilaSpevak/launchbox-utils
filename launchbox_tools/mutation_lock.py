from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from .runtime_checks import MutationBlockedError


LOCK_FILE_NAME = ".launchbox-utils-mutation.lock"


def _ensure_lock_byte(file: BinaryIO) -> None:
    file.seek(0, os.SEEK_END)
    if file.tell() == 0:
        file.write(b"\0")
        file.flush()
    file.seek(0)


def _try_lock(file: BinaryIO) -> bool:
    file.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(file: BinaryIO) -> None:
    file.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _read_owner(file: BinaryIO) -> dict[str, object]:
    try:
        file.seek(1)
        payload = json.loads(file.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _owner_text(owner: dict[str, object]) -> str:
    details: list[str] = []
    for label, key in (("operation", "operation"), ("run_id", "run_id"), ("pid", "pid")):
        value = owner.get(key)
        if value not in (None, ""):
            details.append(f"{label}={value}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"Another LaunchBox Utils mutation is already running{suffix}."


def _string_value(owner: dict[str, object], key: str) -> str | None:
    value = owner.get(key)
    return value if isinstance(value, str) and value else None


def _pid_value(owner: dict[str, object]) -> int | None:
    value = owner.get("pid")
    return value if isinstance(value, int) else None


@contextmanager
def mutation_run_lock(root: Path, operation: str, run_id: str) -> Iterator[Path]:
    lock_path = root.resolve(strict=False) / "Data" / LOCK_FILE_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    file = lock_path.open("a+b", buffering=0)
    acquired = False
    try:
        _ensure_lock_byte(file)
        acquired = _try_lock(file)
        if not acquired:
            owner = _read_owner(file)
            raise MutationBlockedError(
                _owner_text(owner),
                reason="mutation_in_progress",
                active_operation=_string_value(owner, "operation"),
                active_run_id=_string_value(owner, "run_id"),
                active_pid=_pid_value(owner),
                active_started_at=_string_value(owner, "started_at"),
            )

        metadata = {
            "schema_version": 1,
            "run_id": run_id,
            "operation": operation,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        file.seek(1)
        file.truncate()
        file.write((json.dumps(metadata, ensure_ascii=False) + "\n").encode("utf-8"))
        file.flush()
        os.fsync(file.fileno())
        yield lock_path
    finally:
        try:
            if acquired:
                _unlock(file)
        finally:
            file.close()

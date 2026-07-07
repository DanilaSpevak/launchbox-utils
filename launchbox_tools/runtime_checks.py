from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class MutationBlockedError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason: str,
        locked_files: list[Path] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.locked_files = locked_files or []


LAUNCHBOX_PROCESS_NAMES = (
    "LaunchBox.exe",
    "LaunchBox Big Box.exe",
)


def is_launchbox_process_running() -> bool:
    if sys.platform != "win32":
        return False

    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    for process_name in LAUNCHBOX_PROCESS_NAMES:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            continue

        output = result.stdout.strip()
        if output and "No tasks are running" not in output:
            return True

    return False


def is_file_locked(path: Path) -> bool:
    if not path.exists():
        return False

    if sys.platform == "win32":
        return _is_file_locked_windows(path)

    return _is_file_locked_posix(path)


def _is_file_locked_windows(path: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(
        str(path),
        GENERIC_READ,
        0,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        return error in (5, 32, 33)

    kernel32.CloseHandle(handle)
    return False


def _is_file_locked_posix(path: Path) -> bool:
    import fcntl

    try:
        with path.open("rb") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True

    return False


def find_locked_database_files(xml_paths: list[Path]) -> list[Path]:
    return [path for path in xml_paths if is_file_locked(path)]


def ensure_safe_to_mutate(xml_paths: list[Path]) -> None:
    if is_launchbox_process_running():
        raise MutationBlockedError(
            "LaunchBox is running. Close LaunchBox before modifying database files.",
            reason="launchbox_running",
        )

    locked_files = find_locked_database_files(xml_paths)
    if not locked_files:
        return

    if len(locked_files) == 1:
        raise MutationBlockedError(
            f"Database file is locked by another process: {locked_files[0]}",
            reason="files_locked",
            locked_files=locked_files,
        )

    paths = ", ".join(str(path) for path in locked_files)
    raise MutationBlockedError(
        f"Database files are locked by another process: {paths}",
        reason="files_locked",
        locked_files=locked_files,
    )

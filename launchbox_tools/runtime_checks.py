from __future__ import annotations

import csv
import io
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
        active_operation: str | None = None,
        active_run_id: str | None = None,
        active_pid: int | None = None,
        active_started_at: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.locked_files = locked_files or []
        self.active_operation = active_operation
        self.active_run_id = active_run_id
        self.active_pid = active_pid
        self.active_started_at = active_started_at
        self.details = details


class SafetyCheckError(RuntimeError):
    """Raised when mutation safety cannot be determined reliably."""


LAUNCHBOX_PROCESS_NAMES = (
    "LaunchBox.exe",
    "BigBox.exe",
)
PROCESS_CHECK_TIMEOUT_SECONDS = 5.0
_LAUNCHBOX_PROCESS_KEYS = frozenset(name.casefold() for name in LAUNCHBOX_PROCESS_NAMES)


def _parse_tasklist_process_names(output: str) -> set[str]:
    process_names: set[str] = set()
    try:
        rows = csv.reader(io.StringIO(output), strict=True)
        for row in rows:
            if not row or all(not value.strip() for value in row):
                continue
            if len(row) != 5:
                raise SafetyCheckError("tasklist returned malformed CSV output")

            image_name = row[0].strip().lstrip("\ufeff")
            pid = row[1].strip()
            if not image_name or not pid.isdecimal():
                raise SafetyCheckError("tasklist returned malformed process data")
            process_names.add(image_name)
    except csv.Error as exc:
        raise SafetyCheckError(f"tasklist returned invalid CSV: {exc}") from exc

    if not process_names:
        raise SafetyCheckError("tasklist returned no structured process data")
    return process_names


def _windows_process_names(*, timeout: float) -> set[str]:
    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=creationflags,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SafetyCheckError(
            f"tasklist timed out after {timeout:g} seconds"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise SafetyCheckError(f"tasklist could not be executed: {exc}") from exc

    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        suffix = f": {details}" if details else ""
        raise SafetyCheckError(
            f"tasklist failed with exit code {result.returncode}{suffix}"
        )
    return _parse_tasklist_process_names(result.stdout)


def is_launchbox_process_running(
    *,
    timeout: float = PROCESS_CHECK_TIMEOUT_SECONDS,
) -> bool:
    if sys.platform != "win32":
        return False

    process_names = _windows_process_names(timeout=timeout)
    return any(name.casefold() in _LAUNCHBOX_PROCESS_KEYS for name in process_names)


def is_file_locked(path: Path) -> bool:
    if sys.platform == "win32":
        return _is_file_locked_windows(path)

    if not path.exists():
        return False

    return _is_file_locked_posix(path)


def _is_file_locked_windows(path: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    ERROR_ACCESS_DENIED = 5
    ERROR_SHARING_VIOLATION = 32
    ERROR_LOCK_VIOLATION = 33
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = create_file(
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
        if error in (ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
            return True
        raise SafetyCheckError(
            f"CreateFileW could not inspect {path}: {ctypes.WinError(error)}"
        )

    ctypes.set_last_error(0)
    if not close_handle(handle):
        error = ctypes.get_last_error()
        raise SafetyCheckError(
            f"CloseHandle failed while inspecting {path}: {ctypes.WinError(error)}"
        )
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
    try:
        if is_launchbox_process_running():
            raise MutationBlockedError(
                "LaunchBox is running. Close LaunchBox before modifying database files.",
                reason="launchbox_running",
            )

        locked_files = find_locked_database_files(xml_paths)
    except SafetyCheckError as exc:
        raise MutationBlockedError(
            "LaunchBox safety checks could not be completed; database mutation was blocked. "
            f"Details: {exc}",
            reason="safety_check_failed",
            details=str(exc),
        ) from exc

    if not locked_files:
        return

    if len(locked_files) == 1:
        raise MutationBlockedError(
            f"Database file is locked by another process: {locked_files[0]}",
            reason="files_locked",
            locked_files=locked_files,
            details=str(locked_files[0]),
        )

    paths = ", ".join(str(path) for path in locked_files)
    raise MutationBlockedError(
        f"Database files are locked by another process: {paths}",
        reason="files_locked",
        locked_files=locked_files,
        details="\n".join(str(path) for path in locked_files),
    )

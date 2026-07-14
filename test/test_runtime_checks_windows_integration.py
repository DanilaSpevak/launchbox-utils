"""Windows integration coverage for the mutation safety boundary."""

import os
import shutil
import subprocess
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import launchbox_tools.safe_write as safe_write
from launchbox_tools.models import MutationOutcome, MutationState
from launchbox_tools.runtime_checks import (
    MutationBlockedError,
    SafetyCheckError,
    _is_file_locked_windows,
    _windows_oem_encoding,
    _windows_process_names,
    ensure_safe_to_mutate,
    is_file_locked,
    is_launchbox_process_running,
)
from launchbox_tools.safe_write import XmlMutation, execute_xml_transaction

from test.support import LaunchBoxTestCase


_HOLD_EXCLUSIVE_HANDLE_SCRIPT = r"""
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
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
invalid_handle = ctypes.c_void_p(-1).value
handle = create_file(str(path), 0x80000000, 0, None, 3, 0x80, None)
if handle == invalid_handle:
    raise ctypes.WinError(ctypes.get_last_error())
ready_path.write_text("ready", encoding="ascii")
try:
    time.sleep(60)
finally:
    close_handle(handle)
"""

_OEM_CSV_SCRIPT = r"""
import ctypes
import sys

encoding = f"cp{ctypes.WinDLL('kernel32').GetOEMCP()}"

for codepoint in range(0xA0, 0x10000):
    localized = chr(codepoint)
    if localized.isspace():
        continue
    try:
        encoded = localized.encode(encoding)
        decoded = encoded.decode(encoding, errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError):
        continue
    if decoded == localized and any(byte >= 0x80 for byte in encoded):
        break
else:
    raise RuntimeError(f"no non-ASCII sample is encodable with {encoding}")

row = f'"LaunchBox.exe","123","{localized}","1","12 345 K"\r\n'
sys.stdout.buffer.write(row.encode(encoding))
"""

_OEM_ERROR_SCRIPT = r"""
import ctypes
import sys

encoding = f"cp{ctypes.WinDLL('kernel32').GetOEMCP()}"

for codepoint in range(0xA0, 0x10000):
    localized = chr(codepoint)
    if localized.isspace():
        continue
    try:
        encoded = localized.encode(encoding)
        decoded = encoded.decode(encoding, errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError):
        continue
    if decoded == localized and any(byte >= 0x80 for byte in encoded):
        break
else:
    raise RuntimeError(f"no non-ASCII sample is encodable with {encoding}")

sys.stderr.buffer.write(f"Access denied {localized}".encode(encoding))
raise SystemExit(5)
"""


@unittest.skipUnless(sys.platform == "win32", "Windows safety integration tests")
class WindowsRuntimeChecksIntegrationTests(LaunchBoxTestCase):
    @staticmethod
    def _hidden_creation_flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

    @classmethod
    def _python_command(cls, script: str) -> list[str]:
        return [sys.executable, "-X", "utf8", "-c", script]

    @staticmethod
    def _localized_oem_sample(encoding: str) -> str:
        for codepoint in range(0xA0, 0x10000):
            candidate = chr(codepoint)
            if candidate.isspace():
                continue
            try:
                encoded = candidate.encode(encoding)
                decoded = encoded.decode(encoding, errors="strict")
            except (UnicodeDecodeError, UnicodeEncodeError):
                continue
            if decoded == candidate and any(byte >= 0x80 for byte in encoded):
                return candidate
        raise AssertionError(f"no non-ASCII sample is encodable with {encoding}")

    @classmethod
    def _terminate_process(cls, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @classmethod
    def _wait_for_marker(
        cls,
        marker: Path,
        process: subprocess.Popen,
        *,
        timeout: float = 10.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.is_file():
                return
            return_code = process.poll()
            if return_code is not None:
                raise AssertionError(
                    f"exclusive-handle helper exited early with code {return_code}"
                )
            time.sleep(0.02)
        raise AssertionError("exclusive-handle helper did not become ready")

    @classmethod
    @contextmanager
    def _exclusive_holder(cls, path: Path):
        marker = path.with_name(f".{path.name}.exclusive-ready")
        marker.unlink(missing_ok=True)
        process = subprocess.Popen(
            cls._python_command(_HOLD_EXCLUSIVE_HANDLE_SCRIPT)
            + [str(path), str(marker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=cls._hidden_creation_flags(),
        )
        try:
            cls._wait_for_marker(marker, process)
            yield
        finally:
            cls._terminate_process(process)
            marker.unlink(missing_ok=True)

    def test_exclusive_handle_from_another_process_blocks_mutation(self) -> None:
        with self.make_root() as temp_dir:
            destination = Path(temp_dir) / "Data" / "Platforms" / "NES.xml"
            destination.write_text("<LaunchBox />", encoding="utf-8")

            with self._exclusive_holder(destination):
                self.assertTrue(is_file_locked(destination))
                with patch(
                    "launchbox_tools.runtime_checks.is_launchbox_process_running",
                    return_value=False,
                ):
                    with self.assertRaises(MutationBlockedError) as context:
                        ensure_safe_to_mutate([destination])

            self.assertEqual(context.exception.reason, "files_locked")
            self.assertEqual(context.exception.locked_files, [destination])

    def test_real_access_denied_is_treated_as_locked(self) -> None:
        # CreateFileW without FILE_FLAG_BACKUP_SEMANTICS receives
        # ERROR_ACCESS_DENIED for a directory on Windows.
        self.assertTrue(_is_file_locked_windows(Path(__file__).resolve().parent))

    def test_oem_localized_tasklist_output_is_decoded(self) -> None:
        names = _windows_process_names(
            timeout=5,
            command=self._python_command(_OEM_CSV_SCRIPT),
        )

        self.assertEqual(names, {"LaunchBox.exe"})

    def test_oem_localized_tasklist_error_is_reported(self) -> None:
        encoding = _windows_oem_encoding()
        localized = self._localized_oem_sample(encoding)

        with self.assertRaises(SafetyCheckError) as context:
            _windows_process_names(
                timeout=5,
                command=self._python_command(_OEM_ERROR_SCRIPT),
            )

        self.assertIn("exit code 5", str(context.exception))
        self.assertIn("Access denied", str(context.exception))
        self.assertIn(localized, str(context.exception))

    def test_tasklist_timeout_from_real_subprocess_is_reported(self) -> None:
        with self.assertRaisesRegex(SafetyCheckError, "timed out"):
            _windows_process_names(
                timeout=0.1,
                command=self._python_command("import time; time.sleep(30)"),
            )

    def test_real_launchbox_and_bigbox_process_names_block_mutation(self) -> None:
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        ping_executable = system_root / "System32" / "ping.exe"
        if not ping_executable.is_file():
            self.skipTest(f"Windows ping executable not found: {ping_executable}")

        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            for process_name in ("LaunchBox.exe", "BigBox.exe"):
                with self.subTest(process_name=process_name):
                    executable = root / process_name
                    shutil.copy2(ping_executable, executable)
                    process = subprocess.Popen(
                        [str(executable), "127.0.0.1", "-t"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=self._hidden_creation_flags(),
                    )
                    try:
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            names = _windows_process_names(timeout=5)
                            if process_name in names:
                                break
                            if process.poll() is not None:
                                self.fail(
                                    f"{process_name} helper exited with code {process.returncode}"
                                )
                            time.sleep(0.05)
                        else:
                            self.fail(f"tasklist did not report {process_name}")

                        self.assertTrue(is_launchbox_process_running())
                        with self.assertRaises(MutationBlockedError) as context:
                            ensure_safe_to_mutate([])
                        self.assertEqual(context.exception.reason, "launchbox_running")
                    finally:
                        self._terminate_process(process)
                        executable.unlink(missing_ok=True)

    def test_file_lock_race_after_preliminary_check_stops_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "NES.xml"
            destination.write_text(
                "<LaunchBox><Value>old</Value></LaunchBox>",
                encoding="utf-8",
            )
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            real_validate = safe_write._validate_xml_file
            holder = None

            def validate_then_lock(path: Path, *, control=None) -> None:
                nonlocal holder
                real_validate(path, control=control)
                if holder is None:
                    holder = self._exclusive_holder(destination)
                    holder.__enter__()

            try:
                with patch(
                    "launchbox_tools.runtime_checks.is_launchbox_process_running",
                    return_value=False,
                ):
                    with patch.object(
                        safe_write,
                        "_validate_xml_file",
                        side_effect=validate_then_lock,
                    ):
                        transaction = execute_xml_transaction(
                            [XmlMutation(destination, tree)],
                            root / "Backups",
                        )
            finally:
                if holder is not None:
                    holder.__exit__(None, None, None)

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.blocked_reason, "files_locked")
            self.assertEqual(transaction.files[0].state, MutationState.PREPARED)
            self.assertEqual(
                ET.parse(destination).getroot().findtext("Value"),
                "old",
            )
            self.assertFalse(list(root.rglob("*.tmp")))

import ctypes
import subprocess
import sys
import unittest
from pathlib import Path
from ctypes import wintypes
from unittest.mock import Mock, patch
from launchbox_tools.runtime_checks import (
    LAUNCHBOX_PROCESS_NAMES,
    PROCESS_CHECK_TIMEOUT_SECONDS,
    TASKLIST_COMMAND,
    MutationBlockedError,
    SafetyCheckError,
    _is_file_locked_windows,
    _parse_tasklist_process_names,
    _windows_process_names,
    ensure_safe_to_mutate,
    is_file_locked,
    is_launchbox_process_running,
)
from launchbox_tools.xml_repository import load_platforms

from test.support import LaunchBoxTestCase


class RuntimeChecksTests(LaunchBoxTestCase):
    def test_launchbox_process_names_use_real_executable_names(self) -> None:
        self.assertEqual(LAUNCHBOX_PROCESS_NAMES, ("LaunchBox.exe", "BigBox.exe"))

    def test_tasklist_parser_uses_structured_rows_with_localized_fields(self) -> None:
        output = (
            '"LaunchBox.exe","123","Консоль","1","12 345 КБ"\r\n'
            '"Other.exe","456","Службы","0","1 024 КБ"\r\n'
        )

        self.assertEqual(
            _parse_tasklist_process_names(output),
            {"LaunchBox.exe", "Other.exe"},
        )

    def test_process_detection_is_case_insensitive_and_exact(self) -> None:
        with patch.object(sys, "platform", "win32"):
            with patch(
                "launchbox_tools.runtime_checks._windows_process_names",
                return_value={"bigbox.EXE", "NotLaunchBox.exe"},
            ):
                self.assertTrue(is_launchbox_process_running())

            with patch(
                "launchbox_tools.runtime_checks._windows_process_names",
                return_value={"LaunchBoxHelper.exe", "MyBigBox.exe"},
            ):
                self.assertFalse(is_launchbox_process_running())

    def test_windows_process_snapshot_uses_one_timeout_bound_tasklist_call(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tasklist"],
            0,
            stdout=b'"explorer.exe","123","Console","1","10,000 K"\r\n',
            stderr=b"",
        )
        with patch(
            "launchbox_tools.runtime_checks._windows_oem_encoding",
            return_value="utf-8",
        ):
            with patch(
                "launchbox_tools.runtime_checks.subprocess.run",
                return_value=completed,
            ) as run:
                names = _windows_process_names(timeout=PROCESS_CHECK_TIMEOUT_SECONDS)

        self.assertEqual(names, {"explorer.exe"})
        run.assert_called_once_with(
            list(TASKLIST_COMMAND),
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=PROCESS_CHECK_TIMEOUT_SECONDS,
        )

    def test_windows_process_snapshot_fails_closed_on_timeout(self) -> None:
        with patch(
            "launchbox_tools.runtime_checks.subprocess.run",
            side_effect=subprocess.TimeoutExpired("tasklist", 0.01),
        ):
            with self.assertRaisesRegex(SafetyCheckError, "timed out"):
                _windows_process_names(timeout=0.01)

    def test_windows_process_snapshot_fails_closed_on_command_error(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tasklist"],
            5,
            stdout=b"",
            stderr=b"Access denied",
        )
        with patch(
            "launchbox_tools.runtime_checks._windows_oem_encoding",
            return_value="utf-8",
        ):
            with patch("launchbox_tools.runtime_checks.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(SafetyCheckError, "exit code 5.*Access denied"):
                    _windows_process_names(timeout=PROCESS_CHECK_TIMEOUT_SECONDS)

    def test_windows_process_snapshot_fails_closed_when_tasklist_cannot_start(self) -> None:
        with patch(
            "launchbox_tools.runtime_checks.subprocess.run",
            side_effect=FileNotFoundError("tasklist missing"),
        ):
            with self.assertRaisesRegex(SafetyCheckError, "could not be executed"):
                _windows_process_names(timeout=PROCESS_CHECK_TIMEOUT_SECONDS)

    def test_windows_process_snapshot_fails_closed_on_decode_error(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tasklist"],
            0,
            stdout=b"\xff",
            stderr=b"",
        )
        with patch(
            "launchbox_tools.runtime_checks._windows_oem_encoding",
            return_value="utf-8",
        ):
            with patch(
                "launchbox_tools.runtime_checks.subprocess.run",
                return_value=completed,
            ):
                with self.assertRaisesRegex(SafetyCheckError, "could not be decoded"):
                    _windows_process_names(timeout=PROCESS_CHECK_TIMEOUT_SECONDS)

    def test_tasklist_parser_fails_closed_on_empty_or_malformed_output(self) -> None:
        for output in ("", "INFO: no matching tasks", '"broken","not-a-pid"'):
            with self.subTest(output=output):
                with self.assertRaises(SafetyCheckError):
                    _parse_tasklist_process_names(output)

    def test_ensure_safe_to_mutate_blocks_when_launchbox_running(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=True):
                with self.assertRaises(MutationBlockedError) as context:
                    ensure_safe_to_mutate([platform.database_xml])

            self.assertEqual(context.exception.reason, "launchbox_running")

    def test_ensure_safe_to_mutate_blocks_when_process_check_is_uncertain(self) -> None:
        with patch(
            "launchbox_tools.runtime_checks.is_launchbox_process_running",
            side_effect=SafetyCheckError("diagnostic denied"),
        ):
            with self.assertRaises(MutationBlockedError) as context:
                ensure_safe_to_mutate([Path("database.xml")])

        self.assertEqual(context.exception.reason, "safety_check_failed")
        self.assertIn("diagnostic denied", str(context.exception))

    def test_ensure_safe_to_mutate_blocks_when_file_locked(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.runtime_checks.is_file_locked", return_value=True):
                    with self.assertRaises(MutationBlockedError) as context:
                        ensure_safe_to_mutate([platform.database_xml])

            self.assertEqual(context.exception.reason, "files_locked")
            self.assertEqual(context.exception.locked_files, [platform.database_xml])
            self.assertEqual(context.exception.details, str(platform.database_xml))

    def test_is_file_locked_returns_false_for_unlocked_file(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            self.assertFalse(is_file_locked(platform.database_xml))

    def test_windows_file_check_does_not_treat_missing_path_as_unlocked(self) -> None:
        missing = Path("missing-database.xml")
        error = SafetyCheckError("CreateFileW reported missing target")
        with patch.object(sys, "platform", "win32"):
            with patch(
                "launchbox_tools.runtime_checks._is_file_locked_windows",
                side_effect=error,
            ) as windows_probe:
                with self.assertRaises(SafetyCheckError) as context:
                    is_file_locked(missing)

        self.assertIs(context.exception, error)
        windows_probe.assert_called_once_with(missing)

    def test_ensure_safe_to_mutate_blocks_when_file_check_is_uncertain(self) -> None:
        with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
            with patch(
                "launchbox_tools.runtime_checks.is_file_locked",
                side_effect=SafetyCheckError("CreateFileW failed"),
            ):
                with self.assertRaises(MutationBlockedError) as context:
                    ensure_safe_to_mutate([Path("database.xml")])

        self.assertEqual(context.exception.reason, "safety_check_failed")
        self.assertIn("CreateFileW failed", str(context.exception))

    @unittest.skipUnless(sys.platform == "win32", "Windows WinAPI signature test")
    def test_windows_file_probe_declares_signatures_and_closes_handle(self) -> None:
        create_file = Mock(return_value=123)
        close_handle = Mock(return_value=True)
        kernel32 = Mock(CreateFileW=create_file, CloseHandle=close_handle)

        with patch("ctypes.WinDLL", return_value=kernel32) as win_dll:
            self.assertFalse(_is_file_locked_windows(Path("database.xml")))

        win_dll.assert_called_once_with("kernel32", use_last_error=True)
        self.assertEqual(
            create_file.argtypes,
            (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ),
        )
        self.assertIs(create_file.restype, wintypes.HANDLE)
        self.assertEqual(close_handle.argtypes, (wintypes.HANDLE,))
        self.assertIs(close_handle.restype, wintypes.BOOL)
        close_handle.assert_called_once_with(123)

    @unittest.skipUnless(sys.platform == "win32", "Windows WinAPI error test")
    def test_windows_file_probe_treats_access_and_sharing_errors_as_locked(self) -> None:
        invalid_handle = ctypes.c_void_p(-1).value
        for error in (5, 32, 33):
            with self.subTest(error=error):
                kernel32 = Mock(
                    CreateFileW=Mock(return_value=invalid_handle),
                    CloseHandle=Mock(return_value=True),
                )
                with patch("ctypes.WinDLL", return_value=kernel32):
                    with patch("ctypes.get_last_error", return_value=error):
                        self.assertTrue(_is_file_locked_windows(Path("database.xml")))

                kernel32.CloseHandle.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows WinAPI error test")
    def test_windows_file_probe_fails_closed_on_unexpected_error(self) -> None:
        invalid_handle = ctypes.c_void_p(-1).value
        kernel32 = Mock(
            CreateFileW=Mock(return_value=invalid_handle),
            CloseHandle=Mock(return_value=True),
        )
        with patch("ctypes.WinDLL", return_value=kernel32):
            with patch("ctypes.get_last_error", return_value=2):
                with self.assertRaisesRegex(SafetyCheckError, "CreateFileW"):
                    _is_file_locked_windows(Path("database.xml"))

    @unittest.skipUnless(sys.platform == "win32", "Windows WinAPI error test")
    def test_windows_file_probe_fails_closed_when_handle_cannot_be_closed(self) -> None:
        kernel32 = Mock(
            CreateFileW=Mock(return_value=123),
            CloseHandle=Mock(return_value=False),
        )
        with patch("ctypes.WinDLL", return_value=kernel32):
            with patch("ctypes.get_last_error", return_value=6):
                with self.assertRaisesRegex(SafetyCheckError, "CloseHandle"):
                    _is_file_locked_windows(Path("database.xml"))

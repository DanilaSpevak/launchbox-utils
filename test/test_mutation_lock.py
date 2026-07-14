import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from launchbox_tools.models import MutationOutcome
from launchbox_tools.mutation_lock import mutation_run_lock
from launchbox_tools.operations.dedupe_additional_apps import run_additional_apps_dedupe
from launchbox_tools.paths import UnsafeDatabasePathError
from launchbox_tools.runtime_checks import MutationBlockedError

from test.support import LaunchBoxTestCase, create_directory_junction, remove_directory_junction


def _hold_mutation_lock(root: str, ready: Any, release: Any) -> None:
    with mutation_run_lock(Path(root), "replace_paths", "holder-run-id"):
        ready.set()
        if not release.wait(15):
            raise TimeoutError("Timed out waiting to release mutation lock")


class MutationLockTests(LaunchBoxTestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows junction integration test")
    def test_mutation_lock_rejects_data_junction_before_external_lock_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = Path(temp_dir)
            root = container / "LaunchBox"
            root.mkdir()
            external_data = container / "ExternalData"
            external_data.mkdir()
            data_dir = root / "Data"
            create_directory_junction(data_dir, external_data)
            try:
                with self.assertRaises(UnsafeDatabasePathError) as context:
                    with mutation_run_lock(root, "dedupe_additional_apps", "test-run"):
                        self.fail("Unsafe mutation lock was acquired")
            finally:
                remove_directory_junction(data_dir)

            self.assertEqual(context.exception.reason, "reparse_point")
            self.assertFalse((external_data / ".launchbox-utils-mutation.lock").exists())

    def test_parallel_apply_is_rejected_until_other_process_releases_lock(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(root, "")
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            original = xml_path.read_bytes()
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_mutation_lock,
                args=(str(root), ready, release),
            )
            process.start()

            try:
                self.assertTrue(ready.wait(10), "Child process did not acquire mutation lock")
                dry_run = run_additional_apps_dedupe(root, apply_changes=False)
                self.assertEqual(dry_run.outcome, MutationOutcome.DRY_RUN)
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    with self.assertRaises(MutationBlockedError) as raised:
                        run_additional_apps_dedupe(root, apply_changes=True)

                self.assertEqual(raised.exception.reason, "mutation_in_progress")
                self.assertEqual(raised.exception.active_operation, "replace_paths")
                self.assertEqual(raised.exception.active_run_id, "holder-run-id")
                self.assertIsNotNone(raised.exception.active_pid)
                self.assertEqual(xml_path.read_bytes(), original)
                self.assertFalse((root / "Data" / "Backups").exists())
            finally:
                release.set()
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)

            self.assertEqual(process.exitcode, 0)
            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                result = run_additional_apps_dedupe(root, apply_changes=True)

            self.assertEqual(result.outcome, MutationOutcome.SUCCESS)
            self.assertTrue(result.manifest_path.is_file())

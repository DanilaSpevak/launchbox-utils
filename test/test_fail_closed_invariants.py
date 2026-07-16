import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

from launchbox_tools.models import MutationOutcome, MutationState
from launchbox_tools.mutation_lock import LOCK_FILE_NAME, mutation_run_lock
from launchbox_tools.operation_lifecycle import OperationControl
from launchbox_tools.paths import UnsafeDatabasePathError
from launchbox_tools.runtime_checks import MutationBlockedError
from launchbox_tools.safe_write import XmlMutation, execute_xml_transaction

from test.support import LaunchBoxTestCase


class FailClosedInvariantTests(LaunchBoxTestCase):
    def assert_no_mutation_artifacts(self, root: Path) -> None:
        self.assertFalse((root / "Backups").exists())
        self.assertFalse(list(root.rglob(".launchbox-utils-work")))
        self.assertFalse(list(root.rglob("*.tmp")))

    def assert_planned_without_errors(self, transaction) -> None:
        self.assertTrue(transaction.files)
        self.assertEqual(
            [result.state for result in transaction.files],
            [MutationState.PLANNED] * len(transaction.files),
        )
        self.assertTrue(all(result.error is None for result in transaction.files))

    @staticmethod
    def changed_tree(destination: Path) -> ET.ElementTree:
        tree = ET.parse(destination)
        tree.getroot().find("Value").text = "new"
        return tree

    @staticmethod
    def make_trusted_xml(anchor: Path, name: str = "NES.xml") -> Path:
        destination = anchor / "Data" / "Platforms" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "<Root><Value>old</Value></Root>",
            encoding="utf-8",
        )
        return destination

    def test_duplicate_destination_fails_before_backup_or_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text(
                "<Root><Value>old</Value></Root>",
                encoding="utf-8",
            )
            original = destination.read_bytes()
            mutations = [
                XmlMutation(destination, self.changed_tree(destination)),
                XmlMutation(destination, self.changed_tree(destination)),
            ]

            with patch("launchbox_tools.safe_write._commit_staged_file") as commit:
                transaction = execute_xml_transaction(mutations, root / "Backups")

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(
                [result.state for result in transaction.files],
                [MutationState.PLANNED, MutationState.FAILED],
            )
            self.assertIsNone(transaction.files[0].error)
            self.assertIn("Duplicate XML transaction destination", transaction.files[1].error)
            self.assertEqual(transaction.error, transaction.files[1].error)
            self.assertEqual(destination.read_bytes(), original)
            commit.assert_not_called()
            self.assert_no_mutation_artifacts(root)

    def test_missing_destination_fails_before_backup_or_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            missing = root / "missing.xml"
            sentinel = root / "sentinel.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            with patch("launchbox_tools.safe_write._commit_staged_file") as commit:
                transaction = execute_xml_transaction(
                    [XmlMutation(missing, ET.ElementTree(ET.Element("Root")))],
                    root / "Backups",
                )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.FAILED)
            self.assertIn("destination not found", transaction.files[0].error)
            self.assertEqual(transaction.error, transaction.files[0].error)
            self.assertFalse(missing.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            commit.assert_not_called()
            self.assert_no_mutation_artifacts(root)

    def test_half_configured_trust_fails_before_file_processing(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "NES.xml"
            destination.write_text("<Root />", encoding="utf-8")
            original = destination.read_bytes()
            incomplete_configurations = (
                {"trusted_parent": destination.parent},
                {"trust_anchor": root},
            )

            for configuration in incomplete_configurations:
                with self.subTest(configuration=configuration):
                    with patch("launchbox_tools.safe_write._commit_staged_file") as commit:
                        transaction = execute_xml_transaction(
                            [XmlMutation(destination, ET.parse(destination), **configuration)],
                            root / "Backups",
                        )

                    self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
                    self.assertIn("must be provided together", transaction.error)
                    self.assert_planned_without_errors(transaction)
                    self.assertEqual(destination.read_bytes(), original)
                    commit.assert_not_called()
                    self.assert_no_mutation_artifacts(root)

    def test_different_trust_anchors_fail_before_file_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = Path(temp_dir)
            first_anchor = container / "FirstLaunchBox"
            second_anchor = container / "SecondLaunchBox"
            first = self.make_trusted_xml(first_anchor, "First.xml")
            second = self.make_trusted_xml(second_anchor, "Second.xml")
            originals = {first: first.read_bytes(), second: second.read_bytes()}

            mutations = [
                XmlMutation(
                    first,
                    self.changed_tree(first),
                    trusted_parent=first.parent,
                    trust_anchor=first_anchor,
                ),
                XmlMutation(
                    second,
                    self.changed_tree(second),
                    trusted_parent=second.parent,
                    trust_anchor=second_anchor,
                ),
            ]
            with patch("launchbox_tools.safe_write._commit_staged_file") as commit:
                transaction = execute_xml_transaction(mutations, container / "Backups")

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertIn("must share one trust anchor", transaction.error)
            self.assert_planned_without_errors(transaction)
            self.assertEqual(first.read_bytes(), originals[first])
            self.assertEqual(second.read_bytes(), originals[second])
            commit.assert_not_called()
            self.assert_no_mutation_artifacts(container)

    def test_cancelled_empty_transaction_has_no_side_effects(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            sentinel = root / "sentinel.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            control = OperationControl()
            self.assertTrue(control.request_cancel())

            with patch("launchbox_tools.safe_write._commit_staged_file") as commit:
                transaction = execute_xml_transaction(
                    [],
                    root / "Backups",
                    control=control,
                )

            self.assertEqual(transaction.outcome, MutationOutcome.CANCELLED)
            self.assertEqual(transaction.error, "Operation cancelled")
            self.assertEqual(transaction.files, [])
            self.assertFalse(control.snapshot().commit_started)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            commit.assert_not_called()
            self.assert_no_mutation_artifacts(root)

    def _assert_invalid_lock_owner_is_fail_closed(self, payload: bytes) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "Data" / LOCK_FILE_NAME
            original = b"\0" + payload
            lock_path.write_bytes(original)
            sentinel = root / "sentinel.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            with patch("launchbox_tools.mutation_lock._try_lock", return_value=False):
                with self.assertRaises(MutationBlockedError) as raised:
                    with mutation_run_lock(root, "replace_paths", "blocked-run"):
                        self.fail("A busy mutation lock must not be acquired")

            error = raised.exception
            self.assertEqual(error.reason, "mutation_in_progress")
            self.assertEqual(
                (
                    error.active_operation,
                    error.active_run_id,
                    error.active_pid,
                    error.active_started_at,
                ),
                (None, None, None, None),
            )
            self.assertEqual(
                str(error),
                "Another LaunchBox Utils mutation is already running.",
            )
            self.assertEqual(lock_path.read_bytes(), original)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assert_no_mutation_artifacts(root)

    def test_corrupted_lock_owner_is_ignored_without_rewriting_lock(self) -> None:
        self._assert_invalid_lock_owner_is_fail_closed(b"{not-json")

    def test_wrongly_typed_lock_owner_is_ignored_without_rewriting_lock(self) -> None:
        payload = json.dumps(
            {
                "operation": ["replace_paths"],
                "run_id": 42,
                "pid": True,
                "started_at": {"utc": "now"},
            }
        ).encode("utf-8")
        self._assert_invalid_lock_owner_is_fail_closed(payload)

    def test_valid_lock_owner_details_remain_available(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "Data" / LOCK_FILE_NAME
            owner = {
                "operation": "replace_paths",
                "run_id": "active-run",
                "pid": 1234,
                "started_at": "2026-07-16T12:00:00Z",
            }
            original = b"\0" + json.dumps(owner).encode("utf-8")
            lock_path.write_bytes(original)

            with patch("launchbox_tools.mutation_lock._try_lock", return_value=False):
                with self.assertRaises(MutationBlockedError) as raised:
                    with mutation_run_lock(root, "dedupe_additional_apps", "blocked-run"):
                        self.fail("A busy mutation lock must not be acquired")

            error = raised.exception
            self.assertEqual(error.reason, "mutation_in_progress")
            self.assertEqual(error.active_operation, owner["operation"])
            self.assertEqual(error.active_run_id, owner["run_id"])
            self.assertEqual(error.active_pid, owner["pid"])
            self.assertEqual(error.active_started_at, owner["started_at"])
            self.assertEqual(
                str(error),
                "Another LaunchBox Utils mutation is already running "
                "(operation=replace_paths, run_id=active-run, pid=1234).",
            )
            self.assertEqual(lock_path.read_bytes(), original)
            self.assert_no_mutation_artifacts(root)

    def _assert_path_guard_transaction_failure(
        self,
        anchor: Path,
        destination: Path,
        *,
        expected_reason: str,
        guard_patch,
    ) -> None:
        original = destination.read_bytes()
        external_sentinel = anchor.parent / "external-sentinel.txt"
        external_sentinel.write_text("preserve", encoding="utf-8")
        mutation = XmlMutation(
            destination,
            self.changed_tree(destination),
            trusted_parent=destination.parent,
            trust_anchor=anchor,
        )
        commit = Mock()

        with patch("launchbox_tools.safe_write._commit_staged_file", commit):
            with guard_patch:
                transaction = execute_xml_transaction(
                    [mutation],
                    anchor.parent / "Backups",
                )

        self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
        self.assertEqual(transaction.files[0].state, MutationState.FAILED)
        self.assertEqual(transaction.files[0].error, transaction.error)
        self.assertIsInstance(transaction.unsafe_path_error, UnsafeDatabasePathError)
        self.assertEqual(transaction.unsafe_path_error.reason, expected_reason)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(external_sentinel.read_text(encoding="utf-8"), "preserve")
        commit.assert_not_called()
        self.assert_no_mutation_artifacts(anchor.parent)

    def test_lstat_failure_is_structured_and_fails_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = Path(temp_dir)
            anchor = container / "LaunchBox"
            destination = self.make_trusted_xml(anchor)
            failing_path = destination.parent
            real_lstat = os.lstat

            def fail_platforms_metadata(path: Path):
                if Path(path) == failing_path:
                    raise OSError("metadata denied")
                return real_lstat(path)

            self._assert_path_guard_transaction_failure(
                anchor,
                destination,
                expected_reason="path_metadata_error",
                guard_patch=patch(
                    "launchbox_tools.paths.os.lstat",
                    side_effect=fail_platforms_metadata,
                ),
            )

    def test_resolve_failure_is_structured_and_fails_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = Path(temp_dir)
            anchor = container / "LaunchBox"
            destination = self.make_trusted_xml(anchor)
            path_type = type(anchor)
            real_resolve = path_type.resolve

            def fail_destination_resolve(path: Path, strict: bool = False) -> Path:
                if path == destination:
                    raise OSError("canonicalization denied")
                return real_resolve(path, strict=strict)

            self._assert_path_guard_transaction_failure(
                anchor,
                destination,
                expected_reason="path_metadata_error",
                guard_patch=patch.object(
                    path_type,
                    "resolve",
                    fail_destination_resolve,
                ),
            )

    def test_canonical_escape_fails_before_external_write_or_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = Path(temp_dir)
            anchor = container / "LaunchBox"
            destination = self.make_trusted_xml(anchor)
            external_destination = container / "External" / destination.name
            external_destination.parent.mkdir()
            external_destination.write_text("external", encoding="utf-8")
            path_type = type(anchor)
            real_resolve = path_type.resolve

            def escape_destination(path: Path, strict: bool = False) -> Path:
                if path == destination:
                    return external_destination
                return real_resolve(path, strict=strict)

            self._assert_path_guard_transaction_failure(
                anchor,
                destination,
                expected_reason="outside_trusted_directory",
                guard_patch=patch.object(path_type, "resolve", escape_destination),
            )
            self.assertEqual(external_destination.read_text(encoding="utf-8"), "external")


if __name__ == "__main__":
    unittest.main()

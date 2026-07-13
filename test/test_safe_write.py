import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from launchbox_tools.models import MutationOutcome, MutationState
from launchbox_tools.operation_lifecycle import OperationControl, OperationPhase
from launchbox_tools.safe_write import (
    XmlMutation,
    backup_xml_file,
    execute_xml_transaction,
    reserve_unique_backup_root,
    write_xml_tree_safely,
)
from launchbox_tools.xml_repository import child_text, parse_xml

from test.support import LaunchBoxTestCase


class SafeWriteTests(LaunchBoxTestCase):
    def test_xml_transaction_cancelled_after_stage_does_not_commit(self) -> None:
        class CancelAtCommitControl(OperationControl):
            def begin_commit(self) -> None:
                self.request_cancel()
                super().begin_commit()

        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            control = CancelAtCommitControl()

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                transaction = execute_xml_transaction(
                    [XmlMutation(destination, tree)],
                    root / "Backups",
                    control=control,
                )

            self.assertEqual(transaction.outcome, MutationOutcome.CANCELLED)
            self.assertEqual(transaction.files[0].state, MutationState.PREPARED)
            self.assertEqual(destination.read_text(encoding="utf-8"), "<Root><Value>old</Value></Root>")
            self.assertTrue(transaction.files[0].backup_path.is_file())
            self.assertFalse(list(root.rglob("*.tmp")))
            snapshot = control.snapshot()
            self.assertTrue(snapshot.cancel_requested)
            self.assertFalse(snapshot.commit_started)

    def test_backup_xml_file_never_overwrites_existing_backup(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            source = root / "database.xml"
            source.write_text("<Root><Value>new</Value></Root>", encoding="utf-8")
            backup_root = root / "Backups"
            backup_root.mkdir()
            existing = backup_root / source.name
            existing.write_text("preserve me", encoding="utf-8")

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with self.assertRaises(FileExistsError):
                    backup_xml_file(source, backup_root)

            self.assertEqual(existing.read_text(encoding="utf-8"), "preserve me")

    def test_reserve_unique_backup_root_rejects_parent_file(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            backup_parent = root / "backup-parent"
            backup_parent.write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(NotADirectoryError, "Backup parent is not a directory"):
                reserve_unique_backup_root(backup_parent, "run")

    def test_reserve_unique_backup_root_never_reuses_existing_directory(self) -> None:
        with self.make_root() as temp_dir:
            backup_parent = Path(temp_dir) / "Backups"
            existing = backup_parent / "run-id"
            existing.mkdir(parents=True)
            marker = existing / "marker.txt"
            marker.write_text("preserve me", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                reserve_unique_backup_root(backup_parent, "run-id")

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me")

    def test_write_xml_tree_safely_cleans_unique_temp_after_validation_failure(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"

            with patch("launchbox_tools.safe_write.ensure_safe_to_mutate"):
                with patch("launchbox_tools.safe_write.ET.parse", side_effect=ET.ParseError("invalid")):
                    with self.assertRaises(ET.ParseError):
                        write_xml_tree_safely(tree, destination)

            self.assertEqual(child_text(parse_xml(destination), "Value"), "old")
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_reserve_unique_backup_root_reraises_non_collision_file_exists_error(self) -> None:
        with self.make_root() as temp_dir:
            backup_parent = Path(temp_dir) / "Backups"
            backup_parent.mkdir()
            real_mkdir = Path.mkdir
            mkdir_calls = 0

            def fail_candidate(path: Path, *args: object, **kwargs: object) -> None:
                nonlocal mkdir_calls
                mkdir_calls += 1
                if path == backup_parent:
                    real_mkdir(path, *args, **kwargs)
                    return
                raise FileExistsError("not a candidate collision")

            with patch.object(Path, "mkdir", autospec=True, side_effect=fail_candidate):
                with self.assertRaisesRegex(FileExistsError, "not a candidate collision"):
                    reserve_unique_backup_root(backup_parent, "run")

            self.assertEqual(mkdir_calls, 2)

    def test_xml_transaction_rolls_back_already_committed_files(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.xml"
            second = root / "second.xml"
            first.write_text("<Root><Value>first</Value></Root>", encoding="utf-8")
            second.write_text("<Root><Value>second</Value></Root>", encoding="utf-8")
            originals = {first: first.read_bytes(), second: second.read_bytes()}
            first_tree = ET.parse(first)
            second_tree = ET.parse(second)
            first_tree.getroot().find("Value").text = "changed-first"
            second_tree.getroot().find("Value").text = "changed-second"
            commit_calls = 0
            control = OperationControl()

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=fail_second_commit):
                    transaction = execute_xml_transaction(
                        [XmlMutation(first, first_tree), XmlMutation(second, second_tree)],
                        root / "Backups",
                        control=control,
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.ROLLED_BACK, transaction)
            self.assertEqual(
                [result.state for result in transaction.files],
                [MutationState.ROLLED_BACK, MutationState.FAILED],
            )
            self.assertEqual(control.snapshot().phase, OperationPhase.ROLLBACK)
            self.assertFalse(control.request_cancel())
            self.assertEqual(first.read_bytes(), originals[first])
            self.assertEqual(second.read_bytes(), originals[second])
            self.assertEqual(len(transaction.backup_paths), 2)
            self.assertFalse(list(root.glob("*.stage.tmp")))
            self.assertFalse(list(root.glob("*.rollback.tmp")))

    def test_xml_transaction_reports_rollback_failure(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.xml"
            second = root / "second.xml"
            first.write_text("<Root><Value>first</Value></Root>", encoding="utf-8")
            second.write_text("<Root><Value>second</Value></Root>", encoding="utf-8")
            first_tree = ET.parse(first)
            second_tree = ET.parse(second)
            first_tree.getroot().find("Value").text = "changed-first"
            second_tree.getroot().find("Value").text = "changed-second"
            commit_calls = 0

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated commit failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=fail_second_commit):
                    with patch("launchbox_tools.safe_write._restore_backup", side_effect=OSError("restore denied")):
                        transaction = execute_xml_transaction(
                            [XmlMutation(first, first_tree), XmlMutation(second, second_tree)],
                            root / "Backups",
                        )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.FAILED)
            self.assertEqual(transaction.files[1].state, MutationState.FAILED)
            self.assertEqual(len(transaction.rollback_errors), 1, transaction)
            self.assertIn(str(first), transaction.rollback_errors[0])
            self.assertTrue(all(path.exists() for path in transaction.backup_paths.values()))

    def test_xml_transaction_rolls_back_same_named_files_from_distinct_backups(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            first = root / "a" / "db.xml"
            second = root / "b" / "db.xml"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("<Root><Value>first</Value></Root>", encoding="utf-8")
            second.write_text("<Root><Value>second</Value></Root>", encoding="utf-8")
            originals = {first: first.read_bytes(), second: second.read_bytes()}
            first_tree = ET.parse(first)
            second_tree = ET.parse(second)
            first_tree.getroot().find("Value").text = "changed-first"
            second_tree.getroot().find("Value").text = "changed-second"
            commit_calls = 0

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=fail_second_commit):
                    transaction = execute_xml_transaction(
                        [XmlMutation(first, first_tree), XmlMutation(second, second_tree)],
                        root / "Backups",
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.ROLLED_BACK, transaction)
            self.assertEqual(first.read_bytes(), originals[first])
            self.assertEqual(second.read_bytes(), originals[second])
            self.assertEqual(len(transaction.backup_paths), 2)
            self.assertEqual(len(set(transaction.backup_paths.values())), 2)
            self.assertEqual(
                {backup_path.parent.name for backup_path in transaction.backup_paths.values()},
                {"0001", "0002"},
            )
            for destination, backup_path in transaction.backup_paths.items():
                self.assertEqual(backup_path.read_bytes(), originals[destination])
            self.assertFalse(list(root.rglob("*.stage.tmp")))
            self.assertFalse(list(root.rglob("*.rollback.tmp")))

    def test_xml_transaction_validation_failure_writes_nothing(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root />", encoding="utf-8")
            original = destination.read_bytes()
            invalid_tree = ET.ElementTree(ET.Element(None))
            backup_root = root / "Backups"

            transaction = execute_xml_transaction([XmlMutation(destination, invalid_tree)], backup_root)

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(backup_root.exists())

    def test_xml_transaction_backup_failure_does_not_stage_or_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            original = destination.read_bytes()
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write.backup_xml_file", side_effect=OSError("backup denied")):
                    transaction = execute_xml_transaction(
                        [XmlMutation(destination, tree)], root / "Backups"
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_xml_transaction_aborts_before_stage_when_backup_hash_differs(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            original = destination.read_bytes()
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write.sha256_file", side_effect=["source", "backup"]):
                    transaction = execute_xml_transaction(
                        [XmlMutation(destination, tree)],
                        root / "Backups",
                        "test-run-id",
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertIn("Backup verification failed", transaction.error)
            self.assertEqual(destination.read_bytes(), original)
            self.assertTrue(transaction.files[0].backup_path.is_file())
            self.assertIsNone(transaction.files[0].source_sha256)
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_xml_transaction_stage_failure_does_not_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            original = destination.read_bytes()
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("pathlib.Path.write_bytes", side_effect=OSError("stage denied")):
                    transaction = execute_xml_transaction(
                        [XmlMutation(destination, tree)], root / "Backups"
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.FAILED)
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(len(transaction.backup_paths), 1)
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_xml_transaction_keeps_prepared_state_when_precommit_check_fails(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"

            with patch(
                "launchbox_tools.safe_write.ensure_safe_to_mutate",
                side_effect=[None, None, OSError("precommit denied")],
            ):
                transaction = execute_xml_transaction(
                    [XmlMutation(destination, tree)], root / "Backups"
                )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.PREPARED)
            self.assertEqual(child_text(parse_xml(destination), "Value"), "old")
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_xml_transaction_keeps_unattempted_files_prepared_after_late_commit_failure(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            paths = [root / f"database-{index}.xml" for index in range(3)]
            trees = []
            for index, path in enumerate(paths):
                path.write_text(f"<Root><Value>{index}</Value></Root>", encoding="utf-8")
                tree = ET.parse(path)
                tree.getroot().find("Value").text = f"changed-{index}"
                trees.append(tree)
            commit_calls = 0

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=fail_second_commit):
                    transaction = execute_xml_transaction(
                        [XmlMutation(path, tree) for path, tree in zip(paths, trees)],
                        root / "Backups",
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.ROLLED_BACK)
            self.assertEqual(
                [file.state for file in transaction.files],
                [MutationState.ROLLED_BACK, MutationState.FAILED, MutationState.PREPARED],
            )
            self.assertFalse(list(root.rglob("*.tmp")))

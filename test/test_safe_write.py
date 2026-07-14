import io
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
import launchbox_tools.safe_write as safe_write
from launchbox_tools.models import MutationOutcome, MutationState
from launchbox_tools.operation_lifecycle import OperationCancelled, OperationControl, OperationPhase
from launchbox_tools.paths import UnsafeDatabasePathError
from launchbox_tools.runtime_checks import MutationBlockedError
from launchbox_tools.safe_write import (
    _MAX_XML_NAME_CHARACTERS,
    XmlMutation,
    _serialize_xml_tree,
    _sort_namespaces_with_checkpoints,
    _validate_xml_file,
    _validate_xml_payload,
    _write_bytes_with_checkpoints,
    backup_xml_file,
    execute_xml_transaction,
    reserve_unique_backup_root,
    sha256_file,
    write_xml_tree_safely,
)
from launchbox_tools.xml_repository import child_text, parse_xml

from test.support import (
    CancelAfterCheckpoints,
    LaunchBoxTestCase,
    create_directory_junction,
    remove_directory_junction,
)


class SafeWriteTests(LaunchBoxTestCase):
    def test_cancellable_xml_serialization_matches_standard_bytes(self) -> None:
        root = ET.Element("Root")
        ET.SubElement(root, "Value", {"kind": "special"}).text = "A & B < C — Привет"
        tree = ET.ElementTree(root)
        expected = io.BytesIO()
        tree.write(expected, encoding="utf-8", xml_declaration=True)

        actual = _serialize_xml_tree(tree, control=OperationControl())

        self.assertEqual(actual, expected.getvalue())

    def test_cancellable_xml_serialization_matches_complex_standard_bytes(self) -> None:
        namespace_map = getattr(ET, "_namespace_map")
        original_namespace_map = namespace_map.copy()

        def restore_namespace_map() -> None:
            namespace_map.clear()
            namespace_map.update(original_namespace_map)

        self.addCleanup(restore_namespace_map)
        ET.register_namespace("sample", "urn:sample")
        root = ET.Element("{urn:sample}Root", {"plain": 'A & B\n"quoted"'})
        root.text = "before < child"
        root.append(ET.Comment("comment"))
        root[-1].tail = "comment-tail"
        root.append(ET.ProcessingInstruction("target", "value"))
        root[-1].tail = "pi-tail"
        child = ET.SubElement(
            root,
            "{urn:sample}Child",
            {
                ET.QName("urn:sample", "kind"): "x",
                "reference": ET.QName("urn:sample", "value"),
                "none-reference": ET.QName(None),
            },
        )
        child.text = "Привет & goodbye"
        child.tail = "after"
        ET.SubElement(root, "Empty")
        tree = ET.ElementTree(root)
        expected = io.BytesIO()
        tree.write(expected, encoding="utf-8", xml_declaration=True)

        actual = _serialize_xml_tree(tree, control=OperationControl())

        self.assertEqual(actual, expected.getvalue())

    def test_cancellable_xml_serialization_rejects_unbounded_qname(self) -> None:
        root = ET.Element("x" * (_MAX_XML_NAME_CHARACTERS + 1))

        with self.assertRaisesRegex(ValueError, "qualified name exceeds"):
            _serialize_xml_tree(ET.ElementTree(root), control=OperationControl())

    def test_cancellable_xml_serialization_preserves_invalid_qname_bytes(self) -> None:
        root = ET.Element(
            "Root",
            {"reference": ET.QName("urn:sample", "invalid&value")},
        )
        tree = ET.ElementTree(root)
        expected = io.BytesIO()
        tree.write(expected, encoding="utf-8", xml_declaration=True)

        actual = _serialize_xml_tree(tree, control=OperationControl())

        self.assertEqual(actual, expected.getvalue())
        with self.assertRaises(ET.ParseError):
            _validate_xml_payload(actual, control=OperationControl())

    def test_namespace_sort_matches_standard_order_and_is_cancellable(self) -> None:
        namespaces = {
            f"urn:namespace:{index}": f"prefix-{300 - index:04d}"
            for index in range(300)
        }
        expected = sorted(namespaces.items(), key=lambda item: item[1])

        actual = _sort_namespaces_with_checkpoints(namespaces, OperationControl())

        self.assertEqual(actual, expected)
        with self.assertRaises(OperationCancelled):
            _sort_namespaces_with_checkpoints(
                namespaces,
                CancelAfterCheckpoints(2),
            )

    def test_xml_serialization_checks_cancellation_inside_one_large_text_node(self) -> None:
        root = ET.Element("Root")
        root.text = "x" * (3 * 1024 * 1024)
        control = OperationControl()
        escape_calls = 0

        from launchbox_tools import safe_write as safe_write_module

        real_escape = safe_write_module._escape_cdata_chunk

        def cancel_on_second_chunk(text: str) -> str:
            nonlocal escape_calls
            escape_calls += 1
            if escape_calls == 2:
                control.request_cancel()
            return real_escape(text)

        with patch.object(
            safe_write_module,
            "_escape_cdata_chunk",
            side_effect=cancel_on_second_chunk,
        ):
            with self.assertRaises(OperationCancelled):
                _serialize_xml_tree(ET.ElementTree(root), control=control)

        self.assertEqual(escape_calls, 2)

    def test_xml_serialization_checks_cancellation_inside_one_large_attribute(self) -> None:
        root = ET.Element("Root", {"large": "&" * (3 * 1024 * 1024)})
        control = OperationControl()
        escape_calls = 0

        from launchbox_tools import safe_write as safe_write_module

        real_escape = safe_write_module._escape_attribute_chunk

        def cancel_on_second_chunk(text: str) -> str:
            nonlocal escape_calls
            escape_calls += 1
            if escape_calls == 2:
                control.request_cancel()
            return real_escape(text)

        with patch.object(
            safe_write_module,
            "_escape_attribute_chunk",
            side_effect=cancel_on_second_chunk,
        ):
            with self.assertRaises(OperationCancelled):
                _serialize_xml_tree(ET.ElementTree(root), control=control)

        self.assertEqual(escape_calls, 2)

    def test_xml_serialization_checks_cancellation_during_large_tree(self) -> None:
        root = ET.Element("Root")
        for index in range(300):
            ET.SubElement(root, "Item").text = str(index)
        control = CancelAfterCheckpoints(2)

        with self.assertRaises(OperationCancelled):
            _serialize_xml_tree(ET.ElementTree(root), control=control)

        self.assertGreaterEqual(control.checkpoint_calls, 2)

    def test_payload_validation_checks_cancellation_during_large_xml(self) -> None:
        payload = (
            "<Root>" + "".join(f"<Item>{index}</Item>" for index in range(300)) + "</Root>"
        ).encode("utf-8")
        control = CancelAfterCheckpoints(3)

        with self.assertRaises(OperationCancelled):
            _validate_xml_payload(payload, control=control)

        self.assertGreaterEqual(control.checkpoint_calls, 3)

    def test_payload_validation_checks_cancellation_inside_one_large_text_node(self) -> None:
        payload = b"<Root>" + b"x" * (3 * 1024 * 1024) + b"</Root>"
        control = CancelAfterCheckpoints(3)

        with self.assertRaises(OperationCancelled):
            _validate_xml_payload(payload, control=control)

        self.assertGreaterEqual(control.checkpoint_calls, 3)

    def test_payload_validation_cancellation_happens_before_backup(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            original = destination.read_bytes()
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            backup_root = root / "Backups"
            control = OperationControl()
            real_validate = _validate_xml_payload

            def cancel_validation(payload: bytes, *, control=None) -> None:
                control.request_cancel()
                real_validate(payload, control=control)

            with patch(
                "launchbox_tools.safe_write._validate_xml_payload",
                side_effect=cancel_validation,
            ):
                transaction = execute_xml_transaction(
                    [XmlMutation(destination, tree)],
                    backup_root,
                    control=control,
                )

            self.assertEqual(transaction.outcome, MutationOutcome.CANCELLED)
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(backup_root.exists())
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_malformed_serialized_payload_fails_before_backup(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root />", encoding="utf-8")
            original = destination.read_bytes()
            backup_root = root / "Backups"

            with patch(
                "launchbox_tools.safe_write._serialize_xml_tree",
                return_value=b"<Root>",
            ):
                transaction = execute_xml_transaction(
                    [XmlMutation(destination, ET.ElementTree(ET.Element("Root")))],
                    backup_root,
                )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(backup_root.exists())

    def test_sha256_checks_cancellation_between_io_chunks(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            source = root / "large.bin"
            source.write_bytes(b"x" * (2 * 1024 * 1024))
            control = CancelAfterCheckpoints(2)

            with self.assertRaises(OperationCancelled):
                sha256_file(source, control=control)

            self.assertGreaterEqual(control.checkpoint_calls, 2)

    def test_backup_cancellation_removes_incomplete_copy(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            source = root / "database.xml"
            source.write_bytes(b"x" * (2 * 1024 * 1024))
            backup_root = root / "Backups"
            control = CancelAfterCheckpoints(2)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with self.assertRaises(OperationCancelled):
                    backup_xml_file(source, backup_root, control=control)

            self.assertFalse((backup_root / source.name).exists())

    def test_stage_validation_checks_cancellation_during_large_xml(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            xml_path = root / "large.xml"
            xml_path.write_text(
                "<Root>" + "".join(f"<Item>{index}</Item>" for index in range(300)) + "</Root>",
                encoding="utf-8",
            )
            control = CancelAfterCheckpoints(2)

            with self.assertRaises(OperationCancelled):
                _validate_xml_file(xml_path, control=control)

            self.assertGreaterEqual(control.checkpoint_calls, 2)

    def test_stage_validation_checks_cancellation_inside_one_large_text_node(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "large-text.xml"
            xml_path.write_bytes(b"<Root>" + b"x" * (3 * 1024 * 1024) + b"</Root>")
            control = CancelAfterCheckpoints(4)

            with self.assertRaises(OperationCancelled):
                _validate_xml_file(xml_path, control=control)

            self.assertGreaterEqual(control.checkpoint_calls, 4)

    def test_transaction_cancellation_during_stage_write_cleans_temp(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            original = destination.read_bytes()
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            control = OperationControl()

            def cancel_stage_write(
                path: Path,
                payload: bytes,
                *,
                control=None,
                exclusive=False,
            ) -> None:
                control.request_cancel()
                _write_bytes_with_checkpoints(
                    path,
                    payload,
                    control=control,
                    exclusive=exclusive,
                )

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.safe_write._write_bytes_with_checkpoints",
                    side_effect=cancel_stage_write,
                ):
                    transaction = execute_xml_transaction(
                        [XmlMutation(destination, tree)],
                        root / "Backups",
                        control=control,
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.CANCELLED)
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(control.snapshot().commit_started)
            self.assertFalse(list(root.rglob("*.tmp")))

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

    @unittest.skipUnless(sys.platform == "win32", "Windows junction integration test")
    def test_reserve_unique_backup_root_rejects_backups_junction(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            backup_parent = root / "Data" / "Backups"
            external_dir = root / "ExternalBackups"
            external_dir.mkdir()
            create_directory_junction(backup_parent, external_dir)
            try:
                with self.assertRaises(UnsafeDatabasePathError):
                    reserve_unique_backup_root(
                        backup_parent,
                        "run",
                        trusted_parent=root / "Data",
                        trust_anchor=root,
                    )
                self.assertFalse((external_dir / "run").exists())
            finally:
                remove_directory_junction(backup_parent)

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

    def test_write_xml_tree_safely_streams_without_materializing_payload(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"

            with patch("launchbox_tools.safe_write.ensure_safe_to_mutate"):
                with patch(
                    "launchbox_tools.safe_write._serialize_xml_tree",
                    side_effect=AssertionError("payload serialization must not be used"),
                ):
                    write_xml_tree_safely(tree, destination)

            self.assertEqual(child_text(parse_xml(destination), "Value"), "new")
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

    def test_trusted_xml_transaction_rolls_back_in_workspace_and_cleans_it(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_dir = root / "Data" / "Platforms"
            first = platforms_dir / "First.xml"
            second = platforms_dir / "Second.xml"
            first.write_text("<Root><Value>first</Value></Root>", encoding="utf-8")
            second.write_text("<Root><Value>second</Value></Root>", encoding="utf-8")
            originals = {first: first.read_bytes(), second: second.read_bytes()}
            trees = [ET.parse(first), ET.parse(second)]
            trees[0].getroot().find("Value").text = "changed-first"
            trees[1].getroot().find("Value").text = "changed-second"
            commit_calls = 0

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            mutations = [
                XmlMutation(path, tree, trusted_parent=platforms_dir, trust_anchor=root)
                for path, tree in zip((first, second), trees)
            ]
            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch.object(safe_write, "_commit_staged_file", side_effect=fail_second_commit):
                    transaction = execute_xml_transaction(
                        mutations,
                        root / "Data" / "Backups" / "run",
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.ROLLED_BACK, transaction)
            self.assertEqual(first.read_bytes(), originals[first])
            self.assertEqual(second.read_bytes(), originals[second])
            self.assertFalse((root / ".launchbox-utils-work").exists())

    def test_trusted_xml_transaction_accounts_for_workspace_guard_failure_during_rollback(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_dir = root / "Data" / "Platforms"
            first = platforms_dir / "First.xml"
            second = platforms_dir / "Second.xml"
            first.write_text("<Root><Value>first</Value></Root>", encoding="utf-8")
            second.write_text("<Root><Value>second</Value></Root>", encoding="utf-8")
            trees = [ET.parse(first), ET.parse(second)]
            commit_calls = 0
            real_workspace_path = safe_write._workspace_path

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            def reject_rollback(*args, **kwargs) -> Path:
                if args[-1] == "rollback":
                    raise UnsafeDatabasePathError(
                        "workspace changed before rollback",
                        reason="reparse_point",
                    )
                return real_workspace_path(*args, **kwargs)

            mutations = [
                XmlMutation(path, tree, trusted_parent=platforms_dir, trust_anchor=root)
                for path, tree in zip((first, second), trees)
            ]
            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch.object(safe_write, "_commit_staged_file", side_effect=fail_second_commit):
                    with patch.object(safe_write, "_workspace_path", side_effect=reject_rollback):
                        transaction = execute_xml_transaction(
                            mutations,
                            root / "Data" / "Backups" / "run",
                        )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.FAILED)
            self.assertIn("workspace changed before rollback", transaction.files[0].rollback_error)
            self.assertIsInstance(transaction.unsafe_path_error, UnsafeDatabasePathError)
            self.assertEqual(len(transaction.rollback_errors), 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows symlink integration test")
    def test_trusted_xml_transaction_never_overwrites_precreated_rollback_symlink(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_dir = root / "Data" / "Platforms"
            first = platforms_dir / "First.xml"
            second = platforms_dir / "Second.xml"
            first.write_text("<Root><Value>first</Value></Root>", encoding="utf-8")
            second.write_text("<Root><Value>second</Value></Root>", encoding="utf-8")
            trees = [ET.parse(first), ET.parse(second)]
            sentinel = root / "rollback-sentinel.xml"
            sentinel.write_text("rollback-sentinel", encoding="utf-8")
            commit_calls = 0
            rollback_path: Path | None = None
            real_workspace_path = safe_write._workspace_path

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            def precreate_rollback(*args, **kwargs) -> Path:
                nonlocal rollback_path
                path = real_workspace_path(*args, **kwargs)
                if args[-1] == "rollback":
                    rollback_path = path
                    try:
                        os.symlink(sentinel, path)
                    except OSError as exc:
                        self.skipTest(f"Windows file symlink is unavailable: {exc}")
                return path

            mutations = [
                XmlMutation(path, tree, trusted_parent=platforms_dir, trust_anchor=root)
                for path, tree in zip((first, second), trees)
            ]
            try:
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    with patch.object(safe_write, "_commit_staged_file", side_effect=fail_second_commit):
                        with patch.object(safe_write, "_workspace_path", side_effect=precreate_rollback):
                            transaction = execute_xml_transaction(
                                mutations,
                                root / "Data" / "Backups" / "run",
                            )

                self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
                self.assertEqual(transaction.files[0].state, MutationState.FAILED)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "rollback-sentinel")
            finally:
                if rollback_path is not None and rollback_path.exists():
                    rollback_path.unlink()
                    workspace_root = rollback_path.parent
                    if workspace_root.exists():
                        workspace_root.rmdir()
                    workspace_parent = root / ".launchbox-utils-work"
                    if workspace_parent.exists():
                        workspace_parent.rmdir()

    @unittest.skipUnless(sys.platform == "win32", "Windows junction integration test")
    def test_trusted_xml_transaction_rejects_backup_junction_before_rollback_read(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_dir = root / "Data" / "Platforms"
            first = platforms_dir / "First.xml"
            second = platforms_dir / "Second.xml"
            first.write_text("<Root><Value>first</Value></Root>", encoding="utf-8")
            second.write_text("<Root><Value>second</Value></Root>", encoding="utf-8")
            trees = [ET.parse(first), ET.parse(second)]
            trees[0].getroot().find("Value").text = "changed-first"
            trees[1].getroot().find("Value").text = "changed-second"
            backup_run = root / "Data" / "Backups" / "run"
            external_dir = root / "ExternalBackup"
            external_dir.mkdir()
            external_backup = external_dir / first.name
            external_backup.write_text(
                "<Root><Value>attacker</Value></Root>",
                encoding="utf-8",
            )
            numbered_backup = backup_run / "0001"
            saved_backup = backup_run / "0001-saved"
            commit_calls = 0
            swapped = False

            def swap_backup_then_fail(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls, swapped
                commit_calls += 1
                if commit_calls == 2:
                    numbered_backup.rename(saved_backup)
                    create_directory_junction(numbered_backup, external_dir)
                    swapped = True
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            mutations = [
                XmlMutation(path, tree, trusted_parent=platforms_dir, trust_anchor=root)
                for path, tree in zip((first, second), trees)
            ]
            try:
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    with patch.object(
                        safe_write,
                        "_commit_staged_file",
                        side_effect=swap_backup_then_fail,
                    ):
                        transaction = execute_xml_transaction(mutations, backup_run)
            finally:
                if swapped:
                    remove_directory_junction(numbered_backup)
                    saved_backup.rename(numbered_backup)

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.FAILED)
            self.assertIsInstance(transaction.unsafe_path_error, UnsafeDatabasePathError)
            self.assertEqual(child_text(parse_xml(first), "Value"), "changed-first")
            self.assertEqual(external_backup.read_text(encoding="utf-8"), "<Root><Value>attacker</Value></Root>")

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

    def test_xml_transaction_rejects_mixed_trust_configuration_before_backup(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            trusted = root / "Data" / "Platforms" / "NES.xml"
            untrusted = root / "other.xml"
            trusted.write_text("<Root />", encoding="utf-8")
            untrusted.write_text("<Root />", encoding="utf-8")
            backup_root = root / "Backups"

            transaction = execute_xml_transaction(
                [
                    XmlMutation(
                        trusted,
                        ET.parse(trusted),
                        trusted_parent=trusted.parent,
                        trust_anchor=root,
                    ),
                    XmlMutation(untrusted, ET.parse(untrusted)),
                ],
                backup_root,
            )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertIn("cannot share a transaction", transaction.error)
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
                with patch(
                    "launchbox_tools.safe_write._write_bytes_with_checkpoints",
                    side_effect=OSError("stage denied"),
                ):
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

    def test_xml_transaction_does_not_recreate_target_missing_at_final_check(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            safety_calls = 0

            def remove_before_final_check(_paths: list[Path]) -> None:
                nonlocal safety_calls
                safety_calls += 1
                if safety_calls == 3:
                    destination.unlink()
                    raise MutationBlockedError(
                        "target disappeared",
                        reason="safety_check_failed",
                        details="CreateFileW reported a missing target",
                    )

            with patch(
                "launchbox_tools.safe_write.ensure_safe_to_mutate",
                side_effect=remove_before_final_check,
            ):
                with patch("launchbox_tools.safe_write._commit_staged_file") as commit:
                    transaction = execute_xml_transaction(
                        [XmlMutation(destination, tree)],
                        root / "Backups",
                    )

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.PREPARED)
            self.assertEqual(transaction.blocked_reason, "safety_check_failed")
            self.assertEqual(
                transaction.blocked_details,
                "CreateFileW reported a missing target",
            )
            self.assertFalse(destination.exists())
            commit.assert_not_called()
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_xml_transaction_runs_final_safety_check_directly_before_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "database.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            control = OperationControl()
            events: list[str] = []
            real_checkpoint = control.checkpoint
            real_begin_commit = control.begin_commit
            real_commit = safe_write._commit_staged_file

            def record_safety(_paths: list[Path]) -> None:
                events.append("safety")

            def record_begin_commit() -> None:
                events.append("begin_commit")
                real_begin_commit()

            def record_checkpoint() -> None:
                events.append("checkpoint")
                real_checkpoint()

            def record_commit(stage_path: Path, target: Path) -> None:
                events.append("commit")
                real_commit(stage_path, target)

            with patch(
                "launchbox_tools.safe_write.ensure_safe_to_mutate",
                side_effect=record_safety,
            ):
                with patch.object(
                    control,
                    "checkpoint",
                    side_effect=record_checkpoint,
                ):
                    with patch.object(
                        control,
                        "begin_commit",
                        side_effect=record_begin_commit,
                    ):
                        with patch(
                            "launchbox_tools.safe_write._commit_staged_file",
                            side_effect=record_commit,
                        ):
                            transaction = execute_xml_transaction(
                                [XmlMutation(destination, tree)],
                                root / "Backups",
                                control=control,
                            )

            self.assertEqual(transaction.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(
                events[-4:],
                ["checkpoint", "safety", "begin_commit", "commit"],
            )
            self.assertEqual(child_text(parse_xml(destination), "Value"), "new")

    def test_xml_transaction_rechecks_trusted_path_immediately_before_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "NES.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            original = destination.read_bytes()
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            stage_validated = False
            real_validate = safe_write._validate_xml_file

            def validate_stage(path: Path, *, control=None) -> None:
                nonlocal stage_validated
                real_validate(path, control=control)
                stage_validated = True

            def fail_precommit(_anchor, _parent, path, **_kwargs) -> None:
                if stage_validated and path == destination:
                    raise UnsafeDatabasePathError(
                        "path changed after stage",
                        reason="reparse_point",
                        path=destination.parent,
                    )

            mutation = XmlMutation(
                destination,
                tree,
                trusted_parent=root / "Data" / "Platforms",
                trust_anchor=root,
            )
            with patch(
                "launchbox_tools.safe_write.ensure_trusted_direct_child",
                side_effect=fail_precommit,
            ):
                with patch.object(safe_write, "_validate_xml_file", side_effect=validate_stage):
                    with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                        with patch("launchbox_tools.safe_write._commit_staged_file") as commit:
                            transaction = execute_xml_transaction([mutation], root / "Backups")

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.PREPARED)
            self.assertIsNotNone(transaction.unsafe_path_error)
            self.assertIn("path changed after stage", transaction.error)
            self.assertEqual(destination.read_bytes(), original)
            commit.assert_not_called()
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_xml_transaction_checks_trusted_path_before_each_commit(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "NES.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            mutation = XmlMutation(
                destination,
                tree,
                trusted_parent=root / "Data" / "Platforms",
                trust_anchor=root,
            )

            with patch(
                "launchbox_tools.safe_write.ensure_trusted_direct_child"
            ) as trusted_guard:
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    transaction = execute_xml_transaction([mutation], root / "Backups")

            self.assertEqual(transaction.outcome, MutationOutcome.SUCCESS)
            self.assertGreaterEqual(trusted_guard.call_count, 9)
            self.assertEqual(child_text(parse_xml(destination), "Value"), "new")

    @unittest.skipUnless(sys.platform == "win32", "Windows junction integration test")
    def test_xml_transaction_rechecks_trusted_path_after_hash_before_backup(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_dir = root / "Data" / "Platforms"
            destination = platforms_dir / "NES.xml"
            destination.write_text("<Root><Value>trusted</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "changed"
            saved_dir = root / "Data" / "SavedPlatforms"
            external_dir = root / "ExternalPlatforms"
            external_dir.mkdir()
            external = external_dir / "NES.xml"
            external.write_text("<Root><Secret>external-sentinel</Secret></Root>", encoding="utf-8")
            real_sha256 = safe_write.sha256_file
            swapped = False

            def hash_then_swap(path: Path, *, control=None) -> str:
                nonlocal swapped
                digest = real_sha256(path, control=control)
                if path == destination and not swapped:
                    platforms_dir.rename(saved_dir)
                    create_directory_junction(platforms_dir, external_dir)
                    swapped = True
                return digest

            mutation = XmlMutation(
                destination,
                tree,
                trusted_parent=platforms_dir,
                trust_anchor=root,
            )
            try:
                with patch.object(safe_write, "sha256_file", side_effect=hash_then_swap):
                    with patch(
                        "launchbox_tools.runtime_checks.is_launchbox_process_running",
                        return_value=False,
                    ):
                        transaction = execute_xml_transaction([mutation], root / "Backups")
            finally:
                if swapped:
                    remove_directory_junction(platforms_dir)
                    saved_dir.rename(platforms_dir)

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertIsInstance(transaction.unsafe_path_error, UnsafeDatabasePathError)
            self.assertEqual(external.read_text(encoding="utf-8"), "<Root><Secret>external-sentinel</Secret></Root>")
            self.assertFalse(list((root / "Backups").rglob("NES.xml")))

    @unittest.skipUnless(sys.platform == "win32", "Windows junction integration test")
    def test_xml_transaction_cleans_workspace_after_real_post_stage_junction_swap(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_dir = root / "Data" / "Platforms"
            destination = platforms_dir / "NES.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            original = destination.read_bytes()
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            saved_dir = root / "Data" / "SavedPlatforms"
            external_dir = root / "ExternalPlatforms"
            external_dir.mkdir()
            external = external_dir / "NES.xml"
            external.write_text("<Root><Value>external</Value></Root>", encoding="utf-8")
            real_validate = safe_write._validate_xml_file
            swapped = False

            def validate_then_swap(path: Path, *, control=None) -> None:
                nonlocal swapped
                real_validate(path, control=control)
                if path.name.endswith(".stage.xml") and not swapped:
                    platforms_dir.rename(saved_dir)
                    create_directory_junction(platforms_dir, external_dir)
                    swapped = True

            mutation = XmlMutation(
                destination,
                tree,
                trusted_parent=platforms_dir,
                trust_anchor=root,
            )
            try:
                with patch.object(
                    safe_write,
                    "_validate_xml_file",
                    side_effect=validate_then_swap,
                ):
                    with patch(
                        "launchbox_tools.runtime_checks.is_launchbox_process_running",
                        return_value=False,
                    ):
                        with patch.object(safe_write, "_commit_staged_file") as commit:
                            transaction = execute_xml_transaction(
                                [mutation],
                                root / "Backups",
                            )
            finally:
                if swapped:
                    remove_directory_junction(platforms_dir)
                    saved_dir.rename(platforms_dir)

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertEqual(transaction.files[0].state, MutationState.PREPARED)
            self.assertIsInstance(transaction.unsafe_path_error, UnsafeDatabasePathError)
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(external.read_text(encoding="utf-8"), "<Root><Value>external</Value></Root>")
            commit.assert_not_called()
            self.assertFalse((root / ".launchbox-utils-work").exists())
            self.assertFalse(list(root.rglob("*.stage.xml")))

    @unittest.skipUnless(sys.platform == "win32", "Windows symlink integration test")
    def test_xml_transaction_rejects_precreated_stage_symlink_in_workspace(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "NES.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            workspace_parent = root / ".launchbox-utils-work"
            workspace_root = workspace_parent / "prepared"
            workspace_root.mkdir(parents=True)
            sentinel = root / "external-sentinel.xml"
            sentinel.write_text("external-sentinel", encoding="utf-8")
            stage_path = workspace_root / "0001.stage.xml"
            try:
                os.symlink(sentinel, stage_path)
            except OSError as exc:
                self.skipTest(f"Windows file symlink is unavailable: {exc}")

            mutation = XmlMutation(
                destination,
                tree,
                trusted_parent=destination.parent,
                trust_anchor=root,
            )
            with patch.object(
                safe_write,
                "_reserve_transaction_workspace",
                return_value=(workspace_parent, workspace_root),
            ):
                with patch(
                    "launchbox_tools.runtime_checks.is_launchbox_process_running",
                    return_value=False,
                ):
                    transaction = execute_xml_transaction([mutation], root / "Backups")

            self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
            self.assertIsInstance(transaction.unsafe_path_error, UnsafeDatabasePathError)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "external-sentinel")

    @unittest.skipUnless(sys.platform == "win32", "Windows junction integration test")
    def test_xml_transaction_cleanup_never_follows_swapped_workspace_junction(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            destination = root / "Data" / "Platforms" / "NES.xml"
            destination.write_text("<Root><Value>old</Value></Root>", encoding="utf-8")
            tree = ET.parse(destination)
            tree.getroot().find("Value").text = "new"
            external_dir = root / "ExternalWorkspace"
            external_dir.mkdir()
            real_validate = safe_write._validate_xml_file
            workspace_root: Path | None = None
            saved_workspace: Path | None = None
            external_stage: Path | None = None

            def validate_then_swap_workspace(path: Path, *, control=None) -> None:
                nonlocal workspace_root, saved_workspace, external_stage
                real_validate(path, control=control)
                if not path.name.endswith(".stage.xml") or workspace_root is not None:
                    return
                workspace_root = path.parent
                saved_workspace = workspace_root.with_name(f"{workspace_root.name}-saved")
                external_stage = external_dir / path.name
                external_stage.write_text("external-stage-sentinel", encoding="utf-8")
                workspace_root.rename(saved_workspace)
                create_directory_junction(workspace_root, external_dir)

            mutation = XmlMutation(
                destination,
                tree,
                trusted_parent=destination.parent,
                trust_anchor=root,
            )
            try:
                with patch.object(
                    safe_write,
                    "_validate_xml_file",
                    side_effect=validate_then_swap_workspace,
                ):
                    with patch(
                        "launchbox_tools.runtime_checks.is_launchbox_process_running",
                        return_value=False,
                    ):
                        transaction = execute_xml_transaction([mutation], root / "Backups")

                self.assertEqual(transaction.outcome, MutationOutcome.FAILED)
                self.assertIsInstance(transaction.unsafe_path_error, UnsafeDatabasePathError)
                self.assertIsNotNone(external_stage)
                self.assertEqual(
                    external_stage.read_text(encoding="utf-8"),
                    "external-stage-sentinel",
                )
            finally:
                if workspace_root is not None and workspace_root.exists():
                    remove_directory_junction(workspace_root)
                if saved_workspace is not None and saved_workspace.exists():
                    for path in saved_workspace.iterdir():
                        path.unlink()
                    saved_workspace.rmdir()
                workspace_parent = root / ".launchbox-utils-work"
                if workspace_parent.exists():
                    workspace_parent.rmdir()

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

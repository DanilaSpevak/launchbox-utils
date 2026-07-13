import xml.etree.ElementTree as ET
from pathlib import Path
from launchbox_tools.operation_lifecycle import OperationCancelled
from launchbox_tools.xml_repository import (
    load_application_entries,
    load_platforms,
    parse_xml,
    parse_xml_tree,
)

from test.support import CancelAfterCheckpoints, LaunchBoxTestCase


class XmlRepositoryTests(LaunchBoxTestCase):
    def test_parse_xml_tree_checks_cancellation_during_large_parse(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            xml_path = root / "large.xml"
            xml_path.write_text(
                "<Root>" + "".join(f"<Item>{index}</Item>" for index in range(300)) + "</Root>",
                encoding="utf-8",
            )
            control = CancelAfterCheckpoints(2)

            with self.assertRaises(OperationCancelled):
                parse_xml_tree(xml_path, control=control)

            self.assertGreaterEqual(control.checkpoint_calls, 2)

    def test_parse_xml_tree_checks_cancellation_inside_one_large_text_node(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "large-text.xml"
            xml_path.write_bytes(b"<Root>" + b"x" * (3 * 1024 * 1024) + b"</Root>")
            control = CancelAfterCheckpoints(4)

            with self.assertRaises(OperationCancelled):
                parse_xml_tree(xml_path, control=control)

            self.assertGreaterEqual(control.checkpoint_calls, 4)

    def test_parse_xml_tree_preserves_parse_error_across_read_boundary(self) -> None:
        with self.make_root() as temp_dir:
            xml_path = Path(temp_dir) / "malformed.xml"
            xml_path.write_bytes(
                b"<Root>" + b"x" * (1024 * 1024 - len(b"<Root>")) + b"<Broken></Root>"
            )

            with self.assertRaises(ET.ParseError):
                parse_xml_tree(xml_path, control=CancelAfterCheckpoints(10_000))

    def test_load_application_entries_checks_cancellation_during_iteration(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root)
            self.write_games_xml(
                root,
                [(f"Game {index}", f"Games/NES/{index}.zip") for index in range(300)],
            )
            platform = load_platforms(root)[0]
            xml_root = parse_xml(platform.database_xml)
            control = CancelAfterCheckpoints(2)

            with self.assertRaises(OperationCancelled):
                load_application_entries(
                    platform,
                    root,
                    xml_root,
                    include_xml_links=True,
                    control=control,
                )

            self.assertGreaterEqual(control.checkpoint_calls, 2)

    def test_load_platforms_resolves_relative_folder(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")

            platforms = load_platforms(root)

            self.assertEqual(len(platforms), 1)
            self.assertEqual(platforms[0].name, "Nintendo Entertainment System")
            self.assertEqual(platforms[0].folder, (root / "Games" / "NES").resolve(strict=False))
            self.assertEqual(
                platforms[0].database_xml,
                root / "Data" / "Platforms" / "Nintendo Entertainment System.xml",
            )

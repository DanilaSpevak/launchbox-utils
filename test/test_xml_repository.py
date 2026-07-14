import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from launchbox_tools.operation_lifecycle import OperationCancelled
from launchbox_tools.paths import UnsafeDatabasePathError
from launchbox_tools.xml_repository import (
    load_application_entries,
    load_platform_catalog,
    load_platform_database_tree,
    load_platforms,
    parse_xml,
    parse_xml_tree,
)

from test.support import CancelAfterCheckpoints, LaunchBoxTestCase


class XmlRepositoryTests(LaunchBoxTestCase):
    def test_platform_catalog_guards_metadata_before_and_after_single_parse(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root)
            events: list[str] = []

            from launchbox_tools import xml_repository

            real_guard = xml_repository.platforms_metadata_path
            real_parse = xml_repository.parse_xml_tree

            def guard(path: Path) -> Path:
                events.append("guard")
                return real_guard(path)

            def parse(path: Path, *, control=None) -> ET.ElementTree:
                events.append("parse")
                return real_parse(path, control=control)

            with patch.object(xml_repository, "platforms_metadata_path", side_effect=guard):
                with patch.object(xml_repository, "parse_xml_tree", side_effect=parse):
                    snapshot = load_platform_catalog(root)

            self.assertEqual(events, ["guard", "parse", "guard"])
            self.assertEqual(len(snapshot.platforms), 1)
            self.assertEqual(snapshot.metadata_path, root / "Data" / "Platforms.xml")

    def test_platform_catalog_post_guard_replaces_parse_error_with_trust_failure(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            metadata_path = root / "Data" / "Platforms.xml"
            metadata_path.write_text("<broken", encoding="utf-8")
            unsafe_error = UnsafeDatabasePathError(
                "metadata path changed during parse",
                reason="reparse_point",
                path=root / "Data",
            )

            with patch(
                "launchbox_tools.xml_repository.platforms_metadata_path",
                side_effect=[metadata_path, unsafe_error],
            ) as guard:
                with self.assertRaises(UnsafeDatabasePathError) as context:
                    load_platform_catalog(root)

            self.assertIs(context.exception, unsafe_error)
            self.assertEqual(guard.call_count, 2)

    def test_platform_catalog_preserves_parse_error_when_post_guard_is_safe(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            metadata_path = root / "Data" / "Platforms.xml"
            metadata_path.write_text("<broken", encoding="utf-8")

            with self.assertRaises(ET.ParseError):
                load_platform_catalog(root)

    def test_platform_database_tree_guards_immediately_around_parse(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root)
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]
            events: list[str] = []

            from launchbox_tools import xml_repository

            real_guard = xml_repository.ensure_platform_database_path
            real_parse = xml_repository.parse_xml_tree

            def guard(*args, **kwargs) -> Path:
                events.append("guard")
                return real_guard(*args, **kwargs)

            def parse(path: Path, *, control=None) -> ET.ElementTree:
                events.append("parse")
                return real_parse(path, control=control)

            with patch.object(xml_repository, "ensure_platform_database_path", side_effect=guard):
                with patch.object(xml_repository, "parse_xml_tree", side_effect=parse):
                    tree = load_platform_database_tree(platform, root)

            self.assertIsNotNone(tree)
            self.assertEqual(events, ["guard", "guard", "parse", "guard"])

    def test_platform_database_post_guard_replaces_parse_error_with_trust_failure(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root)
            platform = load_platforms(root)[0]
            platform.database_xml.write_text("<broken", encoding="utf-8")
            unsafe_error = UnsafeDatabasePathError(
                "platform path changed during parse",
                reason="reparse_point",
                path=platform.database_xml,
                platform_name=platform.name,
            )

            with patch(
                "launchbox_tools.xml_repository.ensure_platform_database_path",
                side_effect=[platform.database_xml, platform.database_xml, unsafe_error],
            ) as guard:
                with self.assertRaises(UnsafeDatabasePathError) as context:
                    load_platform_database_tree(platform, root)

            self.assertIs(context.exception, unsafe_error)
            self.assertEqual(guard.call_count, 3)

    def test_platform_database_tree_returns_none_without_parsing_missing_file(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root)
            platform = load_platforms(root)[0]

            with patch("launchbox_tools.xml_repository.parse_xml_tree") as parse:
                tree = load_platform_database_tree(platform, root)

            self.assertIsNone(tree)
            parse.assert_not_called()

    def test_load_platforms_rejects_unsafe_name_instead_of_skipping_it(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            (root / "Data" / "Platforms.xml").write_text(
                "<ArrayOfPlatform><Platform><Name>..\\..\\sentinel</Name>"
                "<Folder>Games</Folder></Platform></ArrayOfPlatform>",
                encoding="utf-8",
            )
            sentinel = root / "sentinel.xml"
            sentinel.write_text("<Outside />", encoding="utf-8")

            with self.assertRaises(UnsafeDatabasePathError) as context:
                load_platforms(root)

            self.assertEqual(context.exception.reason, "invalid_platform_name")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "<Outside />")

    def test_load_platforms_rejects_missing_platform_name(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            (root / "Data" / "Platforms.xml").write_text(
                "<ArrayOfPlatform><Platform><Folder>Games</Folder>"
                "</Platform></ArrayOfPlatform>",
                encoding="utf-8",
            )

            with self.assertRaises(UnsafeDatabasePathError) as context:
                load_platforms(root)

            self.assertEqual(context.exception.reason, "invalid_platform_name")

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

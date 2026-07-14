import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from launchbox_tools.paths import (
    UnsafeDatabasePathError,
    ensure_trusted_direct_child,
    ensure_platform_database_path,
    platform_database_path,
    platforms_metadata_path,
)

from test.support import LaunchBoxTestCase, create_directory_junction, remove_directory_junction


class TrustedDatabasePathTests(LaunchBoxTestCase):
    def test_trusted_paths_preserve_non_reparse_root_spelling(self) -> None:
        short_root = (Path.cwd() / "RUNNER~1" / "LaunchBox").absolute()
        canonical_root = (Path.cwd() / "runneradmin" / "LaunchBox").absolute()
        parent = short_root / "Data"
        destination = parent / "Platforms.xml"

        def canonicalize(path: Path, *, strict: bool = False) -> Path:
            return canonical_root / path.relative_to(short_root)

        with patch("launchbox_tools.paths.os.lstat", side_effect=FileNotFoundError):
            with patch.object(type(short_root), "resolve", canonicalize):
                actual = ensure_trusted_direct_child(short_root, parent, destination)
                metadata = platforms_metadata_path(short_root)
                database = platform_database_path(short_root, "NES")

        self.assertEqual(actual, destination)
        self.assertEqual(metadata, destination)
        self.assertEqual(database, short_root / "Data" / "Platforms" / "NES.xml")

    def test_platform_database_path_accepts_valid_unicode_component(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)

            actual = platform_database_path(root, "Nintendo — Япония 2.0")

            self.assertEqual(
                actual,
                root.resolve() / "Data" / "Platforms" / "Nintendo — Япония 2.0.xml",
            )

    def test_platform_database_path_rejects_invalid_windows_components(self) -> None:
        invalid_names = (
            "",
            ".",
            "..",
            "../Outside",
            r"..\Outside",
            r"C:\Outside",
            r"\\server\share",
            "/Outside",
            "Bad:Name",
            "Bad|Name",
            "Bad\x01Name",
            "Bad.",
            "Bad ",
            "CON",
            "con.txt",
            "COM1.arcade",
            "CON .txt",
            "LPT9",
            "COM¹",
            "com².arcade",
            "COM³",
            "LPT¹",
            "lpt².txt",
            "LPT³",
            "x" * 252,
            "bad\ud800name",
        )
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            for name in invalid_names:
                with self.subTest(name=name):
                    with self.assertRaises(UnsafeDatabasePathError) as context:
                        platform_database_path(root, name)
                    self.assertEqual(context.exception.reason, "invalid_platform_name")

    def test_platform_database_path_rejects_forged_direct_child(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            forged = root / "Data" / "Platforms" / "Other.xml"

            with self.assertRaises(UnsafeDatabasePathError) as context:
                ensure_platform_database_path(root, "NES", forged)

            self.assertEqual(context.exception.reason, "outside_trusted_directory")

    @unittest.skipUnless(sys.platform == "win32", "Windows junction integration test")
    def test_platforms_metadata_rejects_data_junction(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            data = root / "Data"
            external_data = root / "ExternalData"
            data.rename(external_data)
            create_directory_junction(data, external_data)
            try:
                with self.assertRaises(UnsafeDatabasePathError) as context:
                    platforms_metadata_path(root)
                self.assertEqual(context.exception.reason, "reparse_point")
                self.assertEqual(context.exception.path, data)
            finally:
                remove_directory_junction(data)

    @unittest.skipUnless(sys.platform == "win32", "Windows reparse integration test")
    def test_platform_database_rejects_file_symlink(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms = root / "Data" / "Platforms"
            target = platforms / "Actual.xml"
            target.write_text("<LaunchBox />", encoding="utf-8")
            link = platforms / "NES.xml"
            try:
                os.symlink(target, link)
            except OSError as exc:
                self.skipTest(f"Windows file symlink is unavailable: {exc}")

            with self.assertRaises(UnsafeDatabasePathError) as context:
                platform_database_path(root, "NES")

            self.assertEqual(context.exception.reason, "reparse_point")
            self.assertEqual(context.exception.path, link)

    @unittest.skipUnless(sys.platform == "win32", "Windows junction integration test")
    def test_canonical_root_alias_is_allowed(self) -> None:
        with self.make_root() as temp_dir:
            container = Path(temp_dir)
            physical_root = container / "PhysicalLaunchBox"
            (physical_root / "Data" / "Platforms").mkdir(parents=True)
            alias = container / "LaunchBoxAlias"
            create_directory_junction(alias, physical_root)
            try:
                actual = platform_database_path(alias, "NES")
                self.assertEqual(actual, physical_root / "Data" / "Platforms" / "NES.xml")
            finally:
                remove_directory_junction(alias)

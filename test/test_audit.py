from pathlib import Path
from launchbox_tools.operations.audit import audit_platform
from launchbox_tools.xml_repository import load_platforms

from test.support import LaunchBoxTestCase


class AuditTests(LaunchBoxTestCase):
    def test_audit_platform_finds_missing_and_unregistered_files(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            rom_folder = root / "Games" / "NES"
            rom_folder.mkdir(parents=True)
            (rom_folder / "present.zip").write_bytes(b"rom")
            (rom_folder / "extra.zip").write_bytes(b"rom")
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(
                root,
                [
                    ("Present Game", "Games/NES/present.zip"),
                    ("Missing Game", "Games/NES/missing.zip"),
                ],
            )

            platform = load_platforms(root)[0]
            result = audit_platform(platform, root)

            self.assertEqual(result.database_count, 2)
            self.assertEqual(result.folder_count, 2)
            self.assertEqual([game.title for game in result.missing_on_disk], ["Missing Game"])
            self.assertEqual([path.name for path in result.not_in_database], ["extra.zip"])

    def test_missing_rom_folder_marks_database_entries_missing(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(
                root,
                [
                    ("First Game", "Games/NES/first.zip"),
                    ("Second Game", "Games/NES/second.zip"),
                ],
            )

            platform = load_platforms(root)[0]
            result = audit_platform(platform, root)

            self.assertEqual(result.folder_count, 0)
            self.assertEqual(sorted(game.title for game in result.missing_on_disk), ["First Game", "Second Game"])
            self.assertEqual(result.not_in_database, [])
            self.assertTrue(any("ROM folder not found" in warning for warning in result.warnings))

    def test_audit_missing_on_disk_uses_exists_even_when_rom_folder_scan_warns(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            rom_folder = root / "Games" / "NES"
            rom_folder.mkdir(parents=True)
            (rom_folder / "present.zip").write_bytes(b"rom")
            self.write_platforms_xml(root, "Games/MissingFolder")
            self.write_games_xml(
                root,
                [
                    ("Present Game", "Games/NES/present.zip"),
                    ("Missing Game", "Games/NES/missing.zip"),
                ],
            )

            platform = load_platforms(root)[0]
            result = audit_platform(platform, root)

            self.assertEqual([game.title for game in result.missing_on_disk], ["Missing Game"])
            self.assertTrue(any("ROM folder not found" in warning for warning in result.warnings))

    def test_additional_application_paths_count_as_database_entries(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            rom_folder = root / "Games" / "NES"
            rom_folder.mkdir(parents=True)
            (rom_folder / "extra-version.zip").write_bytes(b"rom")
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                """  <Game>
    <Title>Main Game</Title>
    <ApplicationPath>Games/NES/main.zip</ApplicationPath>
  </Game>
  <AdditionalApplication>
    <Name>Play extra version</Name>
    <ApplicationPath>Games/NES/extra-version.zip</ApplicationPath>
  </AdditionalApplication>""",
            )

            platform = load_platforms(root)[0]
            result = audit_platform(platform, root)

            self.assertEqual(result.database_count, 2)
            self.assertNotIn("extra-version.zip", [path.name for path in result.not_in_database])

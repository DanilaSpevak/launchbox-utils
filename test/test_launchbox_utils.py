import csv
import tempfile
import unittest
from pathlib import Path

from launchbox_tools.operations.audit import audit_platform
from launchbox_tools.operations.dedupe_additional_apps import run_additional_apps_dedupe
from launchbox_tools.paths import safe_report_dir_name
from launchbox_tools.reports.audit_reports import write_reports
from launchbox_tools.reports.dedupe_reports import write_dedupe_reports
from launchbox_tools.xml_repository import child_text, load_platforms, local_name, parse_xml


class LaunchBoxAuditTests(unittest.TestCase):
    def make_root(self) -> tempfile.TemporaryDirectory:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        (root / "Data" / "Platforms").mkdir(parents=True)
        return temp_dir

    def write_platforms_xml(self, root: Path, folder: str = "Games/NES") -> None:
        (root / "Data" / "Platforms.xml").write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<ArrayOfPlatform>
  <Platform>
    <Name>Nintendo Entertainment System</Name>
    <Folder>{folder}</Folder>
  </Platform>
</ArrayOfPlatform>
""",
            encoding="utf-8",
        )

    def write_games_xml(self, root: Path, games: list[tuple[str, str]]) -> None:
        game_xml = "\n".join(
            f"""  <Game>
    <Title>{title}</Title>
    <ApplicationPath>{application_path}</ApplicationPath>
  </Game>"""
            for title, application_path in games
        )
        (root / "Data" / "Platforms" / "Nintendo Entertainment System.xml").write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
{game_xml}
</LaunchBox>
""",
            encoding="utf-8",
        )

    def write_games_xml_raw(self, root: Path, body: str) -> None:
        (root / "Data" / "Platforms" / "Nintendo Entertainment System.xml").write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
{body}
</LaunchBox>
""",
            encoding="utf-8",
        )

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

    def test_dedupe_additional_apps_dry_run_reports_duplicates_without_changing_xml(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                """  <AdditionalApplication>
    <GameID>game-1</GameID>
    <Name>Keep this version</Name>
    <ApplicationPath>Games/NES/duplicate.zip</ApplicationPath>
  </AdditionalApplication>
  <AdditionalApplication>
    <GameID>game-1</GameID>
    <Name>Удалить этот дубль</Name>
    <ApplicationPath>Games/NES/duplicate.zip</ApplicationPath>
  </AdditionalApplication>""",
            )
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            before = xml_path.read_text(encoding="utf-8")

            results = run_additional_apps_dedupe(root, apply_changes=False)
            output_dir = root / "AuditReports"
            write_dedupe_reports(results, output_dir, apply_changes=False)

            after = xml_path.read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertEqual(len(results[0].duplicates), 1)

            platform_report = output_dir / "Nintendo Entertainment System" / "duplicate_additional_apps.txt"
            report_text = platform_report.read_text(encoding="utf-8")
            self.assertIn("Mode: dry-run", report_text)
            self.assertIn("Удалить этот дубль", report_text)

            summary_text = (output_dir / "duplicate_additional_apps.csv").read_text(encoding="cp1251")
            self.assertTrue(summary_text.startswith("sep=;\n"))
            self.assertIn("Удалить этот дубль", summary_text)

    def test_dedupe_additional_apps_apply_removes_duplicates_and_creates_backup(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                """  <AdditionalApplication>
    <GameID>game-1</GameID>
    <Name>Keep this version</Name>
    <ApplicationPath>Games/NES/duplicate.zip</ApplicationPath>
  </AdditionalApplication>
  <AdditionalApplication>
    <GameID>game-1</GameID>
    <Name>Remove this duplicate</Name>
    <ApplicationPath>Games/NES/duplicate.zip</ApplicationPath>
  </AdditionalApplication>
  <AdditionalApplication>
    <GameID>game-2</GameID>
    <Name>Keep different game</Name>
    <ApplicationPath>Games/NES/duplicate.zip</ApplicationPath>
  </AdditionalApplication>""",
            )

            results = run_additional_apps_dedupe(root, apply_changes=True)

            self.assertTrue(results[0].applied)
            self.assertIsNotNone(results[0].backup_path)
            self.assertTrue(results[0].backup_path.exists())

            xml_root = parse_xml(root / "Data" / "Platforms" / "Nintendo Entertainment System.xml")
            additional_apps = [element for element in xml_root if local_name(element.tag) == "AdditionalApplication"]
            names = [child_text(element, "Name") for element in additional_apps]
            self.assertEqual(len(additional_apps), 2)
            self.assertIn("Keep this version", names)
            self.assertIn("Keep different game", names)
            self.assertNotIn("Remove this duplicate", names)

    def test_dedupe_additional_apps_can_filter_platform(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(root, "")

            results = run_additional_apps_dedupe(root, platform_filter="Missing Platform", apply_changes=False)

            self.assertEqual(results, [])

    def test_write_reports_creates_platform_reports_and_summary_csv(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            rom_folder = root / "Games" / "NES"
            rom_folder.mkdir(parents=True)
            (rom_folder / "extra.zip").write_bytes(b"rom")
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Missing Game", "Games/NES/missing.zip")])

            result = audit_platform(load_platforms(root)[0], root)
            output_dir = root / "AuditReports"
            (output_dir).mkdir()
            (output_dir / "missing_on_disk.txt").write_text("old aggregate report", encoding="utf-8")
            (output_dir / "not_in_database.txt").write_text("old aggregate report", encoding="utf-8")
            write_reports([result], output_dir)

            platform_dir = output_dir / "Nintendo Entertainment System"
            missing_text = (platform_dir / "missing_on_disk.txt").read_text(encoding="utf-8")
            extra_text = (platform_dir / "not_in_database.txt").read_text(encoding="utf-8")
            self.assertIn("Missing Game", missing_text)
            self.assertIn("extra.zip", extra_text)
            self.assertFalse((output_dir / "missing_on_disk.txt").exists())
            self.assertFalse((output_dir / "not_in_database.txt").exists())

            summary_text = (output_dir / "summary.csv").read_text(encoding="utf-8-sig")
            self.assertTrue(summary_text.startswith("sep=;\n"))

            with (output_dir / "summary.csv").open("r", encoding="utf-8-sig", newline="") as file:
                file.readline()
                rows = list(csv.DictReader(file, delimiter=";"))
            self.assertEqual(rows[0]["platform"], "Nintendo Entertainment System")
            self.assertEqual(rows[0]["missing_on_disk"], "1")
            self.assertEqual(rows[0]["not_in_database"], "1")

    def test_write_reports_only_with_findings_skips_clean_platform_logs(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            rom_folder = root / "Games" / "NES"
            rom_folder.mkdir(parents=True)
            (rom_folder / "present.zip").write_bytes(b"rom")
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Present Game", "Games/NES/present.zip")])

            result = audit_platform(load_platforms(root)[0], root)
            output_dir = root / "AuditReports"
            stale_platform_dir = output_dir / "Nintendo Entertainment System"
            stale_platform_dir.mkdir(parents=True)
            (stale_platform_dir / "missing_on_disk.txt").write_text("stale", encoding="utf-8")
            (stale_platform_dir / "not_in_database.txt").write_text("stale", encoding="utf-8")
            (output_dir / "summary.csv").write_text("stale", encoding="utf-8")

            write_reports([result], output_dir, only_with_findings=True)

            self.assertFalse((output_dir / "summary.csv").exists())
            self.assertFalse(stale_platform_dir.exists())

    def test_write_reports_only_with_findings_writes_only_relevant_audit_files(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            rom_folder = root / "Games" / "NES"
            rom_folder.mkdir(parents=True)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Missing Game", "Games/NES/missing.zip")])

            result = audit_platform(load_platforms(root)[0], root)
            output_dir = root / "AuditReports"
            write_reports([result], output_dir, only_with_findings=True)

            platform_dir = output_dir / "Nintendo Entertainment System"
            self.assertTrue((platform_dir / "missing_on_disk.txt").exists())
            self.assertFalse((platform_dir / "not_in_database.txt").exists())
            self.assertTrue((output_dir / "summary.csv").exists())

    def test_write_dedupe_reports_only_with_findings_skips_clean_platform_logs(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                """  <AdditionalApplication>
    <GameID>game-1</GameID>
    <Name>Only version</Name>
    <ApplicationPath>Games/NES/only.zip</ApplicationPath>
  </AdditionalApplication>""",
            )

            results = run_additional_apps_dedupe(root, apply_changes=False)
            output_dir = root / "AuditReports"
            stale_platform_dir = output_dir / "Nintendo Entertainment System"
            stale_platform_dir.mkdir(parents=True)
            (stale_platform_dir / "duplicate_additional_apps.txt").write_text("stale", encoding="utf-8")
            (output_dir / "duplicate_additional_apps.csv").write_text("stale", encoding="cp1251")

            write_dedupe_reports(results, output_dir, apply_changes=False, only_with_findings=True)

            self.assertFalse((output_dir / "duplicate_additional_apps.csv").exists())
            self.assertFalse(stale_platform_dir.exists())

    def test_safe_report_dir_name_replaces_windows_invalid_characters(self) -> None:
        self.assertEqual(safe_report_dir_name('Arcade: MAME/FBNeo?'), "Arcade_ MAME_FBNeo_")


if __name__ == "__main__":
    unittest.main()

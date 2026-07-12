import csv
from pathlib import Path
from launchbox_tools.operations.audit import audit_platform
from launchbox_tools.operations.dedupe_additional_apps import run_additional_apps_dedupe
from launchbox_tools.operations.path_replacement import run_path_replacement
from launchbox_tools.paths import safe_report_dir_name
from launchbox_tools.reports.audit_reports import write_reports
from launchbox_tools.reports.dedupe_reports import write_dedupe_reports
from launchbox_tools.reports.path_replacement_reports import write_path_replacement_reports
from launchbox_tools.xml_repository import load_platforms

from test.support import LaunchBoxTestCase


class ReportTests(LaunchBoxTestCase):
    def test_write_path_replacement_reports_creates_summary_and_detail(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "Reports"
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            results = run_path_replacement(root, root / "Games" / "NES", root / "Games" / "SNES")

            write_path_replacement_reports(results, output_dir, apply_changes=False)

            summary_text = (output_dir / "path_replacements.csv").read_text(encoding="utf-8-sig")
            detail_text = (output_dir / "Nintendo Entertainment System" / "path_replacements.txt").read_text(encoding="utf-8")
            self.assertIn("dry-run", summary_text)
            self.assertIn("outcome", summary_text)
            self.assertIn("state", summary_text)
            self.assertNotIn("applied", summary_text.casefold())
            rows = list(csv.DictReader(summary_text.splitlines()[1:], delimiter=";"))
            self.assertTrue(rows)
            self.assertEqual(rows[0]["state"], "planned")
            self.assertIn("Outcome: dry_run", detail_text)
            self.assertIn("State: planned", detail_text)
            self.assertIn("Games/NES/game.zip", detail_text)
            self.assertIn("Games/SNES/game.zip", detail_text)

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
            (output_dir / "duplicate_additional_apps.csv").write_text("stale", encoding="utf-8-sig")

            write_dedupe_reports(results, output_dir, apply_changes=False, only_with_findings=True)

            self.assertFalse((output_dir / "duplicate_additional_apps.csv").exists())
            self.assertFalse(stale_platform_dir.exists())

    def test_safe_report_dir_name_replaces_windows_invalid_characters(self) -> None:
        self.assertEqual(safe_report_dir_name('Arcade: MAME/FBNeo?'), "Arcade_ MAME_FBNeo_")

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launchbox_tools.cli import build_arg_parser
from launchbox_tools.config import (
    ConfigError,
    detect_default_language,
    load_app_config,
    load_configured_language,
    load_configured_only_with_findings,
    load_raw_path_config,
    normalize_path_text,
    resolve_initial_language,
    save_interface_language,
    save_raw_path_config,
)
from launchbox_tools.gui.translations import translate
from launchbox_tools.operations.audit import audit_platform
from launchbox_tools.operations.dedupe_additional_apps import run_additional_apps_dedupe
from launchbox_tools.runtime_checks import MutationBlockedError, ensure_safe_to_mutate, is_file_locked
from launchbox_tools.safe_write import write_xml_tree_safely as real_write_xml_tree_safely
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

    def write_two_platforms_xml(self, root: Path) -> None:
        (root / "Data" / "Platforms.xml").write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<ArrayOfPlatform>
  <Platform>
    <Name>Nintendo Entertainment System</Name>
    <Folder>Games/NES</Folder>
  </Platform>
  <Platform>
    <Name>Sega Genesis</Name>
    <Folder>Games/Genesis</Folder>
  </Platform>
</ArrayOfPlatform>
""",
            encoding="utf-8",
        )

    def write_platform_games_xml_raw(self, root: Path, platform_name: str, body: str) -> None:
        (root / "Data" / "Platforms" / f"{platform_name}.xml").write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
{body}
</LaunchBox>
""",
            encoding="utf-8",
        )

    def duplicate_additional_app_xml(self, game_id: str, keep_title: str, remove_title: str, path: str) -> str:
        return f"""  <AdditionalApplication>
    <GameID>{game_id}</GameID>
    <Name>{keep_title}</Name>
    <ApplicationPath>{path}</ApplicationPath>
  </AdditionalApplication>
  <AdditionalApplication>
    <GameID>{game_id}</GameID>
    <Name>{remove_title}</Name>
    <ApplicationPath>{path}</ApplicationPath>
  </AdditionalApplication>"""

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

    def test_load_app_config_reads_paths_from_ini(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "launchbox_utils.ini"
            config_path.write_text(
                f"""[paths]
launchbox_root = {temp_path / "LaunchBox"}
output_dir = Reports
""",
                encoding="utf-8",
            )

            config = load_app_config(config_path)

            self.assertEqual(config.launchbox_root, (temp_path / "LaunchBox").resolve(strict=False))
            self.assertEqual(config.output_dir, (temp_path / "LaunchBox" / "Reports").resolve(strict=False))

    def test_load_app_config_cli_overrides_ini(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "launchbox_utils.ini"
            config_path.write_text(
                f"""[paths]
launchbox_root = {temp_path / "ConfiguredLaunchBox"}
output_dir = ConfiguredReports
""",
                encoding="utf-8",
            )

            config = load_app_config(
                config_path,
                root_override=str(temp_path / "CliLaunchBox"),
                output_override=str(temp_path / "CliReports"),
            )

            self.assertEqual(config.launchbox_root, (temp_path / "CliLaunchBox").resolve(strict=False))
            self.assertEqual(config.output_dir, (temp_path / "CliReports").resolve(strict=False))

    def test_load_app_config_requires_root_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "missing.ini"

            with self.assertRaises(ConfigError):
                load_app_config(config_path)

    def test_raw_path_config_round_trip_for_gui(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_raw_path_config(config_path, r"D:\Games\LaunchBox", "AuditReports")
            raw_config = load_raw_path_config(config_path)

            self.assertEqual(raw_config.launchbox_root, r"D:\Games\LaunchBox")
            self.assertEqual(raw_config.output_dir, "AuditReports")

    def test_detect_default_language_uses_russian_system_locale(self) -> None:
        with patch("launchbox_tools.config._system_locale_codes", return_value=["Russian_Russia"]):
            self.assertEqual(detect_default_language(), "ru")

        with patch("launchbox_tools.config._system_locale_codes", return_value=["ru_RU"]):
            self.assertEqual(detect_default_language(), "ru")

    def test_detect_default_language_uses_english_for_non_russian_locale(self) -> None:
        with patch("launchbox_tools.config._system_locale_codes", return_value=["en_US"]):
            self.assertEqual(detect_default_language(), "en")

    def test_resolve_initial_language_reads_configured_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"
            config_path.write_text(
                """[interface]
language = en
""",
                encoding="utf-8",
            )

            self.assertEqual(resolve_initial_language(config_path), "en")

    def test_resolve_initial_language_falls_back_to_system_locale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            with patch("launchbox_tools.config.detect_default_language", return_value="ru"):
                self.assertEqual(resolve_initial_language(config_path), "ru")

    def test_resolve_initial_language_ignores_invalid_configured_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"
            config_path.write_text(
                """[interface]
language = fr
""",
                encoding="utf-8",
            )

            with patch("launchbox_tools.config.detect_default_language", return_value="en"):
                self.assertEqual(resolve_initial_language(config_path), "en")

    def test_save_interface_language_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_interface_language(config_path, "ru")

            self.assertEqual(load_configured_language(config_path), "ru")

    def test_save_raw_path_config_preserves_interface_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_interface_language(config_path, "en")
            save_raw_path_config(config_path, r"D:\Games\LaunchBox", "AuditReports")

            self.assertEqual(load_configured_language(config_path), "en")
            raw_config = load_raw_path_config(config_path)
            self.assertEqual(raw_config.launchbox_root, r"D:\Games\LaunchBox")
            self.assertEqual(raw_config.output_dir, "AuditReports")

    def test_load_configured_only_with_findings_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            self.assertFalse(load_configured_only_with_findings(config_path))

    def test_save_raw_path_config_round_trip_for_only_with_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_raw_path_config(
                config_path,
                r"D:\Games\LaunchBox",
                "AuditReports",
                only_with_findings=True,
            )

            self.assertTrue(load_configured_only_with_findings(config_path))

            save_raw_path_config(
                config_path,
                r"D:\Games\LaunchBox",
                "AuditReports",
                only_with_findings=False,
            )

            self.assertFalse(load_configured_only_with_findings(config_path))

    def test_save_interface_language_preserves_only_with_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_raw_path_config(
                config_path,
                r"D:\Games\LaunchBox",
                "AuditReports",
                only_with_findings=True,
            )
            save_interface_language(config_path, "ru")

            self.assertEqual(load_configured_language(config_path), "ru")
            self.assertTrue(load_configured_only_with_findings(config_path))

    def test_save_raw_path_config_without_only_with_findings_preserves_existing_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_raw_path_config(
                config_path,
                r"D:\Games\LaunchBox",
                "AuditReports",
                only_with_findings=True,
            )
            save_raw_path_config(config_path, r"D:\LaunchBox", "Reports")

            self.assertTrue(load_configured_only_with_findings(config_path))
            raw_config = load_raw_path_config(config_path)
            self.assertEqual(raw_config.launchbox_root, r"D:\LaunchBox")
            self.assertEqual(raw_config.output_dir, "Reports")

    def test_save_interface_language_rejects_unsupported_language(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            with self.assertRaises(ConfigError):
                save_interface_language(config_path, "fr")

    def test_normalize_path_text_collapses_duplicate_separators(self) -> None:
        self.assertEqual(normalize_path_text(r"D:\\Games\\LaunchBox"), r"D:\Games\LaunchBox")
        self.assertEqual(normalize_path_text(r"\\server\\share\\LaunchBox"), r"\\server\share\LaunchBox")

    def test_gui_translations_support_russian_and_english(self) -> None:
        self.assertEqual(translate("en", "audit_group"), "Audit")
        self.assertEqual(translate("ru", "audit_group"), "Аудит")
        self.assertEqual(translate("ru", "interface_language_tooltip"), "Язык интерфейса")
        self.assertEqual(translate("en", "browse_launchbox_tooltip"), "Select LaunchBox folder")
        self.assertEqual(translate("ru", "dedupe_dry_run"), "Найти дубли в дополнительных приложениях")
        self.assertEqual(translate("ru", "dedupe_apply"), "Удалить дубли дополнительных приложений")
        self.assertEqual(
            translate("ru", "dedupe_dry_run_tooltip"),
            "Найти дубли дополнительных приложений и записать отчеты без изменения XML-файлов.",
        )
        self.assertIn("резервной копии", translate("ru", "dedupe_apply_tooltip"))
        self.assertIn("LaunchBox", translate("ru", "mutation_blocked_launchbox"))
        self.assertEqual(translate("missing", "audit_group"), "Audit")

    def test_cli_parser_supports_gui_command(self) -> None:
        args = build_arg_parser().parse_args(["gui"])
        self.assertEqual(args.command, "gui")

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

            summary_text = (output_dir / "duplicate_additional_apps.csv").read_text(encoding="utf-8-sig")
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

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
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

    def test_ensure_safe_to_mutate_blocks_when_launchbox_running(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=True):
                with self.assertRaises(MutationBlockedError) as context:
                    ensure_safe_to_mutate([platform.database_xml])

            self.assertEqual(context.exception.reason, "launchbox_running")

    def test_ensure_safe_to_mutate_blocks_when_file_locked(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.runtime_checks.is_file_locked", return_value=True):
                    with self.assertRaises(MutationBlockedError) as context:
                        ensure_safe_to_mutate([platform.database_xml])

            self.assertEqual(context.exception.reason, "files_locked")
            self.assertEqual(context.exception.locked_files, [platform.database_xml])

    def test_dedupe_apply_aborts_when_launchbox_running(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                self.duplicate_additional_app_xml("game-1", "Keep", "Remove", "Games/NES/duplicate.zip"),
            )
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            before = xml_path.read_text(encoding="utf-8")

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=True):
                with self.assertRaises(MutationBlockedError):
                    run_additional_apps_dedupe(root, apply_changes=True)

            self.assertEqual(xml_path.read_text(encoding="utf-8"), before)

    def test_dedupe_dry_run_not_blocked_when_launchbox_running(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                self.duplicate_additional_app_xml("game-1", "Keep", "Remove", "Games/NES/duplicate.zip"),
            )

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=True):
                results = run_additional_apps_dedupe(root, apply_changes=False)

            self.assertEqual(len(results[0].duplicates), 1)
            self.assertFalse(results[0].applied)

    def test_is_file_locked_returns_false_for_unlocked_file(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            self.assertFalse(is_file_locked(platform.database_xml))

    def test_dedupe_additional_apps_can_filter_platform(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(root, "")

            results = run_additional_apps_dedupe(root, platform_filter="Missing Platform", apply_changes=False)

            self.assertEqual(results, [])

    def test_run_additional_apps_dedupe_continues_after_apply_failure(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_two_platforms_xml(root)
            self.write_platform_games_xml_raw(
                root,
                "Nintendo Entertainment System",
                self.duplicate_additional_app_xml("game-1", "Keep NES", "Remove NES", "Games/NES/duplicate.zip"),
            )
            self.write_platform_games_xml_raw(
                root,
                "Sega Genesis",
                self.duplicate_additional_app_xml("game-2", "Keep Genesis", "Remove Genesis", "Games/Genesis/duplicate.zip"),
            )

            write_calls = 0

            def flaky_write(tree, destination: Path) -> None:
                nonlocal write_calls
                write_calls += 1
                if write_calls == 2:
                    raise OSError("simulated write failure")
                real_write_xml_tree_safely(tree, destination)

            with patch("launchbox_tools.operations.dedupe_additional_apps.write_xml_tree_safely", side_effect=flaky_write):
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    results = run_additional_apps_dedupe(root, apply_changes=True)

            self.assertEqual(len(results), 2)
            self.assertTrue(results[0].applied)
            self.assertIsNone(results[0].error)
            self.assertFalse(results[1].applied)
            self.assertEqual(results[1].error, "simulated write failure")

            nes_xml = parse_xml(root / "Data" / "Platforms" / "Nintendo Entertainment System.xml")
            nes_names = [child_text(element, "Name") for element in nes_xml if local_name(element.tag) == "AdditionalApplication"]
            self.assertEqual(nes_names, ["Keep NES"])

            genesis_xml = parse_xml(root / "Data" / "Platforms" / "Sega Genesis.xml")
            genesis_names = [child_text(element, "Name") for element in genesis_xml if local_name(element.tag) == "AdditionalApplication"]
            self.assertEqual(len(genesis_names), 2)

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


if __name__ == "__main__":
    unittest.main()

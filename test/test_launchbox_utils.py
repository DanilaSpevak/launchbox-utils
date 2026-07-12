import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

from launchbox_tools.cli import _resolve_command, build_arg_parser, main
from launchbox_tools.config import (
    AppConfig,
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
from launchbox_tools.operations.path_replacement import build_replacement_value, run_path_replacement
from launchbox_tools.runtime_checks import MutationBlockedError, ensure_safe_to_mutate, is_file_locked
from launchbox_tools.models import (
    MutationFileResult,
    MutationOutcome,
    MutationRunResult,
    MutationState,
)
from launchbox_tools.mutation_manifest import write_mutation_manifest
from launchbox_tools.paths import safe_report_dir_name
from launchbox_tools.reports.audit_reports import write_reports
from launchbox_tools.reports.dedupe_reports import write_dedupe_reports
from launchbox_tools.reports.path_replacement_reports import write_path_replacement_reports
from launchbox_tools.safe_write import XmlMutation, XmlTransactionResult, execute_xml_transaction
from launchbox_tools.xml_repository import child_text, load_platforms, local_name, parse_xml


class LaunchBoxAuditTests(unittest.TestCase):
    def make_root(self) -> tempfile.TemporaryDirectory:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        (root / "Data" / "Platforms").mkdir(parents=True)
        return temp_dir

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
            self.assertEqual(
                [result.state for result in transaction.files],
                [MutationState.ROLLED_BACK, MutationState.FAILED],
            )
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
            self.assertFalse(list(root.glob("*.stage.tmp")))

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
            self.assertFalse(list(root.glob("*.stage.tmp")))

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
            self.assertFalse(list(root.glob("*.stage.tmp")))

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
    <Name>{keep_title}</Name>
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
        self.assertEqual(translate("ru", "outcome_partial"), "Выполнено частично")
        self.assertEqual(translate("en", "outcome_rolled_back"), "Rolled back")
        self.assertEqual(translate("ru", "state_committed"), "Зафиксировано XML-файлов")
        self.assertIn("LaunchBox", translate("ru", "mutation_blocked_launchbox"))
        self.assertEqual(translate("missing", "audit_group"), "Audit")

    def test_gui_mutation_summary_logs_every_state_and_manifest_error(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        messages: list[str] = []
        app.enqueue_log = messages.append
        app.t = lambda key: translate("en", key)
        run_result = MutationRunResult(
            [],
            MutationOutcome.SUCCESS,
            files=[
                MutationFileResult(Path("planned.xml"), MutationState.PLANNED),
                MutationFileResult(Path("committed.xml"), MutationState.COMMITTED),
            ],
            manifest_error="manifest denied",
        )

        app.log_mutation_state_summary(run_result)

        self.assertIn("Planned XML files: 1", messages)
        self.assertIn("Committed XML files: 1", messages)
        self.assertIn("Manifest error: manifest denied", messages)

    def test_gui_mutation_report_status_logs_report_error_separately(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        messages: list[str] = []
        app.enqueue_log = messages.append
        app.t = lambda key: translate("en", key)

        app.log_mutation_report_status(Path("Reports"), "reports denied")

        self.assertEqual(messages, ["Report error: reports denied"])

    def test_gui_dedupe_worker_preserves_outcome_for_empty_report_error(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        messages: list[str] = []
        app.validate_paths = Mock(return_value=(Path("C:/LaunchBox"), Path("C:/Reports")))
        app.audit_output_mode_var = Mock()
        app.audit_output_mode_var.get.return_value = "all"
        app.enqueue_log = messages.append
        app.t = lambda key: translate("en", key)
        app.start_worker = lambda _message, worker: worker()
        run_result = MutationRunResult(
            [],
            MutationOutcome.SUCCESS,
            files=[MutationFileResult(Path("C:/LaunchBox/NES.xml"), MutationState.COMMITTED)],
            manifest_path=Path("C:/LaunchBox/Backups/run/manifest.json"),
        )

        with patch("launchbox_tools.gui.app.load_platforms", return_value=[]):
            with patch("launchbox_tools.gui.app.ensure_safe_to_mutate"):
                with patch("launchbox_tools.gui.app.messagebox.askyesno", return_value=True):
                    with patch(
                        "launchbox_tools.gui.app.run_additional_apps_dedupe",
                        return_value=run_result,
                    ):
                        with patch(
                            "launchbox_tools.gui.app.write_dedupe_reports",
                            side_effect=OSError(),
                        ):
                            app.run_dedupe_operation(True)

        joined = "\n".join(messages)
        self.assertIn("Outcome: Success", joined)
        self.assertIn("Committed XML files: 1", joined)
        self.assertIn("Manifest: C:\\LaunchBox\\Backups\\run\\manifest.json", joined)
        self.assertIn("Report error: OSError", joined)
        self.assertNotIn("Reports written to", joined)
        self.assertNotIn("Finished", joined)

    def test_gui_path_worker_preserves_outcome_and_logs_unexpected_report_traceback(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        messages: list[str] = []
        app.validate_paths = Mock(return_value=(Path("C:/LaunchBox"), Path("C:/Reports")))
        app.validate_replacement_paths = Mock(return_value=(Path("C:/Old"), Path("C:/New")))
        app.audit_output_mode_var = Mock()
        app.audit_output_mode_var.get.return_value = "all"
        app.enqueue_log = messages.append
        app.t = lambda key: translate("en", key)
        app.start_worker = lambda _message, worker: worker()
        run_result = MutationRunResult(
            [],
            MutationOutcome.ROLLED_BACK,
            files=[MutationFileResult(Path("C:/LaunchBox/NES.xml"), MutationState.ROLLED_BACK)],
            manifest_path=Path("C:/LaunchBox/Backups/run/manifest.json"),
        )

        with patch("launchbox_tools.gui.app.load_platforms", return_value=[]):
            with patch("launchbox_tools.gui.app.ensure_safe_to_mutate"):
                with patch("launchbox_tools.gui.app.messagebox.askyesno", return_value=True):
                    with patch(
                        "launchbox_tools.gui.app.run_path_replacement",
                        return_value=run_result,
                    ):
                        with patch(
                            "launchbox_tools.gui.app.write_path_replacement_reports",
                            side_effect=RuntimeError("report bug"),
                        ):
                            app.run_path_replacement_operation(True)

        joined = "\n".join(messages)
        self.assertIn("Outcome: Rolled back", joined)
        self.assertIn("Rolled-back XML files: 1", joined)
        self.assertIn("Report error: report bug", joined)
        self.assertIn("Traceback (most recent call last)", joined)
        self.assertIn("RuntimeError: report bug", joined)
        self.assertNotIn("Reports written to", joined)

    def test_gui_language_button_toggles_and_saves_language(self) -> None:
        import tkinter as tk

        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        root = None
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk is not available: {exc}")

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "launchbox_utils.ini"
                save_interface_language(config_path, "ru")

                app = LaunchBoxUtilsApp(root, config_path)
                root.update_idletasks()

                self.assertIsNotNone(app.language_button)
                self.assertEqual(app.language_button.cget("text"), "RU")

                app.language_button.invoke()
                root.update_idletasks()

                self.assertEqual(app.language, "en")
                self.assertEqual(app.language_button.cget("text"), "EN")
                self.assertEqual(load_configured_language(config_path), "en")
        finally:
            if root is not None:
                root.destroy()

    def test_gui_hides_planned_operations(self) -> None:
        import tkinter as tk

        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        root = None
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk is not available: {exc}")

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "launchbox_utils.ini"

                app = LaunchBoxUtilsApp(root, config_path)
                root.update_idletasks()

                self.assertEqual(list(app.operation_buttons), ["audit", "dedupe", "replace_paths"])
                self.assertNotIn("restore_main_files", app.operation_buttons)
                self.assertNotIn("export_favorites", app.operation_buttons)

                app.show_operation("restore_main_files")
                root.update_idletasks()

                self.assertEqual(app.current_operation_key.get(), "audit")
        finally:
            if root is not None:
                root.destroy()

    def test_cli_parser_supports_gui_command(self) -> None:
        args = build_arg_parser().parse_args(["gui"])
        self.assertEqual(args.command, "gui")

    def test_cli_parser_supports_replace_paths_command(self) -> None:
        args = build_arg_parser().parse_args(
            ["replace-paths", "--old", r"C:\OldRoms", "--new", r"D:\NewRoms", "--apply", "--platform", "NES"]
        )
        self.assertEqual(args.command, "replace-paths")
        self.assertEqual(args.old, r"C:\OldRoms")
        self.assertEqual(args.new, r"D:\NewRoms")
        self.assertTrue(args.apply)
        self.assertEqual(args.platform, "NES")

    def test_cli_returns_nonzero_for_partial_mutation_outcome(self) -> None:
        config = AppConfig(Path("C:/LaunchBox"), Path("C:/Reports"), Path("config.ini"))
        run_result = MutationRunResult([], MutationOutcome.PARTIAL)

        with patch("launchbox_tools.cli.load_app_config", return_value=config):
            with patch("launchbox_tools.cli.run_additional_apps_dedupe", return_value=run_result):
                with patch("launchbox_tools.cli.write_dedupe_reports"):
                    with patch("builtins.print"):
                        exit_code = main(["dedupe-additional-apps", "--apply"])

        self.assertEqual(exit_code, 1)

    def test_cli_returns_nonzero_for_manifest_error_without_changing_success_outcome(self) -> None:
        config = AppConfig(Path("C:/LaunchBox"), Path("C:/Reports"), Path("config.ini"))
        run_result = MutationRunResult([], MutationOutcome.SUCCESS, manifest_error="manifest denied")

        with patch("launchbox_tools.cli.load_app_config", return_value=config):
            with patch("launchbox_tools.cli.run_additional_apps_dedupe", return_value=run_result):
                with patch("launchbox_tools.cli.write_dedupe_reports"):
                    with patch("builtins.print"):
                        exit_code = main(["dedupe-additional-apps", "--apply"])

        self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
        self.assertEqual(exit_code, 1)

    def test_cli_preserves_dedupe_outcome_when_report_write_fails(self) -> None:
        config = AppConfig(Path("C:/LaunchBox"), Path("C:/Reports"), Path("config.ini"))
        run_result = MutationRunResult(
            [],
            MutationOutcome.SUCCESS,
            files=[MutationFileResult(Path("C:/LaunchBox/NES.xml"), MutationState.COMMITTED)],
            manifest_path=Path("C:/LaunchBox/Backups/run/manifest.json"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("launchbox_tools.cli.load_app_config", return_value=config):
            with patch("launchbox_tools.cli.run_additional_apps_dedupe", return_value=run_result):
                with patch("launchbox_tools.cli.write_dedupe_reports", side_effect=OSError()):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        exit_code = main(["dedupe-additional-apps", "--apply"])

        self.assertEqual(exit_code, 1)
        self.assertIn("Outcome: success", stdout.getvalue())
        self.assertIn("XML files committed: 1", stdout.getvalue())
        self.assertIn("Manifest: C:\\LaunchBox\\Backups\\run\\manifest.json", stdout.getvalue())
        self.assertNotIn("Reports written to", stdout.getvalue())
        self.assertIn("Report error: OSError", stderr.getvalue())

    def test_cli_preserves_path_replacement_outcome_when_report_write_fails(self) -> None:
        config = AppConfig(Path("C:/LaunchBox"), Path("C:/Reports"), Path("config.ini"))
        run_result = MutationRunResult(
            [],
            MutationOutcome.ROLLED_BACK,
            files=[MutationFileResult(Path("C:/LaunchBox/NES.xml"), MutationState.ROLLED_BACK)],
            manifest_path=Path("C:/LaunchBox/Backups/run/manifest.json"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("launchbox_tools.cli.load_app_config", return_value=config):
            with patch("launchbox_tools.cli.run_path_replacement", return_value=run_result):
                with patch(
                    "launchbox_tools.cli.write_path_replacement_reports",
                    side_effect=RuntimeError("report bug"),
                ):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        exit_code = main(
                            ["replace-paths", "--old", "C:/Old", "--new", "C:/New", "--apply"]
                        )

        self.assertEqual(exit_code, 1)
        self.assertIn("Outcome: rolled_back", stdout.getvalue())
        self.assertIn("XML files rolled_back: 1", stdout.getvalue())
        self.assertIn("Manifest: C:\\LaunchBox\\Backups\\run\\manifest.json", stdout.getvalue())
        self.assertNotIn("Reports written to", stdout.getvalue())
        self.assertIn("Report error: report bug", stderr.getvalue())
        self.assertIn("Traceback (most recent call last)", stderr.getvalue())
        self.assertIn("RuntimeError: report bug", stderr.getvalue())

    def test_resolve_command_uses_gui_for_packaged_gui_exe_without_args(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", r"C:\LaunchBoxUtils\LaunchBoxUtils.exe"
        ):
            self.assertEqual(_resolve_command(parser, args, []), "gui")

    def test_resolve_command_prints_help_for_packaged_cli_exe_without_args(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", r"C:\LaunchBoxUtils\LaunchBoxUtils-cli.exe"
        ), patch.object(parser, "print_help") as print_help:
            self.assertIsNone(_resolve_command(parser, args, []))
            print_help.assert_called_once()

    def test_resolve_command_uses_audit_for_source_without_args(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(_resolve_command(parser, args, []), "audit")

    def test_main_exits_zero_when_packaged_cli_exe_runs_without_args(self) -> None:
        parser = build_arg_parser()
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", r"C:\LaunchBoxUtils\LaunchBoxUtils-cli.exe"
        ), patch.object(parser, "print_help"):
            with patch("launchbox_tools.cli.build_arg_parser", return_value=parser):
                self.assertEqual(main([]), 0)

    def test_build_replacement_value_preserves_absolute_paths(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            old_path = root / "OldRoms"
            new_path = root / "NewRoms"

            new_value, error = build_replacement_value(root, old_path, new_path, str(old_path / "NES" / "game.zip"))

            self.assertIsNone(error)
            self.assertEqual(new_value, str(new_path / "NES" / "game.zip"))

    def test_build_replacement_value_preserves_relative_paths_and_forward_slashes(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)

            new_value, error = build_replacement_value(
                root,
                root / "Games" / "NES",
                root / "Games" / "SNES",
                "Games/NES/game.zip",
            )

            self.assertIsNone(error)
            self.assertEqual(new_value, "Games/SNES/game.zip")

    def test_build_replacement_value_does_not_match_similar_prefix(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)

            new_value, error = build_replacement_value(
                root,
                root / "Games" / "ROM",
                root / "Games" / "NewROM",
                "Games/ROMs/game.zip",
            )

            self.assertIsNone(new_value)
            self.assertIsNone(error)

    def test_path_replacement_dry_run_reports_without_changing_xml(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            before = xml_path.read_text(encoding="utf-8")

            results = run_path_replacement(root, root / "Games" / "NES", root / "Games" / "SNES")

            self.assertEqual(results.outcome, MutationOutcome.DRY_RUN)
            self.assertEqual(sum(len(result.replacements) for result in results.results), 2)
            self.assertEqual(xml_path.read_text(encoding="utf-8"), before)
            self.assertTrue(all(file.state == MutationState.PLANNED for file in results.files))
            self.assertTrue(
                all(
                    replacement.state == MutationState.PLANNED
                    for result in results.results
                    for replacement in result.replacements
                )
            )

    def test_path_replacement_apply_updates_entries_platform_folder_and_creates_backups(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                """  <Game>
    <Title>Main Game</Title>
    <ApplicationPath>Games/NES/main.zip</ApplicationPath>
  </Game>
  <AdditionalApplication>
    <GameID>game-1</GameID>
    <Name>Extra</Name>
    <ApplicationPath>Games/NES/extra.zip</ApplicationPath>
  </AdditionalApplication>""",
            )

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                results = run_path_replacement(root, root / "Games" / "NES", root / "Games" / "SNES", apply_changes=True)

            platforms_xml = parse_xml(root / "Data" / "Platforms.xml")
            self.assertEqual(child_text(next(platforms_xml.iter("Platform")), "Folder"), "Games/SNES")
            platform_xml = parse_xml(root / "Data" / "Platforms" / "Nintendo Entertainment System.xml")
            paths = [child_text(element, "ApplicationPath") for element in platform_xml if local_name(element.tag) in {"Game", "AdditionalApplication"}]
            self.assertEqual(paths, ["Games/SNES/main.zip", "Games/SNES/extra.zip"])
            self.assertEqual(results.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(sum(len(result.replacements) for result in results.results), 3)
            self.assertTrue(any(result.backup_paths for result in results.results))
            self.assertTrue(all(file.state == MutationState.COMMITTED for file in results.files))
            self.assertTrue(results.manifest_path.is_file())
            manifest = json.loads(results.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "success")
            self.assertEqual({item["state"] for item in manifest["files"]}, {"committed"})
            self.assertEqual({item["state"] for item in manifest["changes"]}, {"committed"})

    def test_path_replacement_apply_without_changes_still_writes_manifest(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                run_result = run_path_replacement(
                    root,
                    root / "Games" / "Missing",
                    root / "Games" / "New",
                    apply_changes=True,
                )

            self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(run_result.files, [])
            self.assertTrue(run_result.manifest_path.is_file())
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"], [])
            self.assertEqual(manifest["changes"], [])

    def test_path_replacement_same_second_apply_uses_distinct_backup_roots(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            fixed_datetime = patch("launchbox_tools.operations.path_replacement.datetime")

            with fixed_datetime as datetime_mock:
                datetime_mock.now.return_value.strftime.return_value = "20260712-120000"
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    first = run_path_replacement(
                        root,
                        root / "Games" / "Missing",
                        root / "Games" / "New",
                        apply_changes=True,
                    )
                    first_manifest = first.manifest_path.read_text(encoding="utf-8")
                    second = run_path_replacement(
                        root,
                        root / "Games" / "Missing",
                        root / "Games" / "New",
                        apply_changes=True,
                    )

            self.assertEqual(first.manifest_path.parent.name, "PathReplacement-20260712-120000")
            self.assertEqual(second.manifest_path.parent.name, "PathReplacement-20260712-120000-2")
            self.assertEqual(first.manifest_path.read_text(encoding="utf-8"), first_manifest)
            self.assertTrue(second.manifest_path.is_file())

    def test_manifest_write_failure_does_not_change_committed_mutation_state(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.mutation_manifest.Path.write_text",
                    side_effect=OSError("manifest denied"),
                ):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "NES",
                        root / "Games" / "SNES",
                        apply_changes=True,
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
            self.assertTrue(all(file.state == MutationState.COMMITTED for file in run_result.files))
            self.assertEqual(run_result.manifest_error, "manifest denied")
            self.assertIsNone(run_result.manifest_path)
            self.assertFalse(list(root.rglob("manifest.json.tmp")))
            self.assertFalse(list(root.rglob("manifest.json")))

    def test_manifest_write_failure_without_message_uses_exception_type(self) -> None:
        run_result = MutationRunResult([], MutationOutcome.SUCCESS)

        with patch("launchbox_tools.mutation_manifest.Path.mkdir"):
            with patch(
                "launchbox_tools.mutation_manifest.Path.write_text",
                side_effect=OSError(),
            ):
                with patch("launchbox_tools.mutation_manifest.Path.unlink"):
                    write_mutation_manifest(run_result, Path("C:/Backups/run"), "test_operation", [])

        self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
        self.assertEqual(run_result.manifest_error, "OSError")
        self.assertIsNone(run_result.manifest_path)

    def test_manifest_replace_failure_preserves_existing_manifest_and_cleans_temp(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "Data" / "Backups" / "run"
            backup_root.mkdir(parents=True)
            manifest_path = backup_root / "manifest.json"
            manifest_path.write_text("previous manifest\n", encoding="utf-8")
            run_result = MutationRunResult(
                [],
                MutationOutcome.SUCCESS,
                files=[MutationFileResult(root / "database.xml", MutationState.COMMITTED)],
            )

            with patch(
                "launchbox_tools.mutation_manifest.os.replace",
                side_effect=OSError("manifest replace denied"),
            ):
                write_mutation_manifest(run_result, backup_root, "test_operation", [])

            self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(run_result.files[0].state, MutationState.COMMITTED)
            self.assertEqual(run_result.manifest_error, "manifest replace denied")
            self.assertIsNone(run_result.manifest_path)
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), "previous manifest\n")
            self.assertFalse((backup_root / "manifest.json.tmp").exists())

    def test_path_replacement_rolls_back_all_xml_after_late_commit_failure(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platforms_xml = root / "Data" / "Platforms.xml"
            games_xml = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            originals = {platforms_xml: platforms_xml.read_bytes(), games_xml: games_xml.read_bytes()}
            commit_calls = 0

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated late commit failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=fail_second_commit):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "NES",
                        root / "Games" / "SNES",
                        apply_changes=True,
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.ROLLED_BACK)
            self.assertEqual(platforms_xml.read_bytes(), originals[platforms_xml])
            self.assertEqual(games_xml.read_bytes(), originals[games_xml])
            self.assertEqual(
                {file.state for file in run_result.files},
                {MutationState.ROLLED_BACK, MutationState.FAILED},
            )
            self.assertFalse(
                any(
                    replacement.state == MutationState.COMMITTED
                    for result in run_result.results
                    for replacement in result.replacements
                )
            )
            self.assertTrue(any(result.backup_paths for result in run_result.results))
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "rolled_back")
            self.assertEqual(
                {item["state"] for item in manifest["files"]},
                {"rolled_back", "failed"},
            )

    def test_path_replacement_rolls_back_platforms_xml_name_collision(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_xml = root / "Data" / "Platforms.xml"
            platform_xml = root / "Data" / "Platforms" / "Platforms.xml"
            platforms_xml.write_text(
                "<ArrayOfPlatform><Platform><Name>Platforms</Name><Folder>Games/Old</Folder>"
                "</Platform></ArrayOfPlatform>",
                encoding="utf-8",
            )
            platform_xml.write_text(
                "<LaunchBox><Game><Title>Game</Title><ApplicationPath>Games/Old/game.zip</ApplicationPath>"
                "</Game></LaunchBox>",
                encoding="utf-8",
            )
            originals = {
                platforms_xml: platforms_xml.read_bytes(),
                platform_xml: platform_xml.read_bytes(),
            }
            commit_calls = 0

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=fail_second_commit):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "Old",
                        root / "Games" / "New",
                        apply_changes=True,
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.ROLLED_BACK)
            self.assertEqual(platforms_xml.read_bytes(), originals[platforms_xml])
            self.assertEqual(platform_xml.read_bytes(), originals[platform_xml])
            self.assertEqual(
                {file.state for file in run_result.files},
                {MutationState.ROLLED_BACK, MutationState.FAILED},
            )
            self.assertFalse(
                any(
                    replacement.state == MutationState.COMMITTED
                    for result in run_result.results
                    for replacement in result.replacements
                )
            )
            backup_paths = [path for result in run_result.results for path in result.backup_paths]
            self.assertEqual(len(backup_paths), 2)
            self.assertEqual(len(set(backup_paths)), 2)
            self.assertEqual({path.read_bytes() for path in backup_paths}, set(originals.values()))
            self.assertFalse(list(root.rglob("*.stage.tmp")))
            self.assertFalse(list(root.rglob("*.rollback.tmp")))

    def test_path_replacement_can_filter_platform(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_two_platforms_xml(root)
            self.write_platform_games_xml_raw(
                root,
                "Nintendo Entertainment System",
                """  <Game>
    <Title>NES Game</Title>
    <ApplicationPath>Games/NES/game.zip</ApplicationPath>
  </Game>""",
            )
            self.write_platform_games_xml_raw(
                root,
                "Sega Genesis",
                """  <Game>
    <Title>Genesis Game</Title>
    <ApplicationPath>Games/Genesis/game.zip</ApplicationPath>
  </Game>""",
            )

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                results = run_path_replacement(
                    root,
                    root / "Games" / "NES",
                    root / "Games" / "NewNES",
                    platform_filter="Nintendo Entertainment System",
                    apply_changes=True,
                )

            self.assertEqual(len(results.results), 1)
            genesis_xml = parse_xml(root / "Data" / "Platforms" / "Sega Genesis.xml")
            self.assertEqual(child_text(next(genesis_xml.iter("Game")), "ApplicationPath"), "Games/Genesis/game.zip")

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
    <Name>Keep this version</Name>
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
            self.assertEqual(results.outcome, MutationOutcome.DRY_RUN)
            self.assertEqual(len(results.results[0].duplicates), 1)

            platform_report = output_dir / "Nintendo Entertainment System" / "duplicate_additional_apps.txt"
            report_text = platform_report.read_text(encoding="utf-8")
            self.assertIn("Mode: dry-run", report_text)
            self.assertIn("Keep this version", report_text)

            summary_text = (output_dir / "duplicate_additional_apps.csv").read_text(encoding="utf-8-sig")
            self.assertTrue(summary_text.startswith("sep=;\n"))
            self.assertIn("outcome", summary_text)
            self.assertIn("state", summary_text)
            self.assertNotIn("applied", summary_text.casefold())
            rows = list(csv.DictReader(summary_text.splitlines()[1:], delimiter=";"))
            self.assertTrue(rows)
            self.assertEqual(rows[0]["state"], "planned")
            self.assertIn("Keep this version", summary_text)

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
    <Name>Keep this version</Name>
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

            self.assertEqual(results.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(results.results[0].state, MutationState.COMMITTED)
            self.assertTrue(all(duplicate.state == MutationState.COMMITTED for duplicate in results.results[0].duplicates))
            self.assertIsNotNone(results.results[0].backup_path)
            self.assertTrue(results.results[0].backup_path.exists())
            manifest = json.loads(results.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["operation"], "dedupe_additional_apps")
            self.assertEqual(manifest["outcome"], "success")
            self.assertEqual(manifest["changes"][0]["state"], "committed")

            xml_root = parse_xml(root / "Data" / "Platforms" / "Nintendo Entertainment System.xml")
            additional_apps = [element for element in xml_root if local_name(element.tag) == "AdditionalApplication"]
            names = [child_text(element, "Name") for element in additional_apps]
            self.assertEqual(len(additional_apps), 2)
            self.assertIn("Keep this version", names)
            self.assertIn("Keep different game", names)

    def test_dedupe_same_second_apply_uses_distinct_backup_roots(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(root, "")
            fixed_datetime = patch("launchbox_tools.operations.dedupe_additional_apps.datetime")

            with fixed_datetime as datetime_mock:
                datetime_mock.now.return_value.strftime.return_value = "20260712-120000"
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    first = run_additional_apps_dedupe(root, apply_changes=True)
                    first_manifest = first.manifest_path.read_text(encoding="utf-8")
                    second = run_additional_apps_dedupe(root, apply_changes=True)

            self.assertEqual(first.manifest_path.parent.name, "AdditionalAppsDedupe-20260712-120000")
            self.assertEqual(second.manifest_path.parent.name, "AdditionalAppsDedupe-20260712-120000-2")
            self.assertEqual(first.manifest_path.read_text(encoding="utf-8"), first_manifest)
            self.assertTrue(second.manifest_path.is_file())

    def test_dedupe_propagates_rolled_back_file_state_to_duplicates_and_manifest(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                self.duplicate_additional_app_xml(
                    "game-1", "Keep", "Remove", "Games/NES/duplicate.zip"
                ),
            )
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            backup_path = root / "Data" / "Backups" / "fake.xml"
            transaction = XmlTransactionResult(
                MutationOutcome.ROLLED_BACK,
                {xml_path.resolve(strict=False): backup_path},
                "simulated commit failure",
                files=[
                    MutationFileResult(
                        xml_path.resolve(strict=False),
                        MutationState.ROLLED_BACK,
                        backup_path,
                    )
                ],
            )

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.operations.dedupe_additional_apps.execute_xml_transaction",
                    return_value=transaction,
                ):
                    run_result = run_additional_apps_dedupe(root, apply_changes=True)

            self.assertEqual(run_result.outcome, MutationOutcome.ROLLED_BACK)
            self.assertEqual(run_result.results[0].state, MutationState.ROLLED_BACK)
            self.assertEqual(run_result.results[0].duplicates[0].state, MutationState.ROLLED_BACK)
            self.assertEqual(run_result.results[0].duplicates[0].error, "simulated commit failure")
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "rolled_back")
            self.assertEqual(manifest["changes"][0]["state"], "rolled_back")
            self.assertEqual(manifest["changes"][0]["error"], "simulated commit failure")

    def test_dedupe_prepared_change_repeats_transaction_error_in_report_and_manifest(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                self.duplicate_additional_app_xml(
                    "game-1", "Keep", "Remove", "Games/NES/duplicate.zip"
                ),
            )
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            transaction = XmlTransactionResult(
                MutationOutcome.FAILED,
                error="precommit check failed",
                files=[
                    MutationFileResult(
                        xml_path.resolve(strict=False),
                        MutationState.PREPARED,
                    )
                ],
            )

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.operations.dedupe_additional_apps.execute_xml_transaction",
                    return_value=transaction,
                ):
                    run_result = run_additional_apps_dedupe(root, apply_changes=True)

            output_dir = root / "Reports"
            write_dedupe_reports(run_result, output_dir, apply_changes=True)
            rows = list(
                csv.DictReader(
                    (output_dir / "duplicate_additional_apps.csv")
                    .read_text(encoding="utf-8-sig")
                    .splitlines()[1:],
                    delimiter=";",
                )
            )
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(run_result.results[0].duplicates[0].state, MutationState.PREPARED)
            self.assertEqual(run_result.results[0].duplicates[0].error, "precommit check failed")
            self.assertEqual(rows[0]["state"], "prepared")
            self.assertEqual(rows[0]["error"], "precommit check failed")
            self.assertEqual(manifest["changes"][0]["state"], "prepared")
            self.assertEqual(manifest["changes"][0]["error"], "precommit check failed")

    def test_conservative_dedupe_fixture_dry_run_reports_duplicates_and_ambiguities(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            fixture_path = Path(__file__).parent / "fixtures" / "conservative_dedupe.xml"
            xml_path.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
            before = xml_path.read_text(encoding="utf-8")

            run_result = run_additional_apps_dedupe(root, apply_changes=False)
            result = run_result.results[0]
            output_dir = root / "AuditReports"
            write_dedupe_reports(run_result, output_dir, apply_changes=False)

            self.assertEqual(xml_path.read_text(encoding="utf-8"), before)
            self.assertEqual(len(result.duplicates), 3)
            self.assertEqual(len(result.ambiguities), 12)
            ambiguities = {ambiguity.key[0]: ambiguity for ambiguity in result.ambiguities}
            duplicate_keys = {duplicate.key[0] for duplicate in result.duplicates}
            self.assertEqual(ambiguities["command-whitespace"].differing_fields, ("CommandLine",))
            self.assertEqual(ambiguities["future-whitespace"].differing_fields, ("FutureSetting",))
            self.assertEqual(ambiguities["attribute-whitespace"].differing_fields, ("@attributes",))
            self.assertEqual(ambiguities["future-boolean"].differing_fields, ("FutureBoolean",))
            self.assertEqual(ambiguities["mixed-content"].differing_fields, ("FutureSetting",))
            self.assertEqual(ambiguities["repeated-path"].differing_fields, ("ApplicationPath",))
            self.assertEqual(
                sum(duplicate.key[0] == "repeated-path" for duplicate in result.duplicates),
                1,
            )
            self.assertTrue(
                {
                    "command-whitespace",
                    "future-whitespace",
                    "attribute-whitespace",
                    "future-boolean",
                    "mixed-content",
                }.isdisjoint(
                    duplicate_keys
                )
            )
            fields = {field for ambiguity in result.ambiguities for field in ambiguity.differing_fields}
            self.assertTrue(
                {"CommandLine", "AutoRunBefore", "AutoRunAfter", "EmulatorId", "Name", "FutureSetting"} <= fields
            )
            report = (output_dir / "Nintendo Entertainment System" / "duplicate_additional_apps.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("Ambiguous AdditionalApplication groups:", report)
            self.assertIn("Differing fields: CommandLine", report)
            summary = (output_dir / "duplicate_additional_apps.csv").read_text(encoding="utf-8-sig")
            self.assertIn(";duplicate;", summary)
            self.assertIn(";ambiguous;", summary)

    def test_conservative_dedupe_fixture_apply_removes_only_canonical_duplicates(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            fixture_path = Path(__file__).parent / "fixtures" / "conservative_dedupe.xml"
            xml_path.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                run_result = run_additional_apps_dedupe(root, apply_changes=True)
                result = run_result.results[0]

            remaining = [element for element in parse_xml(xml_path) if local_name(element.tag) == "AdditionalApplication"]
            self.assertEqual(result.state, MutationState.COMMITTED)
            self.assertIsNotNone(result.backup_path)
            self.assertTrue(result.backup_path.exists())
            self.assertEqual(len(result.duplicates), 3)
            self.assertEqual(len(result.ambiguities), 12)
            self.assertEqual(len(remaining), 25)
            self.assertEqual(sum(child_text(element, "GameID") == "mixed" for element in remaining), 2)
            command_lines = [
                child_text(element, "CommandLine")
                for element in remaining
                if child_text(element, "GameID") == "command-whitespace"
            ]
            self.assertEqual(command_lines, ['--label="a  b"', '--label="a b"'])
            future_values = [
                child_text(element, "FutureSetting")
                for element in remaining
                if child_text(element, "GameID") == "future-whitespace"
            ]
            self.assertEqual(future_values, ["alpha  beta", "alpha beta"])
            attribute_values = [
                element.attrib["data-mode"]
                for element in remaining
                if child_text(element, "GameID") == "attribute-whitespace"
            ]
            self.assertEqual(attribute_values, ["alpha  beta", "alpha beta"])
            future_booleans = [
                child_text(element, "FutureBoolean")
                for element in remaining
                if child_text(element, "GameID") == "future-boolean"
            ]
            self.assertEqual(future_booleans, ["TRUE", "true"])
            repeated_paths = [
                [
                    child.text or ""
                    for child in element
                    if local_name(child.tag) == "ApplicationPath"
                ]
                for element in remaining
                if child_text(element, "GameID") == "repeated-path"
            ]
            self.assertEqual(
                repeated_paths,
                [
                    ["Games/NES/repeated-main.zip", "Games/NES/variant-a.zip"],
                    ["Games/NES/repeated-main.zip", "Games/NES/variant-b.zip"],
                ],
            )

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

            self.assertEqual(len(results.results[0].duplicates), 1)
            self.assertEqual(results.results[0].state, MutationState.PLANNED)

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

            self.assertEqual(results.results, [])

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

            def flaky_commit(stage_path: Path, destination: Path) -> None:
                nonlocal write_calls
                write_calls += 1
                if write_calls == 2:
                    raise OSError("simulated write failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=flaky_commit):
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    results = run_additional_apps_dedupe(root, apply_changes=True)

            self.assertEqual(results.outcome, MutationOutcome.PARTIAL)
            self.assertEqual(len(results.results), 2)
            self.assertEqual(results.results[0].state, MutationState.COMMITTED)
            self.assertIsNone(results.results[0].error)
            self.assertEqual(results.results[1].state, MutationState.FAILED)
            self.assertEqual(results.results[1].error, "simulated write failure")
            self.assertEqual(results.results[1].duplicates[0].error, "simulated write failure")
            manifest = json.loads(results.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "partial")
            self.assertEqual(
                {item["state"] for item in manifest["files"]},
                {"committed", "failed"},
            )
            failed_change = next(item for item in manifest["changes"] if item["state"] == "failed")
            self.assertEqual(failed_change["error"], "simulated write failure")

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

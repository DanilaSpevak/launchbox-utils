import csv
import json
from pathlib import Path
from unittest.mock import patch
from launchbox_tools.operations.dedupe_additional_apps import run_additional_apps_dedupe
from launchbox_tools.runtime_checks import MutationBlockedError
from launchbox_tools.models import MutationFileResult, MutationOutcome, MutationState
from launchbox_tools.reports.dedupe_reports import write_dedupe_reports
from launchbox_tools.safe_write import XmlTransactionResult
from launchbox_tools.xml_repository import child_text, local_name, parse_xml

from test.support import LaunchBoxTestCase


class DedupeAdditionalAppsTests(LaunchBoxTestCase):
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

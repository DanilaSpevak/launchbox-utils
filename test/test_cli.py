import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch
from launchbox_tools.cli import _resolve_command, build_arg_parser, main
from launchbox_tools.config import AppConfig
from launchbox_tools.models import MutationFileResult, MutationOutcome, MutationRunResult, MutationState

from test.support import LaunchBoxTestCase


class CliTests(LaunchBoxTestCase):
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

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from launchbox_tools.config import load_configured_language, save_interface_language
from launchbox_tools.gui.translations import translate
from launchbox_tools.models import MutationFileResult, MutationOutcome, MutationRunResult, MutationState

from test.support import LaunchBoxTestCase


class GuiTests(LaunchBoxTestCase):
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

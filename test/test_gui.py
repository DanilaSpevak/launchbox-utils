import tempfile
import queue
import threading
from pathlib import Path
from unittest.mock import Mock, patch
from launchbox_tools.config import load_configured_language, save_interface_language
from launchbox_tools.gui.translations import translate
from launchbox_tools.models import MutationFileResult, MutationOutcome, MutationRunResult, MutationState
from launchbox_tools.operation_lifecycle import OperationCancelled, OperationControl, OperationPhase
from launchbox_tools.runtime_checks import MutationBlockedError

from test.support import LaunchBoxTestCase


class GuiTests(LaunchBoxTestCase):
    def test_gui_worker_logs_busy_mutation_lock_without_traceback(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        messages: list[str] = []
        app.validate_paths = Mock(return_value=(Path("C:/LaunchBox"), Path("C:/Reports")))
        app.audit_output_mode_var = Mock()
        app.audit_output_mode_var.get.return_value = "all"
        app.enqueue_log = messages.append
        app.t = lambda key: translate("en", key)
        app.start_worker = lambda _message, worker: worker(OperationControl())
        error = MutationBlockedError(
            "busy",
            reason="mutation_in_progress",
            active_operation="replace_paths",
            active_run_id="active-run",
            active_pid=123,
            active_started_at="2026-07-12T12:00:00Z",
        )

        with patch("launchbox_tools.gui.app.load_platforms", return_value=[]):
            with patch("launchbox_tools.gui.app.ensure_safe_to_mutate"):
                with patch("launchbox_tools.gui.app.messagebox.askyesno", return_value=True):
                    with patch(
                        "launchbox_tools.gui.app.run_additional_apps_dedupe",
                        side_effect=error,
                    ):
                        app.run_dedupe_operation(True)

        joined = "\n".join(messages)
        self.assertIn("Another LaunchBox Utils mutation is already running", joined)
        self.assertIn("Operation: replace_paths", joined)
        self.assertIn("Run ID: active-run", joined)
        self.assertNotIn("Traceback", joined)

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
        self.assertEqual(translate("ru", "outcome_cancelled"), "Отменено")
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
        app.start_worker = lambda _message, worker: worker(OperationControl())
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
        app.start_worker = lambda _message, worker: worker(OperationControl())
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

    def test_gui_close_idle_destroys_window_immediately(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        app.worker = None
        app.root = Mock()

        app.on_close()

        app.root.destroy.assert_called_once_with()

    def test_gui_close_stage_requests_cancel_and_closes_after_worker_stops(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        app.operation_control = OperationControl()
        app.operation_control.set_phase(OperationPhase.STAGE)
        app.close_requested = False
        app.root = Mock()
        app.append_log = Mock()
        app.log_queue = queue.Queue()
        app.t = lambda key: translate("en", key)

        with patch("launchbox_tools.gui.app.messagebox.askyesno", return_value=True):
            app.on_close()

        self.assertTrue(app.close_requested)
        self.assertTrue(app.operation_control.snapshot().cancel_requested)
        app.root.destroy.assert_not_called()

        app.worker.is_alive.return_value = False
        app.process_log_queue()

        app.root.destroy.assert_called_once_with()
        app.root.after.assert_not_called()

    def test_gui_close_scan_can_keep_operation_running(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        app.operation_control = OperationControl()
        app.close_requested = False
        app.root = Mock()
        app.t = lambda key: translate("en", key)

        with patch("launchbox_tools.gui.app.messagebox.askyesno", return_value=False):
            app.on_close()

        self.assertFalse(app.close_requested)
        self.assertFalse(app.operation_control.snapshot().cancel_requested)
        app.root.destroy.assert_not_called()

    def test_gui_close_is_deferred_during_protected_phases(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        for phase in (OperationPhase.COMMIT, OperationPhase.ROLLBACK, OperationPhase.FINALIZE):
            with self.subTest(phase=phase):
                app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
                app.worker = Mock()
                app.worker.is_alive.return_value = True
                app.operation_control = OperationControl()
                if phase != OperationPhase.FINALIZE:
                    app.operation_control.begin_commit()
                app.operation_control.set_phase(phase)
                app.close_requested = False
                app.root = Mock()
                app.append_log = Mock()
                app.log_queue = queue.Queue()
                app.t = lambda key: translate("en", key)

                with patch("launchbox_tools.gui.app.messagebox.showinfo") as showinfo:
                    app.on_close()

                showinfo.assert_called_once()
                self.assertTrue(app.close_requested)
                self.assertFalse(app.operation_control.request_cancel())
                app.root.destroy.assert_not_called()

                app.worker.is_alive.return_value = False
                app.process_log_queue()

                app.root.destroy.assert_called_once_with()
                app.root.after.assert_not_called()

    def test_gui_close_destroys_window_if_worker_finishes_during_confirmation(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        app.operation_control = OperationControl()
        app.close_requested = False
        app.root = Mock()
        app.append_log = Mock()
        app.t = lambda key: translate("en", key)

        def finish_worker(*_args) -> bool:
            app.operation_control.finish()
            app.worker.is_alive.return_value = False
            return True

        with patch("launchbox_tools.gui.app.messagebox.askyesno", side_effect=finish_worker):
            with patch("launchbox_tools.gui.app.messagebox.showinfo") as showinfo:
                app.on_close()

        self.assertTrue(app.close_requested)
        app.root.destroy.assert_called_once_with()
        showinfo.assert_not_called()

    def test_gui_does_not_start_worker_after_close_requested(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        app.close_requested = True
        app.worker = None
        app.t = lambda key: translate("en", key)

        with patch("launchbox_tools.gui.app.messagebox.showinfo") as showinfo:
            app.start_worker("start", Mock())

        showinfo.assert_called_once()
        self.assertIsNone(app.worker)

    def test_gui_worker_is_non_daemon(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        release = threading.Event()
        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        app.worker = None
        app.operation_control = None
        app.close_requested = False
        app.save_config = Mock()
        app.append_log = Mock()
        app.t = lambda key: translate("en", key)

        app.start_worker("start", lambda _control: release.wait(timeout=2))
        try:
            self.assertIsNotNone(app.worker)
            self.assertFalse(app.worker.daemon)
        finally:
            release.set()
            app.worker.join(timeout=2)

        self.assertFalse(app.worker.is_alive())

    def test_gui_cancelled_audit_skips_reports_without_traceback(self) -> None:
        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        app = LaunchBoxUtilsApp.__new__(LaunchBoxUtilsApp)
        messages: list[str] = []
        app.validate_paths = Mock(return_value=(Path("C:/LaunchBox"), Path("C:/Reports")))
        app.audit_output_mode_var = Mock()
        app.audit_output_mode_var.get.return_value = "all"
        app.enqueue_log = messages.append
        app.t = lambda key: translate("en", key)
        app.start_worker = lambda _message, worker: worker(OperationControl())

        with patch("launchbox_tools.gui.app.run_audit", side_effect=OperationCancelled()):
            with patch("launchbox_tools.gui.app.write_reports") as write_reports:
                app.run_audit_operation()

        write_reports.assert_not_called()
        self.assertIn("Cancelled", messages)
        self.assertNotIn("Traceback", "\n".join(messages))

    def test_gui_real_tk_close_lifecycle_for_all_phases(self) -> None:
        import tkinter as tk

        from launchbox_tools.gui.app import LaunchBoxUtilsApp

        for phase in (
            OperationPhase.SCAN,
            OperationPhase.STAGE,
            OperationPhase.COMMIT,
            OperationPhase.ROLLBACK,
            OperationPhase.FINALIZE,
        ):
            with self.subTest(phase=phase):
                root = None
                release = threading.Event()
                app = None
                try:
                    try:
                        root = tk.Tk()
                    except tk.TclError as exc:
                        self.skipTest(f"Tk is not available: {exc}")
                    root.withdraw()

                    with tempfile.TemporaryDirectory() as temp_dir:
                        app = LaunchBoxUtilsApp(root, Path(temp_dir) / "launchbox_utils.ini")
                        root.update_idletasks()
                        self.assertTrue(root.protocol("WM_DELETE_WINDOW"))
                        started = threading.Event()

                        def worker(control: OperationControl) -> None:
                            if phase in {OperationPhase.COMMIT, OperationPhase.ROLLBACK}:
                                control.begin_commit()
                            control.set_phase(phase)
                            started.set()
                            if phase in {OperationPhase.SCAN, OperationPhase.STAGE}:
                                try:
                                    while not release.wait(0.005):
                                        control.checkpoint()
                                except OperationCancelled:
                                    return
                            else:
                                release.wait(timeout=2)

                        app.start_worker("start", worker)
                        self.assertTrue(started.wait(timeout=2))

                        if phase in {OperationPhase.SCAN, OperationPhase.STAGE}:
                            with patch("launchbox_tools.gui.app.messagebox.askyesno", return_value=True):
                                app.on_close()
                            self.assertTrue(app.operation_control.snapshot().cancel_requested)
                        else:
                            with patch("launchbox_tools.gui.app.messagebox.showinfo") as showinfo:
                                app.on_close()
                            showinfo.assert_called_once()
                            self.assertFalse(app.operation_control.request_cancel())
                            release.set()

                        self.assertTrue(app.close_requested)
                        app.worker.join(timeout=2)
                        self.assertFalse(app.worker.is_alive())
                        app.process_log_queue()
                        root = None
                finally:
                    release.set()
                    if app is not None and app.worker is not None and app.worker.is_alive():
                        app.worker.join(timeout=2)
                    if root is not None:
                        try:
                            root.destroy()
                        except tk.TclError:
                            pass

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

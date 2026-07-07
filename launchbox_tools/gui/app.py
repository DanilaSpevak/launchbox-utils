from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from ..config import (
    load_configured_only_with_findings,
    load_raw_path_config,
    resolve_initial_language,
    save_interface_language,
    save_raw_path_config,
)
from ..operations.audit import run_audit
from ..operations.dedupe_additional_apps import run_additional_apps_dedupe
from ..runtime_checks import MutationBlockedError, ensure_safe_to_mutate
from ..xml_repository import load_platforms
from ..reports.audit_reports import write_reports
from ..reports.dedupe_reports import write_dedupe_reports
from .translations import translate


class Tooltip:
    def __init__(self, widget: tk.Widget, text_provider, delay_ms: int = 500) -> None:
        self.widget = widget
        self.text_provider = text_provider
        self.delay_ms = delay_ms
        self.after_id: str | None = None
        self.window: tk.Toplevel | None = None

        self.widget.bind("<Enter>", self.schedule)
        self.widget.bind("<Leave>", self.hide)
        self.widget.bind("<ButtonPress>", self.hide)

    def schedule(self, _event: tk.Event | None = None) -> None:
        self.cancel()
        self.after_id = self.widget.after(self.delay_ms, self.show)

    def cancel(self) -> None:
        if self.after_id is not None:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def show(self) -> None:
        self.after_id = None
        text = self.text_provider()
        if not text or self.window is not None:
            return

        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")

        label = ttk.Label(self.window, text=text, padding=(6, 3), relief="solid", borderwidth=1)
        label.pack()

    def hide(self, _event: tk.Event | None = None) -> None:
        self.cancel()
        if self.window is not None:
            self.window.destroy()
            self.window = None


class LaunchBoxUtilsApp:
    def __init__(self, root: tk.Tk, config_path: Path) -> None:
        self.root = root
        self.config_path = config_path
        self.language = resolve_initial_language(config_path)
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.config_save_after_id: str | None = None
        self.tooltips: list[Tooltip] = []

        raw_config = load_raw_path_config(config_path)
        self.launchbox_root_var = tk.StringVar(value=raw_config.launchbox_root)
        self.output_dir_var = tk.StringVar(value=raw_config.output_dir)
        output_mode = "findings" if load_configured_only_with_findings(config_path) else "full"
        self.audit_output_mode_var = tk.StringVar(value=output_mode)

        self.translatable_widgets: dict[str, ttk.Widget | tk.Widget] = {}
        self.build_form()
        self.setup_config_autosave()
        self.apply_language()
        self.root.after(100, self.process_log_queue)

    def t(self, key: str) -> str:
        return translate(self.language, key)

    def build_form(self) -> None:
        self.root.title(self.t("app_title"))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(4, weight=1)

        language_frame = ttk.Frame(self.root, padding=(10, 10, 10, 0))
        language_frame.grid(row=0, column=0, sticky="ew")
        language_frame.columnconfigure(0, weight=1)

        ru_button = ttk.Button(language_frame, text="RU", width=6, command=lambda: self.set_language("ru"))
        ru_button.grid(row=0, column=1, padx=(5, 0))
        self.add_tooltip(ru_button, "interface_language_tooltip")
        en_button = ttk.Button(language_frame, text="EN", width=6, command=lambda: self.set_language("en"))
        en_button.grid(row=0, column=2, padx=(5, 0))
        self.add_tooltip(en_button, "interface_language_tooltip")

        paths_frame = ttk.LabelFrame(self.root, padding=10)
        paths_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=10)
        paths_frame.columnconfigure(1, weight=1)
        self.translatable_widgets["paths"] = paths_frame

        launchbox_label = ttk.Label(paths_frame)
        launchbox_label.grid(row=0, column=0, sticky="w")
        self.translatable_widgets["launchbox_folder"] = launchbox_label
        launchbox_entry = ttk.Entry(paths_frame, textvariable=self.launchbox_root_var)
        launchbox_entry.grid(row=0, column=1, sticky="ew", padx=5)
        launchbox_entry.bind("<FocusOut>", self.save_config_from_event)
        launchbox_entry.bind("<Return>", self.save_config_from_event)
        launchbox_button = ttk.Button(paths_frame, command=self.browse_launchbox_folder)
        launchbox_button.grid(row=0, column=2)
        launchbox_button.configure(text="📁", width=3)
        self.add_tooltip(launchbox_button, "browse_launchbox_tooltip")
        launchbox_open_button = ttk.Button(paths_frame, text="↗", width=3, command=lambda: self.open_folder(self.launchbox_root_var.get()))
        launchbox_open_button.grid(row=0, column=3, padx=(5, 0))
        self.add_tooltip(launchbox_open_button, "open_launchbox_tooltip")

        output_label = ttk.Label(paths_frame)
        output_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.translatable_widgets["output_folder"] = output_label
        output_entry = ttk.Entry(paths_frame, textvariable=self.output_dir_var)
        output_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=(8, 0))
        output_entry.bind("<FocusOut>", self.save_config_from_event)
        output_entry.bind("<Return>", self.save_config_from_event)
        output_button = ttk.Button(paths_frame, command=self.browse_output_folder)
        output_button.grid(row=1, column=2, pady=(8, 0))
        output_button.configure(text="📁", width=3)
        self.add_tooltip(output_button, "browse_output_tooltip")
        output_open_button = ttk.Button(paths_frame, text="↗", width=3, command=self.open_output_folder)
        output_open_button.grid(row=1, column=3, padx=(5, 0), pady=(8, 0))
        self.add_tooltip(output_open_button, "open_output_tooltip")

        tools_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        tools_frame.grid(row=2, column=0, sticky="ew")
        tools_frame.columnconfigure(0, weight=1)
        tools_frame.columnconfigure(1, weight=1)

        audit_frame = ttk.LabelFrame(tools_frame, padding=10)
        audit_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        audit_frame.columnconfigure(0, weight=1)
        self.translatable_widgets["audit_group"] = audit_frame

        radio_frame = ttk.Frame(audit_frame)
        radio_frame.grid(row=0, column=0, sticky="w")

        audit_full_radio = ttk.Radiobutton(radio_frame, variable=self.audit_output_mode_var, value="full")
        audit_full_radio.grid(row=0, column=0, sticky="w")
        self.translatable_widgets["audit_full_output"] = audit_full_radio
        self.add_tooltip(audit_full_radio, "audit_output_mode_description")
        audit_findings_radio = ttk.Radiobutton(radio_frame, variable=self.audit_output_mode_var, value="findings")
        audit_findings_radio.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.translatable_widgets["audit_only_findings"] = audit_findings_radio
        self.add_tooltip(audit_findings_radio, "audit_output_mode_description")

        audit_button = ttk.Button(audit_frame, command=self.run_audit_operation)
        audit_button.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.translatable_widgets["run_audit"] = audit_button
        self.add_tooltip(audit_button, "run_audit_tooltip")

        dry_run_button = ttk.Button(audit_frame, command=lambda: self.run_dedupe_operation(False))
        dry_run_button.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.translatable_widgets["dedupe_dry_run"] = dry_run_button
        self.add_tooltip(dry_run_button, "dedupe_dry_run_tooltip")

        edit_frame = ttk.LabelFrame(tools_frame, padding=10)
        edit_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        edit_frame.columnconfigure(0, weight=1)
        self.translatable_widgets["edit_group"] = edit_frame

        apply_button = ttk.Button(edit_frame, command=lambda: self.run_dedupe_operation(True))
        apply_button.grid(row=0, column=0, sticky="ew")
        self.translatable_widgets["dedupe_apply"] = apply_button
        self.add_tooltip(apply_button, "dedupe_apply_tooltip")

        self.root.minsize(700, 420)

        logs_frame = ttk.LabelFrame(self.root, padding=10)
        logs_frame.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 10))
        logs_frame.columnconfigure(0, weight=1)
        logs_frame.rowconfigure(0, weight=1)
        self.translatable_widgets["logs"] = logs_frame

        self.logs_text = scrolledtext.ScrolledText(logs_frame, height=14, state="disabled")
        self.logs_text.grid(row=0, column=0, sticky="nsew")
        clear_button = ttk.Button(logs_frame, command=self.clear_logs)
        clear_button.grid(row=1, column=0, sticky="e", pady=(8, 0))
        self.translatable_widgets["clear_logs"] = clear_button

    def add_tooltip(self, widget: tk.Widget, translation_key: str) -> None:
        self.tooltips.append(Tooltip(widget, lambda key=translation_key: self.t(key)))

    def apply_language(self) -> None:
        self.root.title(self.t("app_title"))
        text_map = {
            "paths": "paths",
            "launchbox_folder": "launchbox_folder",
            "output_folder": "output_folder",
            "audit_group": "audit_group",
            "audit_full_output": "full_output",
            "audit_only_findings": "only_findings",
            "run_audit": "run_audit",
            "dedupe_dry_run": "dedupe_dry_run",
            "edit_group": "edit_group",
            "dedupe_apply": "dedupe_apply",
            "logs": "logs",
            "clear_logs": "clear_logs",
        }
        for widget_key, translation_key in text_map.items():
            self.translatable_widgets[widget_key].configure(text=self.t(translation_key))

    def set_language(self, language: str) -> None:
        self.language = language
        self.apply_language()
        save_interface_language(self.config_path, language)

    def setup_config_autosave(self) -> None:
        self.launchbox_root_var.trace_add("write", self.schedule_config_save)
        self.output_dir_var.trace_add("write", self.schedule_config_save)
        self.audit_output_mode_var.trace_add("write", self.schedule_config_save)

    def schedule_config_save(self, *_args) -> None:
        if self.config_save_after_id is not None:
            self.root.after_cancel(self.config_save_after_id)
        self.config_save_after_id = self.root.after(600, self.autosave_config)

    def autosave_config(self) -> None:
        self.config_save_after_id = None
        self.save_config(log=False)

    def browse_launchbox_folder(self) -> None:
        selected = filedialog.askdirectory(title=self.t("select_launchbox_folder"), initialdir=self.launchbox_root_var.get() or None)
        if selected:
            self.launchbox_root_var.set(selected)
            self.save_config()

    def browse_output_folder(self) -> None:
        selected = filedialog.askdirectory(title=self.t("select_output_folder"), initialdir=self.output_dir_var.get() or None)
        if selected:
            self.output_dir_var.set(selected)
            self.save_config()

    def save_config_from_event(self, _event: tk.Event) -> None:
        self.save_config(log=True)

    def save_config(self, log: bool = True) -> None:
        save_raw_path_config(
            self.config_path,
            self.launchbox_root_var.get(),
            self.output_dir_var.get(),
            only_with_findings=self.audit_output_mode_var.get() == "findings",
        )
        if log:
            self.append_log(self.t("saved_config"))

    def resolve_output_folder_for_gui(self) -> Path | None:
        output = self.output_dir_var.get().strip()
        if not output:
            return None
        output_dir = Path(output).expanduser()
        if output_dir.is_absolute():
            return output_dir.resolve(strict=False)

        root = self.launchbox_root_var.get().strip()
        if not root:
            return output_dir.resolve(strict=False)
        return (Path(root).expanduser() / output_dir).resolve(strict=False)

    def open_output_folder(self) -> None:
        output_dir = self.resolve_output_folder_for_gui()
        if output_dir is None:
            messagebox.showerror(self.t("failed"), self.t("missing_paths"))
            return
        self.open_folder(str(output_dir))

    def open_folder(self, raw_path: str) -> None:
        if not raw_path.strip():
            messagebox.showerror(self.t("failed"), self.t("missing_paths"))
            return
        path = Path(raw_path.strip()).expanduser().resolve(strict=False)
        if not path.is_dir():
            messagebox.showerror(self.t("failed"), f"{self.t('folder_not_found')}\n{path}")
            return
        os.startfile(path)

    def validate_paths(self) -> tuple[Path, Path] | None:
        root = self.launchbox_root_var.get().strip()
        output = self.output_dir_var.get().strip()
        if not root or not output:
            messagebox.showerror(self.t("failed"), self.t("missing_paths"))
            return None

        launchbox_root = Path(root).expanduser().resolve(strict=False)
        output_dir = Path(output).expanduser()
        if not output_dir.is_absolute():
            output_dir = launchbox_root / output_dir
        return launchbox_root, output_dir.resolve(strict=False)

    def is_busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def start_worker(self, start_message: str, target) -> None:
        if self.is_busy():
            messagebox.showwarning(self.t("failed"), self.t("busy"))
            return
        self.save_config(log=False)
        self.append_log(start_message)
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def run_audit_operation(self) -> None:
        paths = self.validate_paths()
        if paths is None:
            return
        launchbox_root, output_dir = paths
        only_with_findings = self.audit_output_mode_var.get() == "findings"

        def worker() -> None:
            try:
                results = run_audit(launchbox_root)
                write_reports(results, output_dir, only_with_findings)
                self.enqueue_log(f"{self.t('audited_platforms')}: {len(results)}")
                self.enqueue_log(f"{self.t('missing_on_disk')}: {sum(len(result.missing_on_disk) for result in results)}")
                self.enqueue_log(f"{self.t('not_in_database')}: {sum(len(result.not_in_database) for result in results)}")
                self.enqueue_log(f"{self.t('warnings')}: {sum(len(result.warnings) for result in results)}")
                self.enqueue_log(f"{self.t('reports_written')}: {output_dir}")
                self.enqueue_log(self.t("finished"))
            except Exception:
                self.enqueue_log(f"{self.t('failed')}:\n{traceback.format_exc()}")

        self.start_worker(self.t("starting_audit"), worker)

    def show_mutation_blocked_error(self, exc: MutationBlockedError) -> None:
        if exc.reason == "launchbox_running":
            message = self.t("mutation_blocked_launchbox")
        elif exc.reason == "files_locked":
            if len(exc.locked_files) == 1:
                message = self.t("mutation_blocked_files").format(path=exc.locked_files[0])
            else:
                paths = "\n".join(str(path) for path in exc.locked_files)
                message = self.t("mutation_blocked_files_many").format(paths=paths)
        else:
            message = str(exc)
        messagebox.showerror(self.t("mutation_blocked_title"), message)

    def run_dedupe_operation(self, apply_changes: bool) -> None:
        paths = self.validate_paths()
        if paths is None:
            return

        launchbox_root, output_dir = paths
        if apply_changes:
            try:
                platforms = load_platforms(launchbox_root)
                xml_paths = [platform.database_xml for platform in platforms if platform.database_xml.exists()]
                ensure_safe_to_mutate(xml_paths)
            except MutationBlockedError as exc:
                self.show_mutation_blocked_error(exc)
                return

        if apply_changes and not messagebox.askyesno(self.t("confirm_apply_title"), self.t("confirm_apply_message")):
            return
        only_with_findings = self.audit_output_mode_var.get() == "findings"

        def worker() -> None:
            try:
                results = run_additional_apps_dedupe(launchbox_root, apply_changes=apply_changes)
                write_dedupe_reports(results, output_dir, apply_changes, only_with_findings)
                self.enqueue_log(f"{self.t('dedupe_mode')}: {'apply' if apply_changes else 'dry-run'}")
                self.enqueue_log(f"{self.t('processed_platforms')}: {len(results)}")
                self.enqueue_log(f"{self.t('duplicates')}: {sum(len(result.duplicates) for result in results)}")
                self.enqueue_log(f"{self.t('changed_files')}: {sum(1 for result in results if result.applied)}")
                self.enqueue_log(f"{self.t('warnings')}: {sum(len(result.warnings) for result in results)}")
                failed_results = [result for result in results if result.error]
                if failed_results:
                    self.enqueue_log(f"{self.t('failed_platforms')}: {len(failed_results)}")
                    for result in failed_results:
                        self.enqueue_log(f"  {result.platform.name}: {result.error}")
                self.enqueue_log(f"{self.t('reports_written')}: {output_dir}")
                self.enqueue_log(self.t("finished"))
            except Exception:
                self.enqueue_log(f"{self.t('failed')}:\n{traceback.format_exc()}")

        message = self.t("starting_dedupe_apply") if apply_changes else self.t("starting_dedupe_dry_run")
        self.start_worker(message, worker)

    def enqueue_log(self, message: str) -> None:
        self.log_queue.put(message)

    def process_log_queue(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(message)
        self.root.after(100, self.process_log_queue)

    def append_log(self, message: str) -> None:
        self.logs_text.configure(state="normal")
        self.logs_text.insert("end", f"{message}\n")
        self.logs_text.see("end")
        self.logs_text.configure(state="disabled")

    def clear_logs(self) -> None:
        self.logs_text.configure(state="normal")
        self.logs_text.delete("1.0", "end")
        self.logs_text.configure(state="disabled")


def run_gui(config_path: Path) -> int:
    try:
        root = tk.Tk()
        LaunchBoxUtilsApp(root, config_path)
        root.mainloop()
    except Exception:
        if getattr(sys, "frozen", False):
            messagebox.showerror("LaunchBox Utils", traceback.format_exc())
            return 1
        raise
    return 0

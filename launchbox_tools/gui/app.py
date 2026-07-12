from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable

from ..config import (
    load_configured_only_with_findings,
    load_raw_path_config,
    resolve_initial_language,
    save_interface_language,
    save_raw_path_config,
)
from ..operations.audit import run_audit
from ..operations.dedupe_additional_apps import run_additional_apps_dedupe
from ..operations.path_replacement import run_path_replacement
from ..runtime_checks import MutationBlockedError, ensure_safe_to_mutate
from ..xml_repository import load_platforms
from ..reports.audit_reports import write_reports
from ..reports.dedupe_reports import write_dedupe_reports
from ..reports.path_replacement_reports import write_path_replacement_reports
from .translations import translate


@dataclass(frozen=True)
class GuiOperation:
    key: str
    title_key: str
    category_key: str
    description_key: str
    build_panel: Callable[[], None]
    planned: bool = False


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
        self.current_operation_key = tk.StringVar(value="audit")
        self.operation_buttons: dict[str, ttk.Button] = {}
        self.nav_frame: ttk.LabelFrame | None = None
        self.operation_title: ttk.Label | None = None
        self.operation_description: ttk.Label | None = None
        self.operation_content: ttk.Frame | None = None
        self.main_pane: ttk.PanedWindow | None = None
        self.logs_frame: ttk.Frame | None = None
        self.logs_body: ttk.Frame | None = None
        self.logs_text: scrolledtext.ScrolledText | None = None
        self.logs_toggle_button: ttk.Button | None = None
        self.clear_logs_button: ttk.Button | None = None
        self.language_button: ttk.Button | None = None
        self.logs_collapsed = False

        raw_config = load_raw_path_config(config_path)
        self.launchbox_root_var = tk.StringVar(value=raw_config.launchbox_root)
        self.output_dir_var = tk.StringVar(value=raw_config.output_dir)
        self.logs_status_var = tk.StringVar()
        self.replace_old_path_var = tk.StringVar()
        self.replace_new_path_var = tk.StringVar()
        self.export_destination_var = tk.StringVar()
        self.export_mode_var = tk.StringVar(value="main")
        output_mode = "findings" if load_configured_only_with_findings(config_path) else "full"
        self.audit_output_mode_var = tk.StringVar(value=output_mode)
        self.operations = self.build_operations()

        self.translatable_widgets: dict[str, ttk.Widget | tk.Widget] = {}
        self.build_form()
        self.setup_config_autosave()
        self.apply_language()
        self.root.after(100, self.process_log_queue)

    def t(self, key: str) -> str:
        return translate(self.language, key)

    def build_operations(self) -> list[GuiOperation]:
        return [
            GuiOperation("audit", "operation_audit", "category_analyze", "audit_description", self.build_audit_panel),
            GuiOperation("dedupe", "operation_dedupe", "category_repair", "dedupe_description", self.build_dedupe_panel),
            GuiOperation("replace_paths", "operation_replace_paths", "category_repair", "replace_paths_description", self.build_replace_paths_panel),
            GuiOperation("restore_main_files", "operation_restore_main_files", "category_repair", "restore_main_files_description", self.build_restore_main_files_panel, planned=True),
            GuiOperation("export_favorites", "operation_export_favorites", "category_export", "export_favorites_description", self.build_export_favorites_panel, planned=True),
        ]

    def visible_operations(self) -> list[GuiOperation]:
        return [operation for operation in self.operations if not operation.planned]

    def build_form(self) -> None:
        self.root.title(self.t("app_title"))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self.build_global_header()
        self.main_pane = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        self.main_pane.grid(row=1, column=0, sticky="nsew")

        task_area = self.build_task_area(self.main_pane)
        logs_area = self.build_logs_area(self.main_pane)
        self.main_pane.add(task_area, weight=1)
        self.main_pane.add(logs_area, weight=0)
        self.root.after_idle(self.set_initial_pane_sizes)
        self.root.minsize(900, 620)
        self.show_operation(self.current_operation_key.get())

    def build_global_header(self) -> None:
        header = ttk.LabelFrame(self.root, padding=10)
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        header.columnconfigure(1, weight=1)
        header.columnconfigure(5, weight=1)
        self.translatable_widgets["global_settings"] = header

        launchbox_label = ttk.Label(header)
        launchbox_label.grid(row=0, column=0, sticky="w")
        self.translatable_widgets["launchbox_folder"] = launchbox_label
        launchbox_entry = ttk.Entry(header, textvariable=self.launchbox_root_var)
        launchbox_entry.grid(row=0, column=1, sticky="ew", padx=5)
        launchbox_entry.bind("<FocusOut>", self.save_config_from_event)
        launchbox_entry.bind("<Return>", self.save_config_from_event)
        launchbox_button = ttk.Button(header, text="...", width=3, command=self.browse_launchbox_folder)
        launchbox_button.grid(row=0, column=2)
        self.add_tooltip(launchbox_button, "browse_launchbox_tooltip")
        launchbox_open_button = ttk.Button(header, text=">", width=3, command=lambda: self.open_folder(self.launchbox_root_var.get()))
        launchbox_open_button.grid(row=0, column=3, padx=(5, 16))
        self.add_tooltip(launchbox_open_button, "open_launchbox_tooltip")

        output_label = ttk.Label(header)
        output_label.grid(row=0, column=4, sticky="w")
        self.translatable_widgets["output_folder"] = output_label
        output_entry = ttk.Entry(header, textvariable=self.output_dir_var)
        output_entry.grid(row=0, column=5, sticky="ew", padx=5)
        output_entry.bind("<FocusOut>", self.save_config_from_event)
        output_entry.bind("<Return>", self.save_config_from_event)
        output_button = ttk.Button(header, text="...", width=3, command=self.browse_output_folder)
        output_button.grid(row=0, column=6)
        self.add_tooltip(output_button, "browse_output_tooltip")
        output_open_button = ttk.Button(header, text=">", width=3, command=self.open_output_folder)
        output_open_button.grid(row=0, column=7, padx=(5, 16))
        self.add_tooltip(output_open_button, "open_output_tooltip")

        language_button = ttk.Button(header, width=5, command=self.toggle_language)
        language_button.grid(row=0, column=8, padx=(5, 0))
        self.language_button = language_button
        self.add_tooltip(language_button, "interface_language_tooltip")

    def build_task_area(self, parent: tk.Widget) -> ttk.Frame:
        task_area = ttk.Frame(parent, padding=(10, 0, 10, 10))
        task_area.columnconfigure(1, weight=1)
        task_area.rowconfigure(0, weight=1)

        nav_frame = ttk.LabelFrame(task_area, padding=8, width=220)
        nav_frame.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        nav_frame.grid_propagate(False)
        nav_frame.columnconfigure(0, weight=1)
        self.nav_frame = nav_frame
        self.translatable_widgets["tasks"] = nav_frame

        row = 0
        previous_category = ""
        for operation in self.visible_operations():
            if operation.category_key != previous_category:
                category_label = ttk.Label(nav_frame)
                category_label.grid(row=row, column=0, sticky="w", pady=(8 if row else 0, 4))
                self.translatable_widgets[f"category_{operation.key}"] = category_label
                previous_category = operation.category_key
                row += 1
            button = ttk.Button(nav_frame, command=lambda key=operation.key: self.show_operation(key))
            button.grid(row=row, column=0, sticky="ew", pady=2)
            self.operation_buttons[operation.key] = button
            self.translatable_widgets[f"nav_{operation.key}"] = button
            row += 1

        operation_frame = ttk.LabelFrame(task_area, padding=12)
        operation_frame.grid(row=0, column=1, sticky="nsew")
        operation_frame.columnconfigure(0, weight=1)
        operation_frame.rowconfigure(2, weight=1)
        self.translatable_widgets["operation"] = operation_frame

        self.operation_title = ttk.Label(operation_frame, font=("TkDefaultFont", 12, "bold"))
        self.operation_title.grid(row=0, column=0, sticky="w")
        self.operation_description = ttk.Label(operation_frame, wraplength=620)
        self.operation_description.grid(row=1, column=0, sticky="ew", pady=(4, 12))
        self.operation_content = ttk.Frame(operation_frame)
        self.operation_content.grid(row=2, column=0, sticky="nsew")
        self.operation_content.columnconfigure(0, weight=1)
        return task_area

    def build_logs_area(self, parent: tk.Widget) -> ttk.Frame:
        logs_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        logs_frame.columnconfigure(0, weight=1)
        logs_frame.rowconfigure(1, weight=1)
        self.logs_frame = logs_frame

        header = ttk.Frame(logs_frame)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        logs_label = ttk.Label(header, font=("TkDefaultFont", 9, "bold"))
        logs_label.grid(row=0, column=0, sticky="w")
        self.translatable_widgets["logs"] = logs_label

        status_label = ttk.Label(header, textvariable=self.logs_status_var)
        status_label.grid(row=0, column=1, sticky="ew", padx=(12, 8))

        open_reports_button = ttk.Button(header, command=self.open_output_folder)
        open_reports_button.grid(row=0, column=2, sticky="e", padx=(0, 8))
        self.translatable_widgets["open_reports"] = open_reports_button
        clear_button = ttk.Button(header, command=self.clear_logs)
        clear_button.grid(row=0, column=3, sticky="e", padx=(0, 8))
        self.clear_logs_button = clear_button
        self.translatable_widgets["clear_logs"] = clear_button

        toggle_button = ttk.Button(header, width=12, command=self.toggle_logs)
        toggle_button.grid(row=0, column=4, sticky="e")
        self.logs_toggle_button = toggle_button
        self.translatable_widgets["toggle_logs"] = toggle_button

        self.logs_body = ttk.Frame(logs_frame)
        self.logs_body.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.logs_body.columnconfigure(0, weight=1)
        self.logs_body.rowconfigure(0, weight=1)
        self.logs_text = scrolledtext.ScrolledText(self.logs_body, height=7, state="disabled")
        self.logs_text.grid(row=0, column=0, sticky="nsew")
        return logs_frame

    def set_initial_pane_sizes(self) -> None:
        if self.main_pane is None or self.logs_collapsed:
            return
        pane_height = self.main_pane.winfo_height()
        if pane_height > 0:
            try:
                self.main_pane.sashpos(0, max(320, pane_height - 190))
            except tk.TclError:
                pass

    def toggle_logs(self) -> None:
        if self.logs_collapsed:
            self.expand_logs()
        else:
            self.collapse_logs()

    def collapse_logs(self) -> None:
        if self.logs_body is not None and self.logs_body.winfo_ismapped():
            self.logs_body.grid_remove()
        if self.clear_logs_button is not None:
            self.clear_logs_button.grid_remove()
        self.logs_collapsed = True
        self.update_logs_toggle_text()
        self.root.after_idle(self.resize_collapsed_logs)

    def expand_logs(self) -> None:
        if self.logs_body is not None:
            self.logs_body.grid()
        if self.clear_logs_button is not None:
            self.clear_logs_button.grid()
        self.logs_collapsed = False
        self.update_logs_toggle_text()
        self.root.after_idle(self.resize_expanded_logs)

    def resize_collapsed_logs(self) -> None:
        if self.main_pane is None:
            return
        pane_height = self.main_pane.winfo_height()
        if pane_height > 0:
            try:
                self.main_pane.sashpos(0, max(320, pane_height - 42))
            except tk.TclError:
                pass

    def resize_expanded_logs(self) -> None:
        if self.main_pane is None:
            return
        pane_height = self.main_pane.winfo_height()
        if pane_height > 0:
            try:
                self.main_pane.sashpos(0, max(320, pane_height - 190))
            except tk.TclError:
                pass

    def update_logs_toggle_text(self) -> None:
        if self.logs_toggle_button is not None and self.logs_toggle_button.winfo_exists():
            key = "expand_logs" if self.logs_collapsed else "collapse_logs"
            self.logs_toggle_button.configure(text=self.t(key))

    def update_navigation_width(self) -> None:
        if self.nav_frame is None or not self.nav_frame.winfo_exists():
            return
        visible_operations = self.visible_operations()
        if not visible_operations:
            return

        text_font = tkfont.nametofont("TkDefaultFont")
        widest_label = max(text_font.measure(self.t(operation.title_key)) for operation in visible_operations)
        self.nav_frame.configure(width=max(220, widest_label + 64))

    def show_operation(self, operation_key: str) -> None:
        operation = self.get_operation(operation_key)
        self.current_operation_key.set(operation.key)
        if self.operation_title is not None:
            self.operation_title.configure(text=self.t(operation.title_key))
        if self.operation_description is not None:
            self.operation_description.configure(text=self.t(operation.description_key))
        for key, button in self.operation_buttons.items():
            button.state(["pressed"] if key == operation.key else ["!pressed"])

        if self.operation_content is None:
            return
        for child in self.operation_content.winfo_children():
            child.destroy()
        operation.build_panel()
        self.apply_language()

    def get_operation(self, operation_key: str) -> GuiOperation:
        visible_operations = self.visible_operations()
        for operation in visible_operations:
            if operation.key == operation_key:
                return operation
        return visible_operations[0]

    def build_report_mode_controls(self, parent: ttk.Frame, row: int = 0) -> int:
        frame = ttk.LabelFrame(parent, padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        self.translatable_widgets[f"output_mode_{self.current_operation_key.get()}"] = frame

        audit_full_radio = ttk.Radiobutton(frame, variable=self.audit_output_mode_var, value="full")
        audit_full_radio.grid(row=0, column=0, sticky="w")
        self.translatable_widgets[f"full_output_{self.current_operation_key.get()}"] = audit_full_radio
        self.add_tooltip(audit_full_radio, "audit_output_mode_description")
        audit_findings_radio = ttk.Radiobutton(frame, variable=self.audit_output_mode_var, value="findings")
        audit_findings_radio.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.translatable_widgets[f"only_findings_{self.current_operation_key.get()}"] = audit_findings_radio
        self.add_tooltip(audit_findings_radio, "audit_output_mode_description")
        return row + 1

    def build_action_buttons(
        self,
        parent: ttk.Frame,
        row: int,
        dry_run_key: str,
        dry_run_tooltip_key: str,
        dry_run_command,
        apply_key: str | None = None,
        apply_tooltip_key: str | None = None,
        apply_command=None,
        disabled: bool = False,
    ) -> None:
        dry_run_button = ttk.Button(parent, command=dry_run_command)
        dry_run_button.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        if disabled:
            dry_run_button.state(["disabled"])
        self.translatable_widgets[f"{dry_run_key}_{self.current_operation_key.get()}"] = dry_run_button
        self.add_tooltip(dry_run_button, dry_run_tooltip_key)

        if apply_key is None or apply_tooltip_key is None or apply_command is None:
            return
        apply_button = ttk.Button(parent, command=apply_command)
        apply_button.grid(row=row + 1, column=0, sticky="ew", pady=(8, 0))
        if disabled:
            apply_button.state(["disabled"])
        self.translatable_widgets[f"{apply_key}_{self.current_operation_key.get()}"] = apply_button
        self.add_tooltip(apply_button, apply_tooltip_key)

    def build_audit_panel(self) -> None:
        if self.operation_content is None:
            return
        self.operation_content.columnconfigure(0, weight=1)
        row = self.build_report_mode_controls(self.operation_content)
        self.build_action_buttons(
            self.operation_content,
            row,
            "run_audit",
            "run_audit_tooltip",
            self.run_audit_operation,
        )

    def build_dedupe_panel(self) -> None:
        if self.operation_content is None:
            return
        self.operation_content.columnconfigure(0, weight=1)
        row = self.build_report_mode_controls(self.operation_content)
        self.build_action_buttons(
            self.operation_content,
            row,
            "dedupe_dry_run",
            "dedupe_dry_run_tooltip",
            lambda: self.run_dedupe_operation(False),
            "dedupe_apply",
            "dedupe_apply_tooltip",
            lambda: self.run_dedupe_operation(True),
        )

    def build_replace_paths_panel(self) -> None:
        if self.operation_content is None:
            return
        self.operation_content.columnconfigure(0, weight=1)
        row = self.build_report_mode_controls(self.operation_content)

        paths_frame = ttk.Frame(self.operation_content)
        paths_frame.grid(row=row, column=0, sticky="ew")
        paths_frame.columnconfigure(1, weight=1)

        replace_old_label = ttk.Label(paths_frame)
        replace_old_label.grid(row=0, column=0, sticky="w", pady=(2, 0))
        self.translatable_widgets["replace_old_path_active"] = replace_old_label
        replace_old_entry = ttk.Entry(paths_frame, textvariable=self.replace_old_path_var)
        replace_old_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=(2, 0))
        replace_old_button = ttk.Button(paths_frame, text="...", width=3, command=lambda: self.browse_replacement_path(self.replace_old_path_var))
        replace_old_button.grid(row=0, column=2, pady=(2, 0))
        self.add_tooltip(replace_old_button, "browse_replace_old_tooltip")

        replace_new_label = ttk.Label(paths_frame)
        replace_new_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.translatable_widgets["replace_new_path_active"] = replace_new_label
        replace_new_entry = ttk.Entry(paths_frame, textvariable=self.replace_new_path_var)
        replace_new_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=(8, 0))
        replace_new_button = ttk.Button(paths_frame, text="...", width=3, command=lambda: self.browse_replacement_path(self.replace_new_path_var))
        replace_new_button.grid(row=1, column=2, pady=(8, 0))
        self.add_tooltip(replace_new_button, "browse_replace_new_tooltip")
        row += 1

        button_frame = ttk.Frame(self.operation_content)
        button_frame.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        button_frame.columnconfigure(0, weight=1)
        self.build_action_buttons(
            button_frame,
            0,
            "replace_paths_dry_run",
            "replace_paths_dry_run_tooltip",
            lambda: self.run_path_replacement_operation(False),
            "replace_paths_apply",
            "replace_paths_apply_tooltip",
            lambda: self.run_path_replacement_operation(True),
        )

    def build_restore_main_files_panel(self) -> None:
        self.build_planned_operation_panel("restore_main_files_planned")

    def build_export_favorites_panel(self) -> None:
        if self.operation_content is None:
            return
        self.operation_content.columnconfigure(1, weight=1)
        destination_label = ttk.Label(self.operation_content)
        destination_label.grid(row=0, column=0, sticky="w")
        self.translatable_widgets["export_destination"] = destination_label
        destination_entry = ttk.Entry(self.operation_content, textvariable=self.export_destination_var)
        destination_entry.grid(row=0, column=1, sticky="ew", padx=5)
        destination_button = ttk.Button(self.operation_content, text="...", width=3, command=lambda: self.browse_replacement_path(self.export_destination_var))
        destination_button.grid(row=0, column=2)
        self.add_tooltip(destination_button, "browse_export_destination_tooltip")

        mode_frame = ttk.LabelFrame(self.operation_content, padding=8)
        mode_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.translatable_widgets["export_mode"] = mode_frame
        main_radio = ttk.Radiobutton(mode_frame, variable=self.export_mode_var, value="main")
        main_radio.grid(row=0, column=0, sticky="w")
        self.translatable_widgets["export_main_file_only"] = main_radio
        all_radio = ttk.Radiobutton(mode_frame, variable=self.export_mode_var, value="all")
        all_radio.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.translatable_widgets["export_all_files"] = all_radio

        self.build_planned_operation_panel("export_favorites_planned", start_row=2, columnspan=3)

    def build_planned_operation_panel(self, message_key: str, start_row: int = 0, columnspan: int = 1) -> None:
        if self.operation_content is None:
            return
        planned_label = ttk.Label(self.operation_content, wraplength=620)
        planned_label.grid(row=start_row, column=0, columnspan=columnspan, sticky="ew", pady=(10 if start_row else 0, 10))
        self.translatable_widgets[f"{message_key}_{self.current_operation_key.get()}"] = planned_label

        button_frame = ttk.Frame(self.operation_content)
        button_frame.grid(row=start_row + 1, column=0, columnspan=columnspan, sticky="ew")
        button_frame.columnconfigure(0, weight=1)
        self.build_action_buttons(
            button_frame,
            0,
            "planned_dry_run",
            "planned_operation_tooltip",
            lambda: None,
            "planned_apply",
            "planned_operation_tooltip",
            lambda: None,
            disabled=True,
        )

    def add_tooltip(self, widget: tk.Widget, translation_key: str) -> None:
        self.tooltips.append(Tooltip(widget, lambda key=translation_key: self.t(key)))

    def apply_language(self) -> None:
        self.root.title(self.t("app_title"))
        text_map = {
            "paths": "paths",
            "global_settings": "global_settings",
            "tasks": "tasks",
            "operation": "operation",
            "launchbox_folder": "launchbox_folder",
            "output_folder": "output_folder",
            "audit_group": "audit_group",
            "audit_full_output": "full_output",
            "audit_only_findings": "only_findings",
            "run_audit": "run_audit",
            "dedupe_dry_run": "dedupe_dry_run",
            "edit_group": "edit_group",
            "dedupe_apply": "dedupe_apply",
            "replace_old_path": "replace_old_path",
            "replace_new_path": "replace_new_path",
            "replace_old_path_active": "replace_old_path",
            "replace_new_path_active": "replace_new_path",
            "replace_paths_dry_run": "replace_paths_dry_run",
            "replace_paths_apply": "replace_paths_apply",
            "open_reports": "open_reports",
            "logs": "logs",
            "clear_logs": "clear_logs",
            "toggle_logs": "collapse_logs",
            "export_destination": "export_destination",
            "export_mode": "export_mode",
            "export_main_file_only": "export_main_file_only",
            "export_all_files": "export_all_files",
        }
        for operation in self.visible_operations():
            text_map[f"category_{operation.key}"] = operation.category_key
            text_map[f"nav_{operation.key}"] = operation.title_key
            text_map[f"output_mode_{operation.key}"] = "output_mode"
            text_map[f"full_output_{operation.key}"] = "full_output"
            text_map[f"only_findings_{operation.key}"] = "only_findings"
            text_map[f"run_audit_{operation.key}"] = "run_audit"
            text_map[f"dedupe_dry_run_{operation.key}"] = "dedupe_dry_run"
            text_map[f"dedupe_apply_{operation.key}"] = "dedupe_apply"
            text_map[f"replace_paths_dry_run_{operation.key}"] = "replace_paths_dry_run"
            text_map[f"replace_paths_apply_{operation.key}"] = "replace_paths_apply"
            text_map[f"planned_dry_run_{operation.key}"] = "planned_dry_run"
            text_map[f"planned_apply_{operation.key}"] = "planned_apply"
            text_map[f"restore_main_files_planned_{operation.key}"] = "restore_main_files_planned"
            text_map[f"export_favorites_planned_{operation.key}"] = "export_favorites_planned"

        for widget_key, translation_key in text_map.items():
            widget = self.translatable_widgets.get(widget_key)
            if widget is not None and widget.winfo_exists():
                widget.configure(text=self.t(translation_key))
        current_operation = self.get_operation(self.current_operation_key.get())
        if self.operation_title is not None and self.operation_title.winfo_exists():
            self.operation_title.configure(text=self.t(current_operation.title_key))
        if self.operation_description is not None and self.operation_description.winfo_exists():
            self.operation_description.configure(text=self.t(current_operation.description_key))
        if self.language_button is not None and self.language_button.winfo_exists():
            self.language_button.configure(text=self.language.upper())
        self.update_logs_toggle_text()
        self.update_navigation_width()

    def toggle_language(self) -> None:
        self.set_language("en" if self.language == "ru" else "ru")

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

    def browse_replacement_path(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(title=self.t("select_replacement_folder"), initialdir=variable.get() or None)
        if selected:
            variable.set(selected)

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
                self.enqueue_log(f"{self.t('ambiguities')}: {sum(len(result.ambiguities) for result in results)}")
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

    def validate_replacement_paths(self) -> tuple[Path, Path] | None:
        old_value = self.replace_old_path_var.get().strip()
        new_value = self.replace_new_path_var.get().strip()
        if not old_value or not new_value:
            messagebox.showerror(self.t("failed"), self.t("missing_replacement_paths"))
            return None

        old_path = Path(old_value).expanduser()
        new_path = Path(new_value).expanduser()
        if not old_path.is_absolute() or not new_path.is_absolute():
            messagebox.showerror(self.t("failed"), self.t("replacement_paths_must_be_absolute"))
            return None
        return old_path, new_path

    def run_path_replacement_operation(self, apply_changes: bool) -> None:
        paths = self.validate_paths()
        replacement_paths = self.validate_replacement_paths()
        if paths is None or replacement_paths is None:
            return

        launchbox_root, output_dir = paths
        old_path, new_path = replacement_paths
        if apply_changes:
            try:
                platforms = load_platforms(launchbox_root)
                xml_paths = [launchbox_root / "Data" / "Platforms.xml"]
                xml_paths.extend(platform.database_xml for platform in platforms if platform.database_xml.exists())
                ensure_safe_to_mutate(xml_paths)
            except MutationBlockedError as exc:
                self.show_mutation_blocked_error(exc)
                return

        if apply_changes and not messagebox.askyesno(self.t("confirm_apply_title"), self.t("confirm_apply_message")):
            return
        only_with_findings = self.audit_output_mode_var.get() == "findings"

        def worker() -> None:
            try:
                results = run_path_replacement(launchbox_root, old_path, new_path, apply_changes=apply_changes)
                write_path_replacement_reports(results, output_dir, apply_changes, only_with_findings)
                self.enqueue_log(f"{self.t('path_replacement_mode')}: {'apply' if apply_changes else 'dry-run'}")
                self.enqueue_log(f"{self.t('processed_platforms')}: {len(results)}")
                self.enqueue_log(f"{self.t('path_replacements')}: {sum(len(result.replacements) for result in results)}")
                self.enqueue_log(f"{self.t('changed_files')}: {len({path for result in results for path in result.backup_paths})}")
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

        message = self.t("starting_replace_paths_apply") if apply_changes else self.t("starting_replace_paths_dry_run")
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
        self.logs_status_var.set(message.splitlines()[0] if message else "")
        if "Traceback" in message:
            self.expand_logs()
        if self.logs_text is None:
            return
        self.logs_text.configure(state="normal")
        self.logs_text.insert("end", f"{message}\n")
        self.logs_text.see("end")
        self.logs_text.configure(state="disabled")

    def clear_logs(self) -> None:
        self.logs_status_var.set("")
        if self.logs_text is None:
            return
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

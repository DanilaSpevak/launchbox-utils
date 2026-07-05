from __future__ import annotations

import csv
from pathlib import Path

from ..models import AdditionalAppsDedupeResult
from ..paths import safe_report_dir_name


DEDUPE_DETAIL_FILES = ("duplicate_additional_apps.txt",)


def dedupe_result_has_findings(result: AdditionalAppsDedupeResult) -> bool:
    return bool(result.duplicates or result.warnings)


def cleanup_dedupe_detail_files(platform_dir: Path) -> None:
    for file_name in DEDUPE_DETAIL_FILES:
        (platform_dir / file_name).unlink(missing_ok=True)
    try:
        platform_dir.rmdir()
    except OSError:
        pass


def write_dedupe_reports(
    results: list[AdditionalAppsDedupeResult],
    output_dir: Path,
    apply_changes: bool,
    only_with_findings: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "duplicate_additional_apps.csv"
    mode = "apply" if apply_changes else "dry-run"
    summary_results = [result for result in results if dedupe_result_has_findings(result)] if only_with_findings else results

    if only_with_findings and not summary_results:
        summary_csv.unlink(missing_ok=True)
    else:
        # Excel on Russian Windows often opens .csv files with the system ANSI codepage,
        # even when Notepad++ correctly detects Unicode. cp1251 is the most reliable
        # double-click format for Cyrillic CSV in that environment.
        with summary_csv.open("w", encoding="cp1251", errors="replace", newline="") as file:
            file.write("sep=;\n")
            writer = csv.writer(file, delimiter=";")
            writer.writerow(
                [
                    "mode",
                    "platform",
                    "game_id",
                    "duplicate_title",
                    "duplicate_application_path",
                    "kept_title",
                    "kept_application_path",
                    "applied",
                    "backup_path",
                    "warnings",
                ]
            )
            for result in summary_results:
                if result.duplicates:
                    for duplicate in result.duplicates:
                        writer.writerow(
                            [
                                mode,
                                result.platform.name,
                                duplicate.duplicate.game_id,
                                duplicate.duplicate.title,
                                duplicate.duplicate.application_path,
                                duplicate.kept.title,
                                duplicate.kept.application_path,
                                result.applied,
                                result.backup_path or "",
                                " | ".join(result.warnings),
                            ]
                        )
                else:
                    writer.writerow(
                        [
                            mode,
                            result.platform.name,
                            "",
                            "",
                            "",
                            "",
                            "",
                            result.applied,
                            result.backup_path or "",
                            " | ".join(result.warnings),
                        ]
                    )

    used_dir_names: set[str] = set()
    for result in results:
        base_dir_name = safe_report_dir_name(result.platform.name)
        platform_dir_name = base_dir_name
        suffix = 2
        while platform_dir_name.casefold() in used_dir_names:
            platform_dir_name = f"{base_dir_name} ({suffix})"
            suffix += 1
        used_dir_names.add(platform_dir_name.casefold())

        platform_dir = output_dir / platform_dir_name
        if only_with_findings:
            cleanup_dedupe_detail_files(platform_dir)
            if not dedupe_result_has_findings(result):
                continue

        platform_dir.mkdir(parents=True, exist_ok=True)
        with (platform_dir / "duplicate_additional_apps.txt").open("w", encoding="utf-8", newline="\n") as file:
            file.write(f"=== {result.platform.name} ===\n")
            file.write(f"Mode: {mode}\n")
            file.write(f"Applied: {result.applied}\n")
            if result.backup_path:
                file.write(f"Backup: {result.backup_path}\n")
            if result.warnings:
                file.write("Warnings:\n")
                for warning in result.warnings:
                    file.write(f"  {warning}\n")
            file.write("\nDuplicate AdditionalApplication entries:\n")
            if not result.duplicates:
                file.write("  <none>\n")
            for duplicate in result.duplicates:
                file.write(f"  Remove: {duplicate.duplicate.application_path} | {duplicate.duplicate.title} | GameID: {duplicate.duplicate.game_id}\n")
                file.write(f"  Keep:   {duplicate.kept.application_path} | {duplicate.kept.title}\n\n")

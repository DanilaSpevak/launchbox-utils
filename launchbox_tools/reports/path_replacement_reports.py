from __future__ import annotations

import csv
from pathlib import Path

from ..models import PathReplacementResult
from ..paths import safe_report_dir_name


PATH_REPLACEMENT_DETAIL_FILES = ("path_replacements.txt",)


def path_replacement_result_has_findings(result: PathReplacementResult) -> bool:
    return bool(result.replacements or result.warnings or result.error)


def cleanup_path_replacement_detail_files(platform_dir: Path) -> None:
    for file_name in PATH_REPLACEMENT_DETAIL_FILES:
        (platform_dir / file_name).unlink(missing_ok=True)
    try:
        platform_dir.rmdir()
    except OSError:
        pass


def write_path_replacement_reports(
    results: list[PathReplacementResult],
    output_dir: Path,
    apply_changes: bool,
    only_with_findings: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "path_replacements.csv"
    mode = "apply" if apply_changes else "dry-run"
    summary_results = [result for result in results if path_replacement_result_has_findings(result)] if only_with_findings else results

    if only_with_findings and not summary_results:
        summary_csv.unlink(missing_ok=True)
    else:
        with summary_csv.open("w", encoding="utf-8-sig", newline="") as file:
            file.write("sep=;\n")
            writer = csv.writer(file, delimiter=";")
            writer.writerow(
                [
                    "mode",
                    "platform",
                    "xml_path",
                    "entry_type",
                    "title",
                    "old_value",
                    "new_value",
                    "applied",
                    "backup_paths",
                    "error",
                    "warnings",
                ]
            )
            for result in summary_results:
                if result.replacements:
                    for replacement in result.replacements:
                        writer.writerow(
                            [
                                mode,
                                result.platform.name,
                                replacement.xml_path,
                                replacement.entry_type,
                                replacement.title,
                                replacement.old_value,
                                replacement.new_value,
                                replacement.applied,
                                " | ".join(str(path) for path in result.backup_paths),
                                replacement.error or result.error or "",
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
                            " | ".join(str(path) for path in result.backup_paths),
                            result.error or "",
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
            cleanup_path_replacement_detail_files(platform_dir)
            if not path_replacement_result_has_findings(result):
                continue

        platform_dir.mkdir(parents=True, exist_ok=True)
        with (platform_dir / "path_replacements.txt").open("w", encoding="utf-8", newline="\n") as file:
            file.write(f"=== {result.platform.name} ===\n")
            file.write(f"Mode: {mode}\n")
            file.write(f"Applied: {result.applied}\n")
            if result.backup_paths:
                file.write("Backups:\n")
                for backup_path in result.backup_paths:
                    file.write(f"  {backup_path}\n")
            if result.error:
                file.write(f"Error: {result.error}\n")
            if result.warnings:
                file.write("Warnings:\n")
                for warning in result.warnings:
                    file.write(f"  {warning}\n")

            file.write("\nPath replacements:\n")
            if not result.replacements:
                file.write("  <none>\n")
            for replacement in result.replacements:
                file.write(f"  {replacement.entry_type}: {replacement.title}\n")
                file.write(f"    XML: {replacement.xml_path}\n")
                file.write(f"    Old: {replacement.old_value}\n")
                file.write(f"    New: {replacement.new_value}\n")
                file.write(f"    Applied: {replacement.applied}\n")
                if replacement.error:
                    file.write(f"    Error: {replacement.error}\n")
                file.write("\n")

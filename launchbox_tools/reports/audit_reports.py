from __future__ import annotations

import csv
from pathlib import Path

from ..models import PlatformAuditResult
from ..paths import safe_report_dir_name


AUDIT_DETAIL_FILES = ("missing_on_disk.txt", "not_in_database.txt", "warnings.txt")


def audit_result_has_findings(result: PlatformAuditResult) -> bool:
    return bool(result.missing_on_disk or result.not_in_database or result.warnings)


def cleanup_audit_detail_files(platform_dir: Path) -> None:
    for file_name in AUDIT_DETAIL_FILES:
        (platform_dir / file_name).unlink(missing_ok=True)
    try:
        platform_dir.rmdir()
    except OSError:
        pass


def write_report_section(file, result: PlatformAuditResult, section_title: str, rows: list[str]) -> None:
    file.write(f"=== {result.platform.name} ===\n")
    if result.warnings:
        file.write("Warnings:\n")
        for warning in result.warnings:
            file.write(f"  {warning}\n")
        file.write("\n")

    file.write(f"{section_title}:\n")
    if rows:
        for row in rows:
            file.write(f"  {row}\n")
    else:
        file.write("  <none>\n")
    file.write("\n")


def write_warnings_file(path: Path, result: PlatformAuditResult) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(f"=== {result.platform.name} ===\n")
        file.write("Warnings:\n")
        for warning in result.warnings:
            file.write(f"  {warning}\n")
        file.write("\n")


def write_reports(results: list[PlatformAuditResult], output_dir: Path, only_with_findings: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_csv = output_dir / "summary.csv"
    summary_results = [result for result in results if audit_result_has_findings(result)] if only_with_findings else results

    # Remove aggregate reports created by older versions; reports are now per-platform.
    for old_report in (output_dir / "missing_on_disk.txt", output_dir / "not_in_database.txt"):
        old_report.unlink(missing_ok=True)

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
            cleanup_audit_detail_files(platform_dir)
            if not audit_result_has_findings(result):
                continue

        missing_rows = [
            f"{game.resolved_path} | {game.title} | database path: {game.application_path}"
            for game in result.missing_on_disk
        ]
        extra_rows = [str(path) for path in result.not_in_database]

        platform_dir.mkdir(parents=True, exist_ok=True)

        if missing_rows or not only_with_findings:
            with (platform_dir / "missing_on_disk.txt").open("w", encoding="utf-8", newline="\n") as file:
                write_report_section(file, result, "Missing on disk", missing_rows)

        if extra_rows or not only_with_findings:
            with (platform_dir / "not_in_database.txt").open("w", encoding="utf-8", newline="\n") as file:
                write_report_section(file, result, "Files not in database", extra_rows)

        if only_with_findings and result.warnings and not missing_rows and not extra_rows:
            write_warnings_file(platform_dir / "warnings.txt", result)

    if only_with_findings and not summary_results:
        summary_csv.unlink(missing_ok=True)
        return

    with summary_csv.open("w", encoding="utf-8-sig", newline="") as file:
        file.write("sep=;\n")
        writer = csv.writer(file, delimiter=";")
        writer.writerow(
            [
                "platform",
                "database_entries",
                "folder_files",
                "missing_on_disk",
                "not_in_database",
                "warnings",
            ]
        )
        for result in summary_results:
            writer.writerow(
                [
                    result.platform.name,
                    result.database_count,
                    result.folder_count,
                    len(result.missing_on_disk),
                    len(result.not_in_database),
                    " | ".join(result.warnings),
                ]
            )

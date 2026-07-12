from __future__ import annotations

import csv
from pathlib import Path

from ..models import AdditionalAppsDedupeResult, MutationRunResult
from ..paths import safe_report_dir_name


DEDUPE_DETAIL_FILES = ("duplicate_additional_apps.txt",)


def dedupe_result_has_findings(result: AdditionalAppsDedupeResult) -> bool:
    return bool(result.duplicates or result.ambiguities or result.warnings or result.error)


def cleanup_dedupe_detail_files(platform_dir: Path) -> None:
    for file_name in DEDUPE_DETAIL_FILES:
        (platform_dir / file_name).unlink(missing_ok=True)
    try:
        platform_dir.rmdir()
    except OSError:
        pass


def write_dedupe_reports(
    run_result: MutationRunResult[AdditionalAppsDedupeResult],
    output_dir: Path,
    apply_changes: bool,
    only_with_findings: bool = False,
) -> None:
    results = run_result.results
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "duplicate_additional_apps.csv"
    mode = "apply" if apply_changes else "dry-run"
    summary_results = [result for result in results if dedupe_result_has_findings(result)] if only_with_findings else results

    if only_with_findings and not summary_results:
        summary_csv.unlink(missing_ok=True)
    else:
        with summary_csv.open("w", encoding="utf-8-sig", newline="") as file:
            file.write("sep=;\n")
            writer = csv.writer(file, delimiter=";")
            writer.writerow(
                [
                    "mode",
                    "outcome",
                    "finding_type",
                    "platform",
                    "game_id",
                    "duplicate_title",
                    "duplicate_application_path",
                    "kept_title",
                    "kept_application_path",
                    "state",
                    "backup_path",
                    "manifest_path",
                    "manifest_error",
                    "error",
                    "rollback_errors",
                    "warnings",
                    "differing_fields",
                    "variant_count",
                ]
            )
            for result in summary_results:
                if result.duplicates:
                    for duplicate in result.duplicates:
                        writer.writerow(
                            [
                                mode,
                                run_result.outcome.value,
                                "duplicate",
                                result.platform.name,
                                duplicate.duplicate.game_id,
                                duplicate.duplicate.title,
                                duplicate.duplicate.application_path,
                                duplicate.kept.title,
                                duplicate.kept.application_path,
                                duplicate.state.value,
                                result.backup_path or "",
                                run_result.manifest_path or "",
                                run_result.manifest_error or "",
                                duplicate.error or result.error or "",
                                " | ".join(run_result.rollback_errors),
                                " | ".join(result.warnings),
                                "",
                                "",
                            ]
                        )
                for ambiguity in result.ambiguities:
                    for variant in ambiguity.variants:
                        writer.writerow(
                            [
                                mode,
                                run_result.outcome.value,
                                "ambiguous",
                                result.platform.name,
                                variant.game_id,
                                variant.title,
                                variant.application_path,
                                "",
                                "",
                                "",
                                result.backup_path or "",
                                run_result.manifest_path or "",
                                run_result.manifest_error or "",
                                result.error or "",
                                " | ".join(run_result.rollback_errors),
                                " | ".join(result.warnings),
                                " | ".join(ambiguity.differing_fields),
                                len(ambiguity.variants),
                            ]
                        )
                if not result.duplicates and not result.ambiguities:
                    writer.writerow(
                        [
                            mode,
                            run_result.outcome.value,
                            "",
                            result.platform.name,
                            "",
                            "",
                            "",
                            "",
                            "",
                            result.state.value if result.state else "",
                            result.backup_path or "",
                            run_result.manifest_path or "",
                            run_result.manifest_error or "",
                            result.error or "",
                            " | ".join(run_result.rollback_errors),
                            " | ".join(result.warnings),
                            "",
                            "",
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
            file.write(f"Outcome: {run_result.outcome.value}\n")
            file.write(f"State: {result.state.value if result.state else ''}\n")
            if run_result.manifest_path:
                file.write(f"Manifest: {run_result.manifest_path}\n")
            if run_result.manifest_error:
                file.write(f"Manifest error: {run_result.manifest_error}\n")
            if result.backup_path:
                file.write(f"Backup: {result.backup_path}\n")
            if result.error:
                file.write(f"Error: {result.error}\n")
            for rollback_error in run_result.rollback_errors:
                file.write(f"Rollback error: {rollback_error}\n")
            if result.warnings:
                file.write("Warnings:\n")
                for warning in result.warnings:
                    file.write(f"  {warning}\n")
            file.write("\nDuplicate AdditionalApplication entries:\n")
            if not result.duplicates:
                file.write("  <none>\n")
            for duplicate in result.duplicates:
                file.write(f"  Remove: {duplicate.duplicate.application_path} | {duplicate.duplicate.title} | GameID: {duplicate.duplicate.game_id}\n")
                file.write(f"  Keep:   {duplicate.kept.application_path} | {duplicate.kept.title}\n")
                file.write(f"  State:  {duplicate.state.value}\n")
                if duplicate.error:
                    file.write(f"  Error:  {duplicate.error}\n")
                file.write("\n")
            file.write("\nAmbiguous AdditionalApplication groups:\n")
            if not result.ambiguities:
                file.write("  <none>\n")
            for ambiguity in result.ambiguities:
                file.write(f"  GameID: {ambiguity.variants[0].game_id} | Path: {ambiguity.variants[0].application_path}\n")
                file.write(f"  Differing fields: {', '.join(ambiguity.differing_fields)}\n")
                for variant in ambiguity.variants:
                    file.write(f"  Keep variant: {variant.title} | {variant.application_path}\n")
                file.write("\n")

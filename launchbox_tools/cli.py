from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import LAUNCHBOX_ROOT, REPORT_DIR_NAME
from .operations.audit import run_audit
from .operations.dedupe_additional_apps import run_additional_apps_dedupe
from .paths import resolve_output_dir
from .reports.audit_reports import write_reports
from .reports.dedupe_reports import write_dedupe_reports


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit and maintain a LaunchBox ROM database.")
    parser.add_argument("--root", default=str(LAUNCHBOX_ROOT), help="LaunchBox root directory.")
    parser.add_argument("--output", default=REPORT_DIR_NAME, help="Report directory. Relative paths are resolved from LaunchBox root.")

    subparsers = parser.add_subparsers(dest="command")
    audit_parser = subparsers.add_parser("audit", help="Run the read-only ROM audit.")
    audit_parser.add_argument(
        "--only-with-findings",
        action="store_true",
        help="Write per-platform report folders/files only for platforms with findings.",
    )

    dedupe_parser = subparsers.add_parser("dedupe-additional-apps", help="Find or remove duplicate AdditionalApplication entries.")
    dedupe_parser.add_argument("--apply", action="store_true", help="Remove duplicates from XML files. Without this flag, only reports are written.")
    dedupe_parser.add_argument("--platform", help="Limit dedupe to one platform name.")
    dedupe_parser.add_argument(
        "--only-with-findings",
        action="store_true",
        help="Write duplicate reports only for platforms with duplicates or warnings.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve(strict=False)
    output_dir = resolve_output_dir(root, args.output)
    command = args.command or "audit"
    only_with_findings = getattr(args, "only_with_findings", False)

    try:
        if command == "audit":
            results = run_audit(root)
            write_reports(results, output_dir, only_with_findings)
        elif command == "dedupe-additional-apps":
            results = run_additional_apps_dedupe(root, args.platform, args.apply)
            write_dedupe_reports(results, output_dir, args.apply, only_with_findings)
        else:
            parser.error(f"Unknown command: {command}")
    except FileNotFoundError as exc:
        print(f"LaunchBox operation failed: required file not found: {exc}", file=sys.stderr)
        return 1
    except ET.ParseError as exc:
        print(f"LaunchBox operation failed: XML parse error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"LaunchBox operation failed: {exc}", file=sys.stderr)
        return 1

    if command == "audit":
        missing_count = sum(len(result.missing_on_disk) for result in results)
        extra_count = sum(len(result.not_in_database) for result in results)
        warning_count = sum(len(result.warnings) for result in results)

        print(f"Audited platforms: {len(results)}")
        print(f"Missing on disk: {missing_count}")
        print(f"Files not in database: {extra_count}")
        print(f"Warnings: {warning_count}")
    else:
        duplicate_count = sum(len(result.duplicates) for result in results)
        changed_count = sum(1 for result in results if result.applied)
        warning_count = sum(len(result.warnings) for result in results)
        mode = "apply" if args.apply else "dry-run"

        print(f"Dedupe mode: {mode}")
        print(f"Processed platforms: {len(results)}")
        print(f"Duplicate AdditionalApplication entries: {duplicate_count}")
        print(f"Changed platform XML files: {changed_count}")
        print(f"Warnings: {warning_count}")
    print(f"Reports written to: {output_dir}")
    return 0

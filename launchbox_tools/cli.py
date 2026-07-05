from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET

from .config import ConfigError, DEFAULT_CONFIG_PATH, load_app_config, resolve_config_path
from .operations.audit import run_audit
from .operations.dedupe_additional_apps import run_additional_apps_dedupe
from .reports.audit_reports import write_reports
from .reports.dedupe_reports import write_dedupe_reports


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit and maintain a LaunchBox ROM database.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to launchbox_utils.ini.")
    parser.add_argument("--root", help="LaunchBox root directory. Overrides the config file.")
    parser.add_argument("--output", help="Report directory. Overrides the config file. Relative paths are resolved from LaunchBox root.")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("gui", help="Open the graphical interface.")

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
    command = args.command or "audit"
    only_with_findings = getattr(args, "only_with_findings", False)
    config_path = resolve_config_path(args.config)

    if command == "gui":
        from .gui.app import run_gui

        return run_gui(config_path)

    try:
        app_config = load_app_config(
            config_path,
            root_override=args.root,
            output_override=args.output,
        )
        root = app_config.launchbox_root
        output_dir = app_config.output_dir

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
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
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

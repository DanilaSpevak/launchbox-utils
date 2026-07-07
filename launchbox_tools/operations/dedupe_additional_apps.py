from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from ..models import AdditionalApplicationDuplicate, AdditionalAppsDedupeResult, GameEntry, PlatformInfo
from ..paths import path_key
from ..runtime_checks import ensure_safe_to_mutate
from ..safe_write import backup_platform_xml, write_xml_tree_safely
from ..xml_repository import load_application_entries, load_platforms, parse_xml_tree


def additional_app_dedupe_key(entry: GameEntry) -> tuple[str, str] | None:
    if entry.entry_type != "AdditionalApplication":
        return None

    game_id = entry.game_id.strip()
    application_path = entry.application_path.strip()
    if not game_id or not application_path:
        return None

    return game_id.casefold(), path_key(entry.resolved_path)


def find_additional_app_duplicates(
    platform: PlatformInfo,
    root: Path,
    entries: list[GameEntry],
) -> tuple[list[AdditionalApplicationDuplicate], list[str]]:
    seen: dict[tuple[str, str], GameEntry] = {}
    duplicates: list[AdditionalApplicationDuplicate] = []
    warnings: list[str] = []

    for entry in entries:
        if entry.entry_type != "AdditionalApplication":
            continue

        key = additional_app_dedupe_key(entry)
        if key is None:
            warnings.append(f"AdditionalApplication skipped for dedupe because GameID or ApplicationPath is empty: {entry.title}")
            continue

        kept = seen.get(key)
        if kept is None:
            seen[key] = entry
            continue

        duplicates.append(
            AdditionalApplicationDuplicate(
                platform=platform,
                kept=kept,
                duplicate=entry,
                key=key,
            )
        )

    return duplicates, warnings


def dedupe_additional_apps_for_platform(
    platform: PlatformInfo,
    root: Path,
    apply_changes: bool,
    backup_root: Path,
) -> AdditionalAppsDedupeResult:
    result = AdditionalAppsDedupeResult(platform=platform)
    if not platform.database_xml.exists():
        result.warnings.append(f"Platform XML not found: {platform.database_xml}")
        return result

    tree = parse_xml_tree(platform.database_xml)
    entries, warnings = load_application_entries(platform, root, tree.getroot(), include_xml_links=True)
    result.warnings.extend(warnings)
    duplicates, dedupe_warnings = find_additional_app_duplicates(platform, root, entries)
    result.duplicates = duplicates
    result.warnings.extend(dedupe_warnings)

    if not apply_changes or not duplicates:
        result.warnings.sort()
        return result

    removable_duplicates = [
        duplicate
        for duplicate in duplicates
        if duplicate.duplicate.element is not None and duplicate.duplicate.parent is not None
    ]
    skipped_count = len(duplicates) - len(removable_duplicates)
    if skipped_count:
        result.warnings.append(f"Skipped {skipped_count} duplicate(s) because XML parent could not be determined")

    if not removable_duplicates:
        result.warnings.sort()
        return result

    result.backup_path = backup_platform_xml(platform, backup_root)
    for duplicate in removable_duplicates:
        duplicate.duplicate.parent.remove(duplicate.duplicate.element)

    write_xml_tree_safely(tree, platform.database_xml)
    result.applied = True
    result.warnings.sort()
    return result


def run_additional_apps_dedupe(
    root: Path,
    platform_filter: str | None = None,
    apply_changes: bool = False,
) -> list[AdditionalAppsDedupeResult]:
    root = root.resolve(strict=False)
    platforms = load_platforms(root)
    if platform_filter:
        platforms = [platform for platform in platforms if platform.name.casefold() == platform_filter.casefold()]

    if apply_changes:
        xml_paths = [platform.database_xml for platform in platforms if platform.database_xml.exists()]
        ensure_safe_to_mutate(xml_paths)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = root / "Data" / "Backups" / f"AdditionalAppsDedupe-{timestamp}"
    results: list[AdditionalAppsDedupeResult] = []
    for platform in platforms:
        try:
            results.append(dedupe_additional_apps_for_platform(platform, root, apply_changes, backup_root))
        except (ET.ParseError, OSError) as exc:
            results.append(AdditionalAppsDedupeResult(platform=platform, error=str(exc)))
    return results

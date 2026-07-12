from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from ..models import (
    AdditionalApplicationAmbiguity,
    AdditionalApplicationDuplicate,
    AdditionalAppsDedupeResult,
    GameEntry,
    PlatformInfo,
)
from ..paths import path_key
from ..runtime_checks import ensure_safe_to_mutate
from ..safe_write import backup_platform_xml, write_xml_tree_safely
from ..xml_repository import load_application_entries, load_platforms, local_name, parse_xml_tree


CanonicalElement = tuple[str, tuple[tuple[str, str], ...], str, tuple["CanonicalElement", ...]]


def _normalize_xml_text(tag: str, text: str, entry: GameEntry, is_entry_field: bool = False) -> str:
    value = " ".join(text.split())
    if is_entry_field and tag == "GameID":
        return value.casefold()
    if is_entry_field and tag == "ApplicationPath":
        return path_key(entry.resolved_path)
    if value.casefold() in {"true", "false"}:
        return value.casefold()
    return value


def _canonical_element(element: ET.Element, entry: GameEntry, is_entry_field: bool = False) -> CanonicalElement:
    tag = local_name(element.tag)
    attributes = tuple(sorted((name, " ".join(value.split())) for name, value in element.attrib.items()))
    children = tuple(
        sorted(_canonical_element(child, entry, is_entry_field=tag == "AdditionalApplication") for child in element)
    )
    return element.tag, attributes, _normalize_xml_text(tag, element.text or "", entry, is_entry_field), children


def additional_app_canonical_signature(entry: GameEntry) -> CanonicalElement | None:
    if entry.entry_type != "AdditionalApplication" or entry.element is None:
        return None
    return _canonical_element(entry.element, entry)


def _field_signatures(entry: GameEntry) -> dict[str, tuple[CanonicalElement, ...]]:
    if entry.element is None:
        return {}
    fields: dict[str, list[CanonicalElement]] = {}
    for child in entry.element:
        fields.setdefault(local_name(child.tag), []).append(_canonical_element(child, entry, is_entry_field=True))
    return {name: tuple(sorted(values)) for name, values in fields.items()}


def _differing_fields(variants: list[GameEntry]) -> tuple[str, ...]:
    field_maps = [_field_signatures(entry) for entry in variants]
    names = {name for fields in field_maps for name in fields}
    differing = [name for name in names if len({fields.get(name) for fields in field_maps}) > 1]
    root_attributes = [
        tuple(sorted((name, " ".join(value.split())) for name, value in entry.element.attrib.items()))
        for entry in variants
        if entry.element is not None
    ]
    if len(set(root_attributes)) > 1:
        differing.append("@attributes")
    return tuple(sorted(differing, key=str.casefold))


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
) -> tuple[list[AdditionalApplicationDuplicate], list[AdditionalApplicationAmbiguity], list[str]]:
    groups: dict[tuple[str, str], dict[CanonicalElement, GameEntry]] = {}
    duplicates: list[AdditionalApplicationDuplicate] = []
    ambiguities: list[AdditionalApplicationAmbiguity] = []
    warnings: list[str] = []

    for entry in entries:
        if entry.entry_type != "AdditionalApplication":
            continue

        key = additional_app_dedupe_key(entry)
        if key is None:
            warnings.append(f"AdditionalApplication skipped for dedupe because GameID or ApplicationPath is empty: {entry.title}")
            continue

        signature = additional_app_canonical_signature(entry)
        if signature is None:
            warnings.append(f"AdditionalApplication skipped for dedupe because XML content is unavailable: {entry.title}")
            continue

        variants = groups.setdefault(key, {})
        kept = variants.get(signature)
        if kept is None:
            variants[signature] = entry
            continue

        duplicates.append(
            AdditionalApplicationDuplicate(
                platform=platform,
                kept=kept,
                duplicate=entry,
                key=key,
            )
        )

    for key, signatures in groups.items():
        if len(signatures) < 2:
            continue
        variants = list(signatures.values())
        ambiguities.append(
            AdditionalApplicationAmbiguity(
                platform=platform,
                key=key,
                variants=tuple(variants),
                differing_fields=_differing_fields(variants),
            )
        )

    return duplicates, ambiguities, warnings


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
    duplicates, ambiguities, dedupe_warnings = find_additional_app_duplicates(platform, root, entries)
    result.duplicates = duplicates
    result.ambiguities = ambiguities
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

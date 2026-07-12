from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from ..models import (
    AdditionalApplicationAmbiguity,
    AdditionalApplicationDuplicate,
    AdditionalAppsDedupeResult,
    GameEntry,
    MutationOutcome,
    MutationRunResult,
    PlatformInfo,
)
from ..paths import path_key, resolve_launchbox_path
from ..runtime_checks import ensure_safe_to_mutate
from ..safe_write import XmlMutation, execute_xml_transaction
from ..xml_repository import load_application_entries, load_platforms, local_name, parse_xml_tree


CanonicalElement = tuple[str, tuple[tuple[str, str], ...], str, str, tuple["CanonicalElement", ...]]
_CANONICAL_BOOLEAN_FIELDS = frozenset({"AutoRunBefore", "AutoRunAfter", "UseEmulator"})


def _normalize_xml_text(
    tag: str,
    text: str,
    root: Path,
    is_entry_field: bool = False,
    has_children: bool = False,
) -> str:
    if has_children and not text.strip():
        return ""
    if is_entry_field and tag == "GameID":
        return text.strip().casefold()
    if is_entry_field and tag == "ApplicationPath":
        if not text.strip():
            return text
        return path_key(resolve_launchbox_path(root, text))
    if is_entry_field and tag in _CANONICAL_BOOLEAN_FIELDS:
        value = text.strip()
        if value.casefold() in {"true", "false"}:
            return value.casefold()
    return text


def _canonical_element(
    element: ET.Element,
    root: Path,
    is_entry_field: bool = False,
    include_tail: bool = False,
) -> CanonicalElement:
    tag = local_name(element.tag)
    attributes = tuple(sorted(element.attrib.items()))
    children = tuple(
        sorted(
            _canonical_element(
                child,
                root,
                is_entry_field=tag == "AdditionalApplication",
                include_tail=True,
            )
            for child in element
        )
    )
    text = _normalize_xml_text(tag, element.text or "", root, is_entry_field, has_children=bool(children))
    tail = element.tail or ""
    if not include_tail or not tail.strip():
        tail = ""
    return element.tag, attributes, text, tail, children


def additional_app_canonical_signature(entry: GameEntry, root: Path) -> CanonicalElement | None:
    if entry.entry_type != "AdditionalApplication" or entry.element is None:
        return None
    return _canonical_element(entry.element, root)


def _field_signatures(entry: GameEntry, root: Path) -> dict[str, tuple[CanonicalElement, ...]]:
    if entry.element is None:
        return {}
    fields: dict[str, list[CanonicalElement]] = {}
    for child in entry.element:
        fields.setdefault(local_name(child.tag), []).append(_canonical_element(child, root, is_entry_field=True))
    return {name: tuple(sorted(values)) for name, values in fields.items()}


def _differing_fields(variants: list[GameEntry], root: Path) -> tuple[str, ...]:
    field_maps = [_field_signatures(entry, root) for entry in variants]
    names = {name for fields in field_maps for name in fields}
    differing = [name for name in names if len({fields.get(name) for fields in field_maps}) > 1]
    root_attributes = [
        tuple(sorted(entry.element.attrib.items()))
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

        signature = additional_app_canonical_signature(entry, root)
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
                differing_fields=_differing_fields(variants, root),
            )
        )

    return duplicates, ambiguities, warnings


def dedupe_additional_apps_for_platform(
    platform: PlatformInfo,
    root: Path,
    apply_changes: bool,
    backup_root: Path,
) -> tuple[AdditionalAppsDedupeResult, MutationOutcome, list[str]]:
    result = AdditionalAppsDedupeResult(platform=platform)
    if not platform.database_xml.exists():
        result.warnings.append(f"Platform XML not found: {platform.database_xml}")
        return result, MutationOutcome.DRY_RUN if not apply_changes else MutationOutcome.SUCCESS, []

    tree = parse_xml_tree(platform.database_xml)
    entries, warnings = load_application_entries(platform, root, tree.getroot(), include_xml_links=True)
    result.warnings.extend(warnings)
    duplicates, ambiguities, dedupe_warnings = find_additional_app_duplicates(platform, root, entries)
    result.duplicates = duplicates
    result.ambiguities = ambiguities
    result.warnings.extend(dedupe_warnings)

    if not apply_changes or not duplicates:
        result.warnings.sort()
        return result, MutationOutcome.DRY_RUN if not apply_changes else MutationOutcome.SUCCESS, []

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
        return result, MutationOutcome.SUCCESS, []

    for duplicate in removable_duplicates:
        duplicate.duplicate.parent.remove(duplicate.duplicate.element)

    transaction = execute_xml_transaction([XmlMutation(platform.database_xml, tree)], backup_root)
    result.backup_path = transaction.backup_paths.get(platform.database_xml.resolve(strict=False))
    result.error = transaction.error
    result.applied = transaction.outcome == MutationOutcome.SUCCESS
    result.warnings.sort()
    return result, transaction.outcome, transaction.rollback_errors


def run_additional_apps_dedupe(
    root: Path,
    platform_filter: str | None = None,
    apply_changes: bool = False,
) -> MutationRunResult[AdditionalAppsDedupeResult]:
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
    outcomes: list[MutationOutcome] = []
    rollback_errors: list[str] = []
    for platform in platforms:
        try:
            result, outcome, platform_rollback_errors = dedupe_additional_apps_for_platform(
                platform, root, apply_changes, backup_root
            )
            results.append(result)
            outcomes.append(outcome)
            rollback_errors.extend(platform_rollback_errors)
        except (ET.ParseError, OSError) as exc:
            results.append(AdditionalAppsDedupeResult(platform=platform, error=str(exc)))
            outcomes.append(MutationOutcome.FAILED)

    if not apply_changes:
        outcome = MutationOutcome.FAILED if MutationOutcome.FAILED in outcomes else MutationOutcome.DRY_RUN
    else:
        committed_count = sum(1 for result in results if result.applied)
        unsuccessful = any(item in {MutationOutcome.FAILED, MutationOutcome.ROLLED_BACK} for item in outcomes)
        if committed_count and unsuccessful:
            outcome = MutationOutcome.PARTIAL
        elif MutationOutcome.ROLLED_BACK in outcomes and MutationOutcome.FAILED not in outcomes:
            outcome = MutationOutcome.ROLLED_BACK
        elif unsuccessful:
            outcome = MutationOutcome.FAILED
        else:
            outcome = MutationOutcome.SUCCESS

    errors = [result.error for result in results if result.error]
    return MutationRunResult(results, outcome, " | ".join(errors) or None, rollback_errors)

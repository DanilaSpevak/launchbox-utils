from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ..models import (
    AdditionalApplicationAmbiguity,
    AdditionalApplicationDuplicate,
    AdditionalAppsDedupeResult,
    GameEntry,
    MutationFileResult,
    MutationOutcome,
    MutationRunResult,
    MutationState,
    PlatformInfo,
)
from ..mutation_lock import mutation_run_lock
from ..mutation_manifest import write_mutation_manifest
from ..operation_lifecycle import OperationCancelled, OperationControl, OperationPhase
from ..paths import UnsafeDatabasePathError, path_key, resolve_launchbox_path
from ..runtime_checks import ensure_safe_to_mutate
from ..safe_write import XmlMutation, execute_xml_transaction, reserve_unique_backup_root
from ..xml_repository import (
    existing_platform_database_paths,
    load_application_entries,
    load_platform_database_tree,
    load_platforms,
    local_name,
)


CanonicalElement = tuple[str, tuple[tuple[str, str], ...], str, str, tuple["CanonicalElement", ...]]
_CANONICAL_BOOLEAN_FIELDS = frozenset({"AutoRunBefore", "AutoRunAfter", "UseEmulator"})
_SCAN_CHECKPOINT_INTERVAL = 256


class _CheckpointCounter:
    def __init__(self, control: OperationControl | None) -> None:
        self.control = control
        self.count = 0

    def tick(self) -> None:
        if self.control is None:
            return
        self.count += 1
        if self.count % _SCAN_CHECKPOINT_INTERVAL == 0:
            self.control.checkpoint()


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
    checkpoint_counter: _CheckpointCounter | None = None,
) -> CanonicalElement:
    if checkpoint_counter is not None:
        checkpoint_counter.tick()
    tag = local_name(element.tag)
    attributes = tuple(sorted(element.attrib.items()))
    children = tuple(
        sorted(
            _canonical_element(
                child,
                root,
                is_entry_field=tag == "AdditionalApplication",
                include_tail=True,
                checkpoint_counter=checkpoint_counter,
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


def _field_signatures(
    entry: GameEntry,
    root: Path,
    checkpoint_counter: _CheckpointCounter | None = None,
) -> dict[str, tuple[CanonicalElement, ...]]:
    if entry.element is None:
        return {}
    fields: dict[str, list[CanonicalElement]] = {}
    for child in entry.element:
        fields.setdefault(local_name(child.tag), []).append(
            _canonical_element(
                child,
                root,
                is_entry_field=True,
                checkpoint_counter=checkpoint_counter,
            )
        )
    return {name: tuple(sorted(values)) for name, values in fields.items()}


def _differing_fields(
    variants: list[GameEntry],
    root: Path,
    checkpoint_counter: _CheckpointCounter | None = None,
) -> tuple[str, ...]:
    field_maps: list[dict[str, tuple[CanonicalElement, ...]]] = []
    root_attributes: list[tuple[tuple[str, str], ...]] = []
    for entry in variants:
        if checkpoint_counter is not None:
            checkpoint_counter.tick()
        field_maps.append(_field_signatures(entry, root, checkpoint_counter))
        if entry.element is not None:
            root_attributes.append(tuple(sorted(entry.element.attrib.items())))

    names: set[str] = set()
    for fields in field_maps:
        if checkpoint_counter is not None:
            checkpoint_counter.tick()
        names.update(fields)

    differing: list[str] = []
    for name in names:
        signatures: set[tuple[CanonicalElement, ...] | None] = set()
        for fields in field_maps:
            if checkpoint_counter is not None:
                checkpoint_counter.tick()
            signatures.add(fields.get(name))
        if len(signatures) > 1:
            differing.append(name)
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
    *,
    control: OperationControl | None = None,
) -> tuple[list[AdditionalApplicationDuplicate], list[AdditionalApplicationAmbiguity], list[str]]:
    groups: dict[tuple[str, str], dict[CanonicalElement, GameEntry]] = {}
    duplicates: list[AdditionalApplicationDuplicate] = []
    ambiguities: list[AdditionalApplicationAmbiguity] = []
    warnings: list[str] = []
    analysis_checkpoints = _CheckpointCounter(control)

    if control is not None:
        control.checkpoint()
    for index, entry in enumerate(entries, start=1):
        if control is not None and index % _SCAN_CHECKPOINT_INTERVAL == 0:
            control.checkpoint()
        if entry.entry_type != "AdditionalApplication":
            continue

        key = additional_app_dedupe_key(entry)
        if key is None:
            warnings.append(f"AdditionalApplication skipped for dedupe because GameID or ApplicationPath is empty: {entry.title}")
            continue

        signature = (
            _canonical_element(
                entry.element,
                root,
                checkpoint_counter=analysis_checkpoints,
            )
            if entry.element is not None
            else None
        )
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

    for index, (key, signatures) in enumerate(groups.items(), start=1):
        if control is not None and index % _SCAN_CHECKPOINT_INTERVAL == 0:
            control.checkpoint()
        if len(signatures) < 2:
            continue
        variants = list(signatures.values())
        ambiguities.append(
            AdditionalApplicationAmbiguity(
                platform=platform,
                key=key,
                variants=tuple(variants),
                differing_fields=_differing_fields(
                    variants,
                    root,
                    analysis_checkpoints,
                ),
            )
        )

    return duplicates, ambiguities, warnings


def _remove_duplicate_elements(
    duplicates: list[AdditionalApplicationDuplicate],
    *,
    control: OperationControl | None = None,
) -> None:
    if control is not None:
        control.checkpoint()
    removals_by_parent: dict[int, tuple[ET.Element, set[int]]] = {}
    removal_checkpoints = _CheckpointCounter(control)
    for duplicate in duplicates:
        parent = duplicate.duplicate.parent
        element = duplicate.duplicate.element
        if parent is None or element is None:
            continue
        removal_checkpoints.tick()
        _parent, element_ids = removals_by_parent.setdefault(
            id(parent),
            (parent, set()),
        )
        element_ids.add(id(element))

    filtered_children: list[tuple[ET.Element, list[ET.Element]]] = []
    for parent, element_ids in removals_by_parent.values():
        kept_children: list[ET.Element] = []
        for child in parent:
            removal_checkpoints.tick()
            if id(child) not in element_ids:
                kept_children.append(child)
        filtered_children.append((parent, kept_children))

    for parent, kept_children in filtered_children:
        if control is not None:
            control.checkpoint()
        parent[:] = kept_children


def dedupe_additional_apps_for_platform(
    platform: PlatformInfo,
    root: Path,
    apply_changes: bool,
    reserve_backup_root: Callable[[], Path],
    run_id: str | None = None,
    *,
    control: OperationControl | None = None,
) -> tuple[
    AdditionalAppsDedupeResult,
    MutationOutcome,
    list[str],
    list[MutationFileResult],
    UnsafeDatabasePathError | None,
]:
    if control is not None:
        control.checkpoint()
    result = AdditionalAppsDedupeResult(platform=platform)
    tree = load_platform_database_tree(platform, root, control=control)
    if tree is None:
        result.warnings.append(f"Platform XML not found: {platform.database_xml}")
        return result, MutationOutcome.DRY_RUN if not apply_changes else MutationOutcome.SUCCESS, [], [], None
    entries, warnings = load_application_entries(
        platform,
        root,
        tree.getroot(),
        include_xml_links=True,
        control=control,
    )
    result.warnings.extend(warnings)
    duplicates, ambiguities, dedupe_warnings = find_additional_app_duplicates(
        platform,
        root,
        entries,
        control=control,
    )
    result.duplicates = duplicates
    result.ambiguities = ambiguities
    result.warnings.extend(dedupe_warnings)

    if duplicates:
        result.state = MutationState.PLANNED

    if not apply_changes or not duplicates:
        result.warnings.sort()
        files = (
            [MutationFileResult(platform.database_xml.absolute())]
            if duplicates
            else []
        )
        return result, MutationOutcome.DRY_RUN if not apply_changes else MutationOutcome.SUCCESS, [], files, None

    removable_duplicates = [
        duplicate
        for duplicate in duplicates
        if duplicate.duplicate.element is not None and duplicate.duplicate.parent is not None
    ]
    skipped_count = len(duplicates) - len(removable_duplicates)
    if skipped_count:
        error = f"Could not prepare {skipped_count} duplicate(s) because XML parent could not be determined"
        result.warnings.append(error)
        result.duplicates = [
            replace(duplicate, state=MutationState.FAILED, error=error)
            for duplicate in duplicates
        ]
        result.error = error
        result.state = MutationState.FAILED
        result.warnings.sort()
        file_result = MutationFileResult(
            platform.database_xml.absolute(),
            state=MutationState.FAILED,
            error=error,
        )
        return result, MutationOutcome.FAILED, [], [file_result], None

    try:
        if control is not None:
            control.checkpoint()
        _remove_duplicate_elements(removable_duplicates, control=control)
    except OperationCancelled as exc:
        error = str(exc)
        result.error = error
        result.state = MutationState.PLANNED
        result.duplicates = [
            replace(duplicate, state=MutationState.PLANNED, error=error)
            for duplicate in result.duplicates
        ]
        file_result = MutationFileResult(platform.database_xml.absolute())
        return result, MutationOutcome.CANCELLED, [], [file_result], None

    transaction = execute_xml_transaction(
        [
            XmlMutation(
                platform.database_xml,
                tree,
                trusted_parent=root / "Data" / "Platforms",
                trust_anchor=root,
            )
        ],
        reserve_backup_root(),
        run_id,
        control=control,
    )
    result.backup_path = transaction.files[0].backup_path if transaction.files else None
    result.error = transaction.error
    result.error_reason = transaction.blocked_reason
    result.error_details = transaction.blocked_details
    if transaction.files:
        file_result = transaction.files[0]
        result.state = file_result.state
        change_error = file_result.error or transaction.error
        result.duplicates = [
            replace(duplicate, state=result.state, error=change_error)
            for duplicate in result.duplicates
        ]
    if transaction.outcome != MutationOutcome.CANCELLED:
        result.warnings.sort()
    return (
        result,
        transaction.outcome,
        transaction.rollback_errors,
        transaction.files,
        transaction.unsafe_path_error,
    )


def run_additional_apps_dedupe(
    root: Path,
    platform_filter: str | None = None,
    apply_changes: bool = False,
    *,
    control: OperationControl | None = None,
) -> MutationRunResult[AdditionalAppsDedupeResult]:
    resolved_root = root.absolute()
    if not apply_changes:
        return _run_additional_apps_dedupe(
            resolved_root,
            platform_filter,
            apply_changes=False,
            control=control,
        )

    run_id = str(uuid4())
    with mutation_run_lock(resolved_root, "dedupe_additional_apps", run_id):
        return _run_additional_apps_dedupe(
            resolved_root,
            platform_filter,
            apply_changes=True,
            run_id=run_id,
            control=control,
        )


def _run_additional_apps_dedupe(
    root: Path,
    platform_filter: str | None = None,
    apply_changes: bool = False,
    run_id: str | None = None,
    control: OperationControl | None = None,
) -> MutationRunResult[AdditionalAppsDedupeResult]:
    root = root.absolute()
    if control is not None:
        control.set_phase(OperationPhase.SCAN)
    platforms: list[PlatformInfo] = []
    cancelled_error: str | None = None
    try:
        platforms = load_platforms(root, control=control)
        if platform_filter:
            platforms = [
                platform
                for platform in platforms
                if platform.name.casefold() == platform_filter.casefold()
            ]
    except OperationCancelled as exc:
        cancelled_error = str(exc)

    if apply_changes and cancelled_error is None:
        xml_paths = existing_platform_database_paths(root, platforms)
        ensure_safe_to_mutate(xml_paths)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_parent = root / "Data" / "Backups"
    backup_name = f"AdditionalAppsDedupe-{timestamp}"
    if run_id is not None:
        backup_name = f"{backup_name}-{run_id}"
    backup_root: Path | None = None

    def ensure_backup_root() -> Path:
        nonlocal backup_root
        if backup_root is None:
            backup_root = reserve_unique_backup_root(
                backup_parent,
                backup_name,
                trusted_parent=root / "Data",
                trust_anchor=root,
            )
        return backup_root

    results: list[AdditionalAppsDedupeResult] = []
    outcomes: list[MutationOutcome] = []
    rollback_errors: list[str] = []
    file_results: list[MutationFileResult] = []
    unsafe_path_error: UnsafeDatabasePathError | None = None
    for platform in platforms:
        try:
            if control is not None:
                control.set_phase(OperationPhase.SCAN)
                control.checkpoint()
            (
                result,
                outcome,
                platform_rollback_errors,
                platform_files,
                platform_unsafe_path_error,
            ) = dedupe_additional_apps_for_platform(
                platform,
                root,
                apply_changes,
                ensure_backup_root,
                run_id,
                control=control,
            )
            results.append(result)
            outcomes.append(outcome)
            rollback_errors.extend(platform_rollback_errors)
            file_results.extend(platform_files)
            if platform_unsafe_path_error is not None:
                unsafe_path_error = platform_unsafe_path_error
                break
            if outcome == MutationOutcome.CANCELLED:
                cancelled_error = result.error or "Operation cancelled"
                break
        except OperationCancelled as exc:
            cancelled_error = str(exc)
            break
        except UnsafeDatabasePathError as exc:
            unsafe_path_error = exc
            results.append(
                AdditionalAppsDedupeResult(
                    platform=platform,
                    state=MutationState.FAILED,
                    error=str(exc),
                )
            )
            outcomes.append(MutationOutcome.FAILED)
            file_results.append(
                MutationFileResult(
                    platform.database_xml.absolute(),
                    state=MutationState.FAILED,
                    error=str(exc),
                )
            )
            break
        except (ET.ParseError, OSError) as exc:
            results.append(
                AdditionalAppsDedupeResult(
                    platform=platform,
                    state=MutationState.FAILED,
                    error=str(exc),
                )
            )
            outcomes.append(MutationOutcome.FAILED)
            file_results.append(
                MutationFileResult(
                    platform.database_xml.absolute(),
                    state=MutationState.FAILED,
                    error=str(exc),
                )
            )

    if unsafe_path_error is not None and (not apply_changes or backup_root is None):
        raise unsafe_path_error

    if control is not None:
        if unsafe_path_error is not None or cancelled_error is not None:
            control.set_phase(OperationPhase.FINALIZE)
        else:
            try:
                control.begin_finalize()
            except OperationCancelled as exc:
                cancelled_error = str(exc)

    if unsafe_path_error is not None:
        committed_count = sum(1 for result in file_results if result.state == MutationState.COMMITTED)
        outcome = MutationOutcome.PARTIAL if committed_count else MutationOutcome.FAILED
    elif cancelled_error is not None:
        outcome = MutationOutcome.CANCELLED
    elif not apply_changes:
        outcome = MutationOutcome.FAILED if MutationOutcome.FAILED in outcomes else MutationOutcome.DRY_RUN
    else:
        committed_count = sum(1 for result in file_results if result.state == MutationState.COMMITTED)
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
    if cancelled_error is not None and cancelled_error not in errors:
        errors.append(cancelled_error)
    run_result = MutationRunResult(
        results,
        outcome,
        " | ".join(errors) or None,
        rollback_errors,
        file_results,
        run_id=run_id,
    )
    if apply_changes:
        changes = [
            {
                "type": "additional_application_dedupe",
                "platform": result.platform.name,
                "xml_path": str(result.platform.database_xml),
                "game_id": duplicate.duplicate.game_id,
                "title": duplicate.duplicate.title,
                "application_path": duplicate.duplicate.application_path,
                "state": duplicate.state.value,
                "error": duplicate.error,
            }
            for result in results
            for duplicate in result.duplicates
        ]
        manifest_root = backup_root if unsafe_path_error is not None else ensure_backup_root()
        if manifest_root is None:
            raise RuntimeError("Backup root was not initialized for apply manifest")
        write_mutation_manifest(
            run_result,
            manifest_root,
            "dedupe_additional_apps",
            changes,
            trust_anchor=root,
        )
    return run_result

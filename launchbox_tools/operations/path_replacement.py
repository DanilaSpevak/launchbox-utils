from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..models import (
    MutationFileResult,
    MutationOutcome,
    MutationRunResult,
    MutationState,
    PathReplacement,
    PathReplacementResult,
    PlatformInfo,
)
from ..mutation_lock import mutation_run_lock
from ..mutation_manifest import write_mutation_manifest
from ..operation_lifecycle import OperationCancelled, OperationControl, OperationPhase
from ..paths import path_key, resolve_launchbox_path
from ..runtime_checks import ensure_safe_to_mutate
from ..safe_write import XmlMutation, execute_xml_transaction, reserve_unique_backup_root
from ..xml_repository import child_text, load_platforms, local_name, parse_xml_tree


_SCAN_CHECKPOINT_INTERVAL = 256


class _PlannedFileIndex:
    """Incrementally keep canonical file states during scanning."""

    def __init__(self) -> None:
        self._files_by_path: dict[Path, MutationFileResult] = {}

    @property
    def files(self) -> list[MutationFileResult]:
        return list(self._files_by_path.values())

    def record_replacement(self, replacement: PathReplacement) -> None:
        path = replacement.xml_path.resolve(strict=False)
        file_result = self._files_by_path.setdefault(path, MutationFileResult(path))
        if replacement.error:
            self.record_error(path, replacement.error)
        replacement._bind_state_source(file_result)

    def record_error(self, path: Path, error: str) -> None:
        resolved_path = path.resolve(strict=False)
        file_result = self._files_by_path.setdefault(
            resolved_path,
            MutationFileResult(resolved_path),
        )
        file_result.state = MutationState.FAILED
        file_result.error = error


def _application_path_child(element: ET.Element) -> ET.Element | None:
    for child in element:
        if local_name(child.tag) == "ApplicationPath":
            return child
    return None


def _folder_child(element: ET.Element) -> ET.Element | None:
    for child in element:
        if local_name(child.tag) == "Folder":
            return child
    return None


def _is_same_or_descendant(path: Path, parent: Path) -> bool:
    path_text = path_key(path)
    parent_text = path_key(parent)
    if path_text == parent_text:
        return True
    parent_prefix = parent_text.rstrip("\\/")
    return path_text.startswith(parent_prefix + os.sep) or path_text.startswith(parent_prefix + "/")


def _relative_suffix(path: Path, old_root: Path) -> Path:
    try:
        return path.resolve(strict=False).relative_to(old_root.resolve(strict=False))
    except ValueError:
        return Path(os.path.relpath(path.resolve(strict=False), old_root.resolve(strict=False)))


def _format_path_like_source(source_value: str, new_resolved_path: Path, root: Path) -> tuple[str | None, str | None]:
    source_value = source_value.strip()
    use_forward_slashes = "/" in source_value

    if Path(source_value).is_absolute():
        output_path = str(new_resolved_path)
    else:
        try:
            output_path = os.path.relpath(new_resolved_path, root)
        except ValueError as exc:
            return None, f"Could not write relative path for {source_value}: {exc}"

    if use_forward_slashes:
        output_path = output_path.replace("\\", "/")
    return output_path, None


def build_replacement_value(root: Path, old_path: Path, new_path: Path, source_value: str) -> tuple[str | None, str | None]:
    resolved_source = resolve_launchbox_path(root, source_value)
    old_resolved = old_path.resolve(strict=False)
    if not _is_same_or_descendant(resolved_source, old_resolved):
        return None, None

    suffix = _relative_suffix(resolved_source, old_resolved)
    new_value_path = new_path if str(suffix) == "." else new_path / suffix
    return _format_path_like_source(source_value, new_value_path, root)


def _append_replacement(
    result: PathReplacementResult,
    root: Path,
    old_path: Path,
    new_path: Path,
    xml_path: Path,
    entry_type: str,
    title: str,
    source_value: str,
    apply_changes: bool,
    target_element: ET.Element,
    planned_files: _PlannedFileIndex,
) -> bool:
    new_value, error = build_replacement_value(root, old_path, new_path, source_value)
    if new_value is None and error is None:
        return False

    replacement = PathReplacement(
        platform=result.platform,
        xml_path=xml_path,
        entry_type=entry_type,
        title=title,
        old_value=source_value,
        new_value=new_value or "",
        state=MutationState.FAILED if error else MutationState.PLANNED,
        error=error,
    )
    result.replacements.append(replacement)
    planned_files.record_replacement(replacement)
    if error:
        result.warnings.append(error)
        return False

    target_element.text = new_value
    return True


def _collect_platform_folder_replacements(
    platforms_tree: ET.ElementTree,
    result_by_platform: dict[str, PathReplacementResult],
    root: Path,
    old_path: Path,
    new_path: Path,
    apply_changes: bool,
    platform_filter: str | None,
    planned_files: _PlannedFileIndex,
    control: OperationControl | None = None,
) -> bool:
    changed = False
    platforms_xml = root / "Data" / "Platforms.xml"
    for element in platforms_tree.getroot().iter():
        if control is not None:
            control.checkpoint()
        if local_name(element.tag) != "Platform":
            continue

        platform_name = child_text(element, "Name")
        if platform_filter and platform_name.casefold() != platform_filter.casefold():
            continue

        result = result_by_platform.get(platform_name.casefold())
        folder_element = _folder_child(element)
        if result is None or folder_element is None:
            continue

        folder_value = (folder_element.text or "").strip()
        if not folder_value:
            continue

        changed = (
            _append_replacement(
                result,
                root,
                old_path,
                new_path,
                platforms_xml,
                "PlatformFolder",
                platform_name,
                folder_value,
                apply_changes,
                folder_element,
                planned_files,
            )
            or changed
        )
    return changed


def _collect_application_path_replacements(
    platform: PlatformInfo,
    result: PathReplacementResult,
    root: Path,
    old_path: Path,
    new_path: Path,
    apply_changes: bool,
    planned_files: _PlannedFileIndex,
    control: OperationControl | None = None,
) -> tuple[ET.ElementTree | None, bool]:
    if control is not None:
        control.checkpoint()
    if not platform.database_xml.exists():
        result.warnings.append(f"Platform XML not found: {platform.database_xml}")
        return None, False

    tree = parse_xml_tree(platform.database_xml, control=control)
    changed = False
    for element in tree.getroot().iter():
        if control is not None:
            control.checkpoint()
        entry_type = local_name(element.tag)
        if entry_type not in {"Game", "AdditionalApplication"}:
            continue

        path_element = _application_path_child(element)
        if path_element is None:
            title = child_text(element, "Title") or child_text(element, "Name") or "<untitled>"
            result.warnings.append(f"{entry_type} has no ApplicationPath: {title}")
            continue

        application_path = (path_element.text or "").strip()
        if not application_path:
            title = child_text(element, "Title") or child_text(element, "Name") or "<untitled>"
            result.warnings.append(f"{entry_type} has no ApplicationPath: {title}")
            continue

        title = child_text(element, "Title") or child_text(element, "Name") or "<untitled>"
        changed = (
            _append_replacement(
                result,
                root,
                old_path,
                new_path,
                platform.database_xml,
                entry_type,
                title,
                application_path,
                apply_changes,
                path_element,
                planned_files,
            )
            or changed
        )
    return tree, changed


def run_path_replacement(
    root: Path,
    old_path: Path,
    new_path: Path,
    platform_filter: str | None = None,
    apply_changes: bool = False,
    *,
    control: OperationControl | None = None,
) -> MutationRunResult[PathReplacementResult]:
    resolved_root = root.resolve(strict=False)
    if not apply_changes:
        return _run_path_replacement(
            resolved_root,
            old_path,
            new_path,
            platform_filter,
            apply_changes=False,
            control=control,
        )

    run_id = str(uuid4())
    with mutation_run_lock(resolved_root, "replace_paths", run_id):
        return _run_path_replacement(
            resolved_root,
            old_path,
            new_path,
            platform_filter,
            apply_changes=True,
            run_id=run_id,
            control=control,
        )


def _run_path_replacement(
    root: Path,
    old_path: Path,
    new_path: Path,
    platform_filter: str | None = None,
    apply_changes: bool = False,
    run_id: str | None = None,
    control: OperationControl | None = None,
) -> MutationRunResult[PathReplacementResult]:
    root = root.resolve(strict=False)
    old_path = old_path.expanduser().resolve(strict=False)
    new_path = new_path.expanduser().resolve(strict=False)
    if control is not None:
        control.set_phase(OperationPhase.SCAN)
    platforms: list[PlatformInfo] = []
    result_by_platform: dict[str, PathReplacementResult] = {}
    results: list[PathReplacementResult] = []
    planned_file_index = _PlannedFileIndex()
    platforms_xml = root / "Data" / "Platforms.xml"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_parent = root / "Data" / "Backups"
    backup_name = f"PathReplacement-{timestamp}"
    if run_id is not None:
        backup_name = f"{backup_name}-{run_id}"
    backup_root = backup_parent / backup_name

    try:
        platforms = load_platforms(root, control=control)
        if platform_filter:
            platforms = [
                platform
                for platform in platforms
                if platform.name.casefold() == platform_filter.casefold()
            ]
        result_by_platform = {
            platform.name.casefold(): PathReplacementResult(platform=platform)
            for platform in platforms
        }
        results = list(result_by_platform.values())

        if apply_changes:
            xml_paths = [platforms_xml]
            xml_paths.extend(
                platform.database_xml
                for platform in platforms
                if platform.database_xml.exists()
            )
            ensure_safe_to_mutate(xml_paths)

        if control is not None:
            control.checkpoint()
        platforms_tree = parse_xml_tree(platforms_xml, control=control)
        platforms_changed = _collect_platform_folder_replacements(
            platforms_tree,
            result_by_platform,
            root,
            old_path,
            new_path,
            apply_changes,
            platform_filter,
            planned_file_index,
            control,
        )

        platform_trees: list[tuple[PathReplacementResult, ET.ElementTree, bool]] = []
        for platform in platforms:
            if control is not None:
                control.checkpoint()
            result = result_by_platform[platform.name.casefold()]
            try:
                tree, changed = _collect_application_path_replacements(
                    platform,
                    result,
                    root,
                    old_path,
                    new_path,
                    apply_changes,
                    planned_file_index,
                    control,
                )
                if tree is not None:
                    platform_trees.append((result, tree, changed))
            except (ET.ParseError, OSError) as exc:
                result.error = str(exc)
                planned_file_index.record_error(result.platform.database_xml, result.error)
    except OperationCancelled as exc:
        # Cancellation has won; finish the terminal snapshot without further
        # checkpoints so its manifest covers every replacement found by scan.
        planned_files = planned_file_index.files
        run_result = MutationRunResult(
            results,
            MutationOutcome.CANCELLED,
            str(exc),
            files=planned_files,
            run_id=run_id,
        )
        if control is not None:
            control.set_phase(OperationPhase.FINALIZE)
        if apply_changes:
            backup_root = reserve_unique_backup_root(backup_parent, backup_name)
            _write_path_replacement_manifest(run_result, backup_root)
        return run_result

    try:
        planning_errors: list[str] = []
        planning_item_count = 0
        for result in results:
            if control is not None:
                control.checkpoint()
            result.warnings.sort()
            if control is not None:
                control.checkpoint()
            if result.error:
                planning_errors.append(result.error)
            for replacement in result.replacements:
                planning_item_count += 1
                if (
                    control is not None
                    and planning_item_count % _SCAN_CHECKPOINT_INTERVAL == 0
                ):
                    control.checkpoint()
                if replacement.error:
                    planning_errors.append(replacement.error)
        planned_files = planned_file_index.files
    except OperationCancelled as exc:
        planned_files = planned_file_index.files
        run_result = MutationRunResult(
            results,
            MutationOutcome.CANCELLED,
            str(exc),
            files=planned_files,
            run_id=run_id,
        )
        if control is not None:
            control.set_phase(OperationPhase.FINALIZE)
        if apply_changes:
            backup_root = reserve_unique_backup_root(backup_parent, backup_name)
            _write_path_replacement_manifest(run_result, backup_root)
        return run_result

    if planning_errors:
        try:
            if control is not None:
                control.begin_finalize()
        except OperationCancelled as exc:
            run_result = MutationRunResult(
                results,
                MutationOutcome.CANCELLED,
                str(exc),
                files=planned_files,
                run_id=run_id,
            )
            if apply_changes:
                backup_root = reserve_unique_backup_root(backup_parent, backup_name)
                _write_path_replacement_manifest(run_result, backup_root)
            return run_result

        run_result = MutationRunResult(
            results,
            MutationOutcome.FAILED,
            " | ".join(planning_errors),
            files=planned_files,
            run_id=run_id,
        )
        if apply_changes:
            backup_root = reserve_unique_backup_root(backup_parent, backup_name)
            _write_path_replacement_manifest(run_result, backup_root)
        return run_result

    if not apply_changes:
        try:
            if control is not None:
                control.begin_finalize()
        except OperationCancelled as exc:
            return MutationRunResult(
                results,
                MutationOutcome.CANCELLED,
                str(exc),
                files=planned_files,
            )
        return MutationRunResult(results, MutationOutcome.DRY_RUN, files=planned_files)

    backup_root = reserve_unique_backup_root(backup_parent, backup_name)
    mutations: list[XmlMutation] = []
    if platforms_changed:
        mutations.append(XmlMutation(platforms_xml, platforms_tree))
    mutations.extend(
        XmlMutation(result.platform.database_xml, tree)
        for result, tree, changed in platform_trees
        if changed
    )
    transaction = execute_xml_transaction(mutations, backup_root, run_id, control=control)
    late_cancel_error: str | None = None
    if control is not None:
        if transaction.outcome == MutationOutcome.CANCELLED:
            control.set_phase(OperationPhase.FINALIZE)
        else:
            try:
                control.begin_finalize()
            except OperationCancelled as exc:
                late_cancel_error = str(exc)

    if transaction.outcome != MutationOutcome.SUCCESS:
        for result in results:
            if result.replacements:
                result.error = transaction.error

    run_result = MutationRunResult(
        results,
        MutationOutcome.CANCELLED if late_cancel_error is not None else transaction.outcome,
        late_cancel_error or transaction.error,
        transaction.rollback_errors,
        transaction.files,
        run_id=run_id,
    )
    _write_path_replacement_manifest(
        run_result,
        backup_root,
        backup_paths=transaction.backup_paths,
    )
    return run_result


def _write_path_replacement_manifest(
    run_result: MutationRunResult[PathReplacementResult],
    backup_root: Path,
    *,
    backup_paths: dict[Path, Path] | None = None,
) -> None:
    files_by_path = {
        file_result.path.resolve(strict=False): file_result
        for file_result in run_result.files
    }
    transaction_backups = backup_paths or {}
    changes: list[dict[str, object]] = []
    for result in run_result.results:
        result_backups: list[Path] = []
        seen_backup_paths: set[Path] = set()
        for replacement in result.replacements:
            resolved_path = replacement.xml_path.resolve(strict=False)
            file_result = files_by_path.get(resolved_path)
            if file_result is not None:
                replacement._bind_state_source(file_result)
            if resolved_path not in seen_backup_paths:
                backup_path = transaction_backups.get(resolved_path)
                if backup_path is not None:
                    result_backups.append(backup_path)
                    seen_backup_paths.add(resolved_path)
            changes.append(
                {
                    "type": "path_replacement",
                    "platform": result.platform.name,
                    "xml_path": str(replacement.xml_path),
                    "entry_type": replacement.entry_type,
                    "title": replacement.title,
                    "old_value": replacement.old_value,
                    "new_value": replacement.new_value,
                    "state": replacement.state.value,
                    "error": replacement.error or result.error,
                }
            )
        result.backup_paths = result_backups
    write_mutation_manifest(run_result, backup_root, "replace_paths", changes)

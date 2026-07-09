from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from ..models import PathReplacement, PathReplacementResult, PlatformInfo
from ..paths import path_key, resolve_launchbox_path
from ..runtime_checks import ensure_safe_to_mutate
from ..safe_write import backup_xml_file, write_xml_tree_safely
from ..xml_repository import child_text, load_platforms, local_name, parse_xml_tree


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
        applied=apply_changes and error is None,
        error=error,
    )
    result.replacements.append(replacement)
    if error:
        result.warnings.append(error)
        return False

    if apply_changes:
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
) -> bool:
    changed = False
    platforms_xml = root / "Data" / "Platforms.xml"
    for element in platforms_tree.getroot().iter():
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
) -> tuple[ET.ElementTree | None, bool]:
    if not platform.database_xml.exists():
        result.warnings.append(f"Platform XML not found: {platform.database_xml}")
        return None, False

    tree = parse_xml_tree(platform.database_xml)
    changed = False
    for element in tree.getroot().iter():
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
) -> list[PathReplacementResult]:
    root = root.resolve(strict=False)
    old_path = old_path.expanduser().resolve(strict=False)
    new_path = new_path.expanduser().resolve(strict=False)
    platforms = load_platforms(root)
    if platform_filter:
        platforms = [platform for platform in platforms if platform.name.casefold() == platform_filter.casefold()]

    result_by_platform = {
        platform.name.casefold(): PathReplacementResult(platform=platform)
        for platform in platforms
    }
    results = list(result_by_platform.values())

    platforms_xml = root / "Data" / "Platforms.xml"
    if apply_changes:
        xml_paths = [platforms_xml]
        xml_paths.extend(platform.database_xml for platform in platforms if platform.database_xml.exists())
        ensure_safe_to_mutate(xml_paths)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = root / "Data" / "Backups" / f"PathReplacement-{timestamp}"

    platforms_tree = parse_xml_tree(platforms_xml)
    platforms_changed = _collect_platform_folder_replacements(
        platforms_tree,
        result_by_platform,
        root,
        old_path,
        new_path,
        apply_changes,
        platform_filter,
    )

    platform_trees: list[tuple[PathReplacementResult, ET.ElementTree, bool]] = []
    for platform in platforms:
        result = result_by_platform[platform.name.casefold()]
        try:
            tree, changed = _collect_application_path_replacements(
                platform,
                result,
                root,
                old_path,
                new_path,
                apply_changes,
            )
            if tree is not None:
                platform_trees.append((result, tree, changed))
        except (ET.ParseError, OSError) as exc:
            result.error = str(exc)

    if apply_changes and platforms_changed:
        backup_path = backup_xml_file(platforms_xml, backup_root)
        for result in results:
            if any(replacement.xml_path == platforms_xml and replacement.applied for replacement in result.replacements):
                result.backup_paths.append(backup_path)
        write_xml_tree_safely(platforms_tree, platforms_xml)

    if apply_changes:
        for result, tree, changed in platform_trees:
            if not changed:
                continue
            try:
                backup_path = backup_xml_file(result.platform.database_xml, backup_root)
                result.backup_paths.append(backup_path)
                write_xml_tree_safely(tree, result.platform.database_xml)
                result.applied = True
            except (ET.ParseError, OSError) as exc:
                result.error = str(exc)

        if platforms_changed:
            for result in results:
                if any(replacement.xml_path == platforms_xml and replacement.applied for replacement in result.replacements):
                    result.applied = True

    for result in results:
        result.warnings.sort()
    return results

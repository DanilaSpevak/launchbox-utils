from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .models import GameEntry, PlatformInfo
from .operation_lifecycle import OperationControl
from .paths import (
    ensure_platform_database_path,
    platform_database_path,
    platforms_metadata_path,
    resolve_launchbox_path,
)
from .xml_checkpoint_io import XML_CHECKPOINT_INTERVAL, parse_xml_tree_with_checkpoints


def _checkpoint_periodically(
    control: OperationControl | None,
    index: int,
) -> None:
    if control is not None and index % XML_CHECKPOINT_INTERVAL == 0:
        control.checkpoint()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(element: ET.Element, child_name: str) -> str:
    for child in element:
        if local_name(child.tag) == child_name:
            return (child.text or "").strip()
    return ""


def parse_xml(
    path: Path,
    *,
    control: OperationControl | None = None,
) -> ET.Element:
    return parse_xml_tree(path, control=control).getroot()


def parse_xml_tree(
    path: Path,
    *,
    control: OperationControl | None = None,
) -> ET.ElementTree:
    if control is None:
        return ET.parse(path)

    return parse_xml_tree_with_checkpoints(path, control)


def load_platforms(
    root: Path,
    *,
    control: OperationControl | None = None,
) -> list[PlatformInfo]:
    platforms_xml = platforms_metadata_path(root)
    xml_root = parse_xml(platforms_xml, control=control)
    platforms: list[PlatformInfo] = []

    for index, element in enumerate(xml_root.iter(), start=1):
        _checkpoint_periodically(control, index)
        if local_name(element.tag) != "Platform":
            continue

        name = child_text(element, "Name")
        raw_folder = child_text(element, "Folder")
        if not name:
            platform_database_path(root, name)

        folder = resolve_launchbox_path(root, raw_folder) if raw_folder else root
        platforms.append(
            PlatformInfo(
                name=name,
                folder=folder,
                database_xml=platform_database_path(root, name),
                raw_folder=raw_folder,
            )
        )

    return platforms


def load_application_entries(
    platform: PlatformInfo,
    root: Path,
    xml_root: ET.Element | None = None,
    include_xml_links: bool = False,
    *,
    control: OperationControl | None = None,
) -> tuple[list[GameEntry], list[str]]:
    if control is not None:
        control.checkpoint()
    warnings: list[str] = []
    database_xml = ensure_platform_database_path(root, platform.name, platform.database_xml)
    if not database_xml.exists():
        return [], [f"Platform XML not found: {database_xml}"]

    if xml_root is None:
        xml_root = parse_xml(database_xml, control=control)

    parent_by_child: dict[int, ET.Element] = {}
    if include_xml_links:
        child_index = 0
        for index, parent in enumerate(xml_root.iter(), start=1):
            _checkpoint_periodically(control, index)
            for child in parent:
                child_index += 1
                _checkpoint_periodically(control, child_index)
                parent_by_child[id(child)] = parent

    entries: list[GameEntry] = []

    for index, element in enumerate(xml_root.iter(), start=1):
        _checkpoint_periodically(control, index)
        entry_type = local_name(element.tag)
        if entry_type not in {"Game", "AdditionalApplication"}:
            continue

        application_path = child_text(element, "ApplicationPath")
        if not application_path:
            title = child_text(element, "Title") or child_text(element, "Name") or "<untitled>"
            warnings.append(f"{entry_type} has no ApplicationPath: {title}")
            continue

        title = child_text(element, "Title") or child_text(element, "Name") or "<untitled>"
        entries.append(
            GameEntry(
                title=title,
                application_path=application_path,
                resolved_path=resolve_launchbox_path(root, application_path),
                entry_type=entry_type,
                game_id=child_text(element, "GameID"),
                element=element if include_xml_links else None,
                parent=parent_by_child.get(id(element)) if include_xml_links else None,
            )
        )

    return entries, warnings


def load_games(
    platform: PlatformInfo,
    root: Path,
    *,
    control: OperationControl | None = None,
) -> tuple[list[GameEntry], list[str]]:
    return load_application_entries(platform, root, control=control)

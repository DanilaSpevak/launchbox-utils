from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .models import GameEntry, PlatformInfo
from .paths import platform_database_path, resolve_launchbox_path


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(element: ET.Element, child_name: str) -> str:
    for child in element:
        if local_name(child.tag) == child_name:
            return (child.text or "").strip()
    return ""


def parse_xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def parse_xml_tree(path: Path) -> ET.ElementTree:
    return ET.parse(path)


def load_platforms(root: Path) -> list[PlatformInfo]:
    platforms_xml = root / "Data" / "Platforms.xml"
    xml_root = parse_xml(platforms_xml)
    platforms: list[PlatformInfo] = []

    for element in xml_root.iter():
        if local_name(element.tag) != "Platform":
            continue

        name = child_text(element, "Name")
        raw_folder = child_text(element, "Folder")
        if not name:
            continue

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
) -> tuple[list[GameEntry], list[str]]:
    warnings: list[str] = []
    if not platform.database_xml.exists():
        return [], [f"Platform XML not found: {platform.database_xml}"]

    if xml_root is None:
        xml_root = parse_xml(platform.database_xml)

    parent_by_child: dict[int, ET.Element] = {}
    if include_xml_links:
        parent_by_child = {id(child): parent for parent in xml_root.iter() for child in parent}

    entries: list[GameEntry] = []

    for element in xml_root.iter():
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


def load_games(platform: PlatformInfo, root: Path) -> tuple[list[GameEntry], list[str]]:
    return load_application_entries(platform, root)

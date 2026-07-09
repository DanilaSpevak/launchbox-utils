from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import PlatformInfo
from .runtime_checks import ensure_safe_to_mutate


def backup_xml_file(xml_path: Path, backup_root: Path) -> Path:
    ensure_safe_to_mutate([xml_path])
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / xml_path.name
    shutil.copy2(xml_path, backup_path)
    return backup_path


def backup_platform_xml(platform: PlatformInfo, backup_root: Path) -> Path:
    return backup_xml_file(platform.database_xml, backup_root)


def write_xml_tree_safely(tree: ET.ElementTree, destination: Path) -> None:
    ensure_safe_to_mutate([destination])
    temp_path = destination.with_name(f"{destination.name}.tmp")
    tree.write(temp_path, encoding="utf-8", xml_declaration=True)
    ET.parse(temp_path)
    os.replace(temp_path, destination)

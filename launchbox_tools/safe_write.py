from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import PlatformInfo


def backup_platform_xml(platform: PlatformInfo, backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / platform.database_xml.name
    shutil.copy2(platform.database_xml, backup_path)
    return backup_path


def write_xml_tree_safely(tree: ET.ElementTree, destination: Path) -> None:
    temp_path = destination.with_name(f"{destination.name}.tmp")
    tree.write(temp_path, encoding="utf-8", xml_declaration=True)
    ET.parse(temp_path)
    os.replace(temp_path, destination)

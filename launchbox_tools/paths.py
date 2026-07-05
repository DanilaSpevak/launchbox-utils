from __future__ import annotations

import os
from pathlib import Path

from .config import WINDOWS_INVALID_FILENAME_CHARS


def resolve_launchbox_path(root: Path, raw_path: str) -> Path:
    raw_path = (raw_path or "").strip().strip('"')
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=False)


def path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def safe_report_dir_name(platform_name: str) -> str:
    safe_name = "".join("_" if char in WINDOWS_INVALID_FILENAME_CHARS else char for char in platform_name)
    safe_name = safe_name.strip().rstrip(".")
    return safe_name or "Unnamed Platform"


def platform_database_path(root: Path, platform_name: str) -> Path:
    return root / "Data" / "Platforms" / f"{platform_name}.xml"


def resolve_output_dir(root: Path, raw_output: str) -> Path:
    output_dir = Path(raw_output)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    return output_dir.resolve(strict=False)

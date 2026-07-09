from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PlatformInfo:
    name: str
    folder: Path
    database_xml: Path
    raw_folder: str = ""


@dataclass(frozen=True)
class GameEntry:
    title: str
    application_path: str
    resolved_path: Path
    entry_type: str = "Game"
    game_id: str = ""
    element: ET.Element | None = field(default=None, compare=False)
    parent: ET.Element | None = field(default=None, compare=False)


@dataclass
class PlatformAuditResult:
    platform: PlatformInfo
    missing_on_disk: list[GameEntry] = field(default_factory=list)
    not_in_database: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    database_count: int = 0
    folder_count: int = 0


@dataclass(frozen=True)
class AdditionalApplicationDuplicate:
    platform: PlatformInfo
    kept: GameEntry
    duplicate: GameEntry
    key: tuple[str, str]


@dataclass
class AdditionalAppsDedupeResult:
    platform: PlatformInfo
    duplicates: list[AdditionalApplicationDuplicate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backup_path: Path | None = None
    applied: bool = False
    error: str | None = None


@dataclass
class PathReplacement:
    platform: PlatformInfo
    xml_path: Path
    entry_type: str
    title: str
    old_value: str
    new_value: str
    applied: bool = False
    error: str | None = None


@dataclass
class PathReplacementResult:
    platform: PlatformInfo
    replacements: list[PathReplacement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backup_paths: list[Path] = field(default_factory=list)
    applied: bool = False
    error: str | None = None

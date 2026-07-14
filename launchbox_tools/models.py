from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Generic, TypeVar


class MutationOutcome(str, Enum):
    DRY_RUN = "dry_run"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


class MutationState(str, Enum):
    PLANNED = "planned"
    PREPARED = "prepared"
    COMMITTED = "committed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class MutationFileResult:
    path: Path
    state: MutationState = MutationState.PLANNED
    backup_path: Path | None = None
    error: str | None = None
    rollback_error: str | None = None
    source_sha256: str | None = None


T = TypeVar("T")


@dataclass
class MutationRunResult(Generic[T]):
    results: list[T]
    outcome: MutationOutcome
    error: str | None = None
    rollback_errors: list[str] = field(default_factory=list)
    files: list[MutationFileResult] = field(default_factory=list)
    manifest_path: Path | None = None
    manifest_error: str | None = None
    run_id: str | None = None


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
    state: MutationState = MutationState.PLANNED
    error: str | None = None


@dataclass(frozen=True)
class AdditionalApplicationAmbiguity:
    platform: PlatformInfo
    key: tuple[str, str]
    variants: tuple[GameEntry, ...]
    differing_fields: tuple[str, ...]


@dataclass
class AdditionalAppsDedupeResult:
    platform: PlatformInfo
    duplicates: list[AdditionalApplicationDuplicate] = field(default_factory=list)
    ambiguities: list[AdditionalApplicationAmbiguity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backup_path: Path | None = None
    state: MutationState | None = None
    error: str | None = None
    error_reason: str | None = None
    error_details: str | None = None


@dataclass
class PathReplacement:
    platform: PlatformInfo
    xml_path: Path
    entry_type: str
    title: str
    old_value: str
    new_value: str
    state: MutationState = MutationState.PLANNED
    error: str | None = None

    def __getattribute__(self, name: str):
        # Keep the state source outside dataclass fields so repr, equality,
        # asdict(), replace(), and the public constructor retain their shape.
        if name == "state":
            attributes = object.__getattribute__(self, "__dict__")
            state_source = attributes.get("_state_source")
            if state_source is not None:
                return state_source.state
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "state":
            object.__getattribute__(self, "__dict__").pop("_state_source", None)
        object.__setattr__(self, name, value)

    def _bind_state_source(self, file_result: MutationFileResult) -> None:
        """Read state live from the canonical result for this XML file."""
        object.__setattr__(self, "_state_source", file_result)


@dataclass
class PathReplacementResult:
    platform: PlatformInfo
    replacements: list[PathReplacement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backup_paths: list[Path] = field(default_factory=list)
    error: str | None = None
    error_reason: str | None = None
    error_details: str | None = None

from __future__ import annotations

from pathlib import Path

from ..models import GameEntry, PlatformAuditResult, PlatformInfo
from ..operation_lifecycle import OperationControl, OperationPhase
from ..paths import path_key
from ..xml_repository import load_games, load_platforms


_SCAN_CHECKPOINT_INTERVAL = 256


def _find_missing_on_disk(
    games: list[GameEntry],
    *,
    control: OperationControl | None = None,
) -> list[GameEntry]:
    missing: list[GameEntry] = []
    for game in games:
        if control is not None:
            control.checkpoint()
        if not game.resolved_path.exists():
            missing.append(game)
    return missing


def scan_folder(
    folder: Path,
    *,
    control: OperationControl | None = None,
) -> tuple[dict[str, Path], list[str]]:
    if not folder.exists():
        return {}, [f"ROM folder not found: {folder}"]
    if not folder.is_dir():
        return {}, [f"ROM folder is not a directory: {folder}"]

    files: dict[str, Path] = {}
    warnings: list[str] = []
    try:
        for path in folder.rglob("*"):
            if control is not None:
                control.checkpoint()
            if path.is_file():
                files[path_key(path)] = path.resolve(strict=False)
    except OSError as exc:
        warnings.append(f"Could not fully scan ROM folder {folder}: {exc}")

    return files, warnings


def audit_platform(
    platform: PlatformInfo,
    root: Path,
    *,
    control: OperationControl | None = None,
) -> PlatformAuditResult:
    if control is not None:
        control.checkpoint()
    result = PlatformAuditResult(platform=platform)

    if not platform.raw_folder:
        result.warnings.append("Platform has an empty Folder value")

    games, game_warnings = load_games(platform, root, control=control)
    result.warnings.extend(game_warnings)
    result.database_count = len(games)

    database_paths: dict[str, GameEntry] = {}
    for game in games:
        if control is not None:
            control.checkpoint()
        database_paths[path_key(game.resolved_path)] = game

    if not platform.raw_folder:
        result.missing_on_disk = _find_missing_on_disk(games, control=control)
        result.missing_on_disk.sort(key=lambda game: (str(game.resolved_path).lower(), game.title.lower()))
        result.warnings.sort()
        return result

    folder_paths, folder_warnings = scan_folder(platform.folder, control=control)
    result.warnings.extend(folder_warnings)
    result.folder_count = len(folder_paths)
    result.missing_on_disk = _find_missing_on_disk(games, control=control)

    for index, (key, path) in enumerate(folder_paths.items(), start=1):
        if control is not None and index % _SCAN_CHECKPOINT_INTERVAL == 0:
            control.checkpoint()
        if key not in database_paths:
            result.not_in_database.append(path)

    result.missing_on_disk.sort(key=lambda game: (str(game.resolved_path).lower(), game.title.lower()))
    result.not_in_database.sort(key=lambda path: str(path).lower())
    result.warnings.sort()
    return result


def run_audit(
    root: Path,
    *,
    control: OperationControl | None = None,
) -> list[PlatformAuditResult]:
    root = root.resolve(strict=False)
    if control is not None:
        control.set_phase(OperationPhase.SCAN)
        control.checkpoint()
    platforms = load_platforms(root, control=control)
    results: list[PlatformAuditResult] = []
    for platform in platforms:
        if control is not None:
            control.checkpoint()
        results.append(audit_platform(platform, root, control=control))
    return results

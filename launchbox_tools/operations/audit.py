from __future__ import annotations

from pathlib import Path

from ..models import GameEntry, PlatformAuditResult, PlatformInfo
from ..paths import path_key
from ..xml_repository import load_games, load_platforms


def scan_folder(folder: Path) -> tuple[dict[str, Path], list[str]]:
    if not folder.exists():
        return {}, [f"ROM folder not found: {folder}"]
    if not folder.is_dir():
        return {}, [f"ROM folder is not a directory: {folder}"]

    files: dict[str, Path] = {}
    warnings: list[str] = []
    try:
        for path in folder.rglob("*"):
            if path.is_file():
                files[path_key(path)] = path.resolve(strict=False)
    except OSError as exc:
        warnings.append(f"Could not fully scan ROM folder {folder}: {exc}")

    return files, warnings


def audit_platform(platform: PlatformInfo, root: Path) -> PlatformAuditResult:
    result = PlatformAuditResult(platform=platform)

    if not platform.raw_folder:
        result.warnings.append("Platform has an empty Folder value")

    games, game_warnings = load_games(platform, root)
    result.warnings.extend(game_warnings)
    result.database_count = len(games)

    database_paths: dict[str, GameEntry] = {}
    for game in games:
        database_paths[path_key(game.resolved_path)] = game

    if not platform.raw_folder:
        result.missing_on_disk = [game for game in games if not game.resolved_path.exists()]
        result.missing_on_disk.sort(key=lambda game: (str(game.resolved_path).lower(), game.title.lower()))
        result.warnings.sort()
        return result

    folder_paths, folder_warnings = scan_folder(platform.folder)
    result.warnings.extend(folder_warnings)
    result.folder_count = len(folder_paths)

    if folder_warnings:
        result.missing_on_disk = list(games)
    else:
        result.missing_on_disk = [game for game in games if not game.resolved_path.exists()]

    for key, path in folder_paths.items():
        if key not in database_paths:
            result.not_in_database.append(path)

    result.missing_on_disk.sort(key=lambda game: (str(game.resolved_path).lower(), game.title.lower()))
    result.not_in_database.sort(key=lambda path: str(path).lower())
    result.warnings.sort()
    return result


def run_audit(root: Path) -> list[PlatformAuditResult]:
    root = root.resolve(strict=False)
    platforms = load_platforms(root)
    return [audit_platform(platform, root) for platform in platforms]

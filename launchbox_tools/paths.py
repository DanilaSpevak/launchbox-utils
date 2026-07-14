from __future__ import annotations

import os
import stat
from pathlib import Path
from pathlib import PureWindowsPath

from .config import WINDOWS_INVALID_FILENAME_CHARS


_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_WINDOWS_MAX_COMPONENT_UTF16_UNITS = 255
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_DEVICE_DIGIT_TRANSLATION = str.maketrans({"¹": "1", "²": "2", "³": "3"})


class UnsafeDatabasePathError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason: str,
        path: Path | None = None,
        platform_name: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.path = path
        self.platform_name = platform_name
        self.detail = detail


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


def _raise_invalid_platform_name(platform_name: str, detail: str) -> None:
    raise UnsafeDatabasePathError(
        f"Invalid LaunchBox platform name {platform_name!r}: {detail}",
        reason="invalid_platform_name",
        platform_name=platform_name,
        detail=detail,
    )


def validate_platform_name(platform_name: str) -> None:
    if not platform_name:
        _raise_invalid_platform_name(platform_name, "the name is empty")

    windows_path = PureWindowsPath(platform_name)
    if (
        platform_name in {".", ".."}
        or windows_path.anchor
        or len(windows_path.parts) != 1
    ):
        _raise_invalid_platform_name(platform_name, "the name must be one relative path component")

    invalid_character = next(
        (
            character
            for character in platform_name
            if character in WINDOWS_INVALID_FILENAME_CHARS or ord(character) < 32
        ),
        None,
    )
    if invalid_character is not None:
        _raise_invalid_platform_name(
            platform_name,
            f"the name contains an invalid Windows filename character {invalid_character!r}",
        )

    if platform_name.endswith((" ", ".")):
        _raise_invalid_platform_name(platform_name, "the name ends with a space or period")

    device_name = (
        platform_name.split(".", 1)[0]
        .rstrip(" ")
        .translate(_WINDOWS_DEVICE_DIGIT_TRANSLATION)
        .upper()
    )
    if device_name in _WINDOWS_RESERVED_NAMES:
        _raise_invalid_platform_name(platform_name, "the name is reserved by Windows")

    filename = f"{platform_name}.xml"
    try:
        utf16_units = len(filename.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        _raise_invalid_platform_name(platform_name, "the name is not valid UTF-16")
    if utf16_units > _WINDOWS_MAX_COMPONENT_UTF16_UNITS:
        _raise_invalid_platform_name(
            platform_name,
            "the generated XML filename exceeds 255 UTF-16 code units",
        )


def _absolute_normalized(path: Path) -> Path:
    return Path(os.path.abspath(path))


def normalize_trust_anchor(path: Path) -> Path:
    """Keep the caller's absolute path spelling unless the root itself is an alias."""
    absolute = _absolute_normalized(path)
    try:
        metadata = os.lstat(absolute)
    except FileNotFoundError:
        return absolute

    is_reparse = bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )
    if is_reparse or stat.S_ISLNK(metadata.st_mode):
        return absolute.resolve(strict=False)
    return absolute


def _raise_unsafe_path(
    *,
    reason: str,
    path: Path,
    platform_name: str | None,
    detail: str,
) -> None:
    label = f" for platform {platform_name!r}" if platform_name is not None else ""
    raise UnsafeDatabasePathError(
        f"Unsafe LaunchBox database path{label}: {path} ({detail})",
        reason=reason,
        path=path,
        platform_name=platform_name,
        detail=detail,
    )


def ensure_trusted_direct_child(
    trust_anchor: Path,
    trusted_parent: Path,
    destination: Path,
    *,
    platform_name: str | None = None,
) -> Path:
    source_anchor = _absolute_normalized(trust_anchor)
    anchor = normalize_trust_anchor(trust_anchor)
    parent = _absolute_normalized(trusted_parent)
    candidate = _absolute_normalized(destination)

    if anchor != source_anchor:
        parent = parent.resolve(strict=False)
        candidate = candidate.resolve(strict=False)

    try:
        parent.relative_to(anchor)
    except ValueError:
        _raise_unsafe_path(
            reason="outside_trusted_directory",
            path=candidate,
            platform_name=platform_name,
            detail=f"trusted parent is outside canonical root {anchor}",
        )

    if candidate.parent != parent:
        _raise_unsafe_path(
            reason="outside_trusted_directory",
            path=candidate,
            platform_name=platform_name,
            detail=f"path is not an immediate child of {parent}",
        )

    try:
        relative_candidate = candidate.relative_to(anchor)
    except ValueError:
        _raise_unsafe_path(
            reason="outside_trusted_directory",
            path=candidate,
            platform_name=platform_name,
            detail=f"path is outside canonical root {anchor}",
        )

    current = anchor
    for component in relative_candidate.parts:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            _raise_unsafe_path(
                reason="path_metadata_error",
                path=current,
                platform_name=platform_name,
                detail=f"path metadata could not be read: {exc}",
            )

        is_reparse = bool(
            getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        )
        is_symlink = stat.S_ISLNK(metadata.st_mode)
        if is_reparse or is_symlink:
            _raise_unsafe_path(
                reason="reparse_point",
                path=current,
                platform_name=platform_name,
                detail="reparse points, junctions, and symbolic links are not trusted",
            )

    try:
        canonical_anchor = anchor.resolve(strict=False)
        canonical_parent = parent.resolve(strict=False)
        canonical_candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        _raise_unsafe_path(
            reason="path_metadata_error",
            path=candidate,
            platform_name=platform_name,
            detail=f"canonical path could not be determined: {exc}",
        )

    try:
        canonical_parent.relative_to(canonical_anchor)
    except ValueError:
        _raise_unsafe_path(
            reason="outside_trusted_directory",
            path=candidate,
            platform_name=platform_name,
            detail=f"canonical path is outside trusted root {canonical_anchor}",
        )

    if canonical_candidate.parent != canonical_parent:
        _raise_unsafe_path(
            reason="outside_trusted_directory",
            path=candidate,
            platform_name=platform_name,
            detail=f"canonical path is not an immediate child of {canonical_parent}",
        )

    return candidate


def platforms_metadata_path(root: Path) -> Path:
    anchor = normalize_trust_anchor(root)
    parent = anchor / "Data"
    return ensure_trusted_direct_child(anchor, parent, parent / "Platforms.xml")


def ensure_platform_database_path(
    root: Path,
    platform_name: str,
    destination: Path,
) -> Path:
    validate_platform_name(platform_name)
    anchor = normalize_trust_anchor(root)
    parent = anchor / "Data" / "Platforms"
    expected = _absolute_normalized(parent / f"{platform_name}.xml")
    candidate = _absolute_normalized(destination)
    if candidate != expected:
        _raise_unsafe_path(
            reason="outside_trusted_directory",
            path=candidate,
            platform_name=platform_name,
            detail=f"expected platform database path is {expected}",
        )
    return ensure_trusted_direct_child(
        anchor,
        parent,
        candidate,
        platform_name=platform_name,
    )


def platform_database_path(root: Path, platform_name: str) -> Path:
    anchor = normalize_trust_anchor(root)
    return ensure_platform_database_path(
        anchor,
        platform_name,
        anchor / "Data" / "Platforms" / f"{platform_name}.xml",
    )


def resolve_output_dir(root: Path, raw_output: str) -> Path:
    output_dir = Path(raw_output)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    return output_dir.resolve(strict=False)

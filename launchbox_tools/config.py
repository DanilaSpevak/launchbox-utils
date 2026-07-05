from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "launchbox_utils.ini"
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "launchbox_utils.example.ini"
WINDOWS_INVALID_FILENAME_CHARS = '<>:"/\\|?*'
DEDUPLICATE_BY_RESOLVED_PATH = True


@dataclass(frozen=True)
class AppConfig:
    launchbox_root: Path
    output_dir: Path
    config_path: Path


@dataclass(frozen=True)
class RawPathConfig:
    launchbox_root: str = ""
    output_dir: str = ""


class ConfigError(ValueError):
    pass


def load_config_file(config_path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    return parser


def get_config_value(parser: configparser.ConfigParser, section: str, option: str) -> str | None:
    if not parser.has_section(section):
        return None
    value = parser.get(section, option, fallback="").strip()
    return value or None


def normalize_path_text(raw_path: str) -> str:
    path = raw_path.strip()
    if not path:
        return ""

    # Keep the UNC prefix (\\server\share) intact, but collapse accidental
    # doubled separators in regular paths such as literal:D:\\Games\\LaunchBox.
    if path.startswith("\\\\"):
        return "\\\\" + re.sub(r"\\+", r"\\", path.lstrip("\\"))
    return re.sub(r"\\+", r"\\", path)


def load_raw_path_config(config_path: Path) -> RawPathConfig:
    parser = load_config_file(config_path) if config_path.exists() else configparser.ConfigParser()
    return RawPathConfig(
        launchbox_root=normalize_path_text(get_config_value(parser, "paths", "launchbox_root") or ""),
        output_dir=normalize_path_text(get_config_value(parser, "paths", "output_dir") or ""),
    )


def save_raw_path_config(config_path: Path, launchbox_root: str, output_dir: str) -> None:
    parser = load_config_file(config_path) if config_path.exists() else configparser.ConfigParser()
    if not parser.has_section("paths"):
        parser.add_section("paths")

    parser.set("paths", "launchbox_root", normalize_path_text(launchbox_root))
    parser.set("paths", "output_dir", normalize_path_text(output_dir))

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as file:
        parser.write(file)


def resolve_config_path(raw_config_path: str | None = None) -> Path:
    if raw_config_path:
        return Path(raw_config_path).expanduser().resolve(strict=False)
    return DEFAULT_CONFIG_PATH


def load_app_config(
    config_path: Path,
    root_override: str | None = None,
    output_override: str | None = None,
) -> AppConfig:
    parser = load_config_file(config_path) if config_path.exists() else configparser.ConfigParser()

    raw_root = normalize_path_text(root_override or get_config_value(parser, "paths", "launchbox_root") or "")
    if not raw_root:
        raise ConfigError(
            "LaunchBox root is not configured. Pass --root or create launchbox_utils.ini with [paths] launchbox_root."
        )

    launchbox_root = Path(raw_root).expanduser().resolve(strict=False)

    raw_output = normalize_path_text(output_override or get_config_value(parser, "paths", "output_dir") or "")
    if not raw_output:
        raise ConfigError(
            "Output directory is not configured. Pass --output or add [paths] output_dir to launchbox_utils.ini."
        )

    output_dir = Path(raw_output).expanduser()
    if not output_dir.is_absolute():
        output_dir = launchbox_root / output_dir

    return AppConfig(
        launchbox_root=launchbox_root,
        output_dir=output_dir.resolve(strict=False),
        config_path=config_path,
    )

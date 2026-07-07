from __future__ import annotations

import configparser
import locale
import os
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "launchbox_utils.ini"
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "launchbox_utils.example.ini"
WINDOWS_INVALID_FILENAME_CHARS = '<>:"/\\|?*'
SUPPORTED_LANGUAGES = frozenset({"en", "ru"})
INTERFACE_SECTION = "interface"
LANGUAGE_OPTION = "language"
ONLY_WITH_FINDINGS_OPTION = "only_with_findings"


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


def _system_locale_codes() -> list[str]:
    codes: list[str] = []
    for getter in (locale.getlocale, locale.getdefaultlocale):
        try:
            code = getter()[0]
            if code:
                codes.append(code)
        except Exception:
            continue

    lang = os.environ.get("LANG", "").split(".")[0]
    if lang:
        codes.append(lang)
    return codes


def detect_default_language() -> str:
    for code in _system_locale_codes():
        if code.lower().startswith("ru"):
            return "ru"
    return "en"


def load_configured_language(config_path: Path) -> str | None:
    if not config_path.exists():
        return None

    parser = load_config_file(config_path)
    language = get_config_value(parser, INTERFACE_SECTION, LANGUAGE_OPTION)
    if language in SUPPORTED_LANGUAGES:
        return language
    return None


def load_configured_only_with_findings(config_path: Path) -> bool:
    if not config_path.exists():
        return False

    parser = load_config_file(config_path)
    raw_value = get_config_value(parser, INTERFACE_SECTION, ONLY_WITH_FINDINGS_OPTION)
    if raw_value is None:
        return False

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return False


def resolve_initial_language(config_path: Path) -> str:
    configured_language = load_configured_language(config_path)
    if configured_language is not None:
        return configured_language
    return detect_default_language()


def save_interface_language(config_path: Path, language: str) -> None:
    if language not in SUPPORTED_LANGUAGES:
        raise ConfigError(f"Unsupported interface language: {language}")

    parser = load_config_file(config_path) if config_path.exists() else configparser.ConfigParser()
    if not parser.has_section(INTERFACE_SECTION):
        parser.add_section(INTERFACE_SECTION)

    parser.set(INTERFACE_SECTION, LANGUAGE_OPTION, language)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as file:
        parser.write(file)


def save_raw_path_config(
    config_path: Path,
    launchbox_root: str,
    output_dir: str,
    only_with_findings: bool | None = None,
) -> None:
    parser = load_config_file(config_path) if config_path.exists() else configparser.ConfigParser()
    if not parser.has_section("paths"):
        parser.add_section("paths")

    parser.set("paths", "launchbox_root", normalize_path_text(launchbox_root))
    parser.set("paths", "output_dir", normalize_path_text(output_dir))

    if only_with_findings is not None:
        if not parser.has_section(INTERFACE_SECTION):
            parser.add_section(INTERFACE_SECTION)
        parser.set(INTERFACE_SECTION, ONLY_WITH_FINDINGS_OPTION, "true" if only_with_findings else "false")

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

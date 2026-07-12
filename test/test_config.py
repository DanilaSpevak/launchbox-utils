import tempfile
from pathlib import Path
from unittest.mock import patch
from launchbox_tools.config import ConfigError, detect_default_language, load_app_config, load_configured_language, load_configured_only_with_findings, load_raw_path_config, normalize_path_text, resolve_initial_language, save_interface_language, save_raw_path_config

from test.support import LaunchBoxTestCase


class ConfigTests(LaunchBoxTestCase):
    def test_load_app_config_reads_paths_from_ini(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "launchbox_utils.ini"
            config_path.write_text(
                f"""[paths]
launchbox_root = {temp_path / "LaunchBox"}
output_dir = Reports
""",
                encoding="utf-8",
            )

            config = load_app_config(config_path)

            self.assertEqual(config.launchbox_root, (temp_path / "LaunchBox").resolve(strict=False))
            self.assertEqual(config.output_dir, (temp_path / "LaunchBox" / "Reports").resolve(strict=False))

    def test_load_app_config_cli_overrides_ini(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "launchbox_utils.ini"
            config_path.write_text(
                f"""[paths]
launchbox_root = {temp_path / "ConfiguredLaunchBox"}
output_dir = ConfiguredReports
""",
                encoding="utf-8",
            )

            config = load_app_config(
                config_path,
                root_override=str(temp_path / "CliLaunchBox"),
                output_override=str(temp_path / "CliReports"),
            )

            self.assertEqual(config.launchbox_root, (temp_path / "CliLaunchBox").resolve(strict=False))
            self.assertEqual(config.output_dir, (temp_path / "CliReports").resolve(strict=False))

    def test_load_app_config_requires_root_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "missing.ini"

            with self.assertRaises(ConfigError):
                load_app_config(config_path)

    def test_raw_path_config_round_trip_for_gui(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_raw_path_config(config_path, r"D:\Games\LaunchBox", "AuditReports")
            raw_config = load_raw_path_config(config_path)

            self.assertEqual(raw_config.launchbox_root, r"D:\Games\LaunchBox")
            self.assertEqual(raw_config.output_dir, "AuditReports")

    def test_detect_default_language_uses_russian_system_locale(self) -> None:
        with patch("launchbox_tools.config._system_locale_codes", return_value=["Russian_Russia"]):
            self.assertEqual(detect_default_language(), "ru")

        with patch("launchbox_tools.config._system_locale_codes", return_value=["ru_RU"]):
            self.assertEqual(detect_default_language(), "ru")

    def test_detect_default_language_uses_english_for_non_russian_locale(self) -> None:
        with patch("launchbox_tools.config._system_locale_codes", return_value=["en_US"]):
            self.assertEqual(detect_default_language(), "en")

    def test_resolve_initial_language_reads_configured_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"
            config_path.write_text(
                """[interface]
language = en
""",
                encoding="utf-8",
            )

            self.assertEqual(resolve_initial_language(config_path), "en")

    def test_resolve_initial_language_falls_back_to_system_locale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            with patch("launchbox_tools.config.detect_default_language", return_value="ru"):
                self.assertEqual(resolve_initial_language(config_path), "ru")

    def test_resolve_initial_language_ignores_invalid_configured_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"
            config_path.write_text(
                """[interface]
language = fr
""",
                encoding="utf-8",
            )

            with patch("launchbox_tools.config.detect_default_language", return_value="en"):
                self.assertEqual(resolve_initial_language(config_path), "en")

    def test_save_interface_language_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_interface_language(config_path, "ru")

            self.assertEqual(load_configured_language(config_path), "ru")

    def test_save_raw_path_config_preserves_interface_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_interface_language(config_path, "en")
            save_raw_path_config(config_path, r"D:\Games\LaunchBox", "AuditReports")

            self.assertEqual(load_configured_language(config_path), "en")
            raw_config = load_raw_path_config(config_path)
            self.assertEqual(raw_config.launchbox_root, r"D:\Games\LaunchBox")
            self.assertEqual(raw_config.output_dir, "AuditReports")

    def test_load_configured_only_with_findings_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            self.assertFalse(load_configured_only_with_findings(config_path))

    def test_save_raw_path_config_round_trip_for_only_with_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_raw_path_config(
                config_path,
                r"D:\Games\LaunchBox",
                "AuditReports",
                only_with_findings=True,
            )

            self.assertTrue(load_configured_only_with_findings(config_path))

            save_raw_path_config(
                config_path,
                r"D:\Games\LaunchBox",
                "AuditReports",
                only_with_findings=False,
            )

            self.assertFalse(load_configured_only_with_findings(config_path))

    def test_save_interface_language_preserves_only_with_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_raw_path_config(
                config_path,
                r"D:\Games\LaunchBox",
                "AuditReports",
                only_with_findings=True,
            )
            save_interface_language(config_path, "ru")

            self.assertEqual(load_configured_language(config_path), "ru")
            self.assertTrue(load_configured_only_with_findings(config_path))

    def test_save_raw_path_config_without_only_with_findings_preserves_existing_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            save_raw_path_config(
                config_path,
                r"D:\Games\LaunchBox",
                "AuditReports",
                only_with_findings=True,
            )
            save_raw_path_config(config_path, r"D:\LaunchBox", "Reports")

            self.assertTrue(load_configured_only_with_findings(config_path))
            raw_config = load_raw_path_config(config_path)
            self.assertEqual(raw_config.launchbox_root, r"D:\LaunchBox")
            self.assertEqual(raw_config.output_dir, "Reports")

    def test_save_interface_language_rejects_unsupported_language(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "launchbox_utils.ini"

            with self.assertRaises(ConfigError):
                save_interface_language(config_path, "fr")

    def test_normalize_path_text_collapses_duplicate_separators(self) -> None:
        self.assertEqual(normalize_path_text(r"D:\\Games\\LaunchBox"), r"D:\Games\LaunchBox")
        self.assertEqual(normalize_path_text(r"\\server\\share\\LaunchBox"), r"\\server\share\LaunchBox")

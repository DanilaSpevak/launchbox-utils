from pathlib import Path
from unittest.mock import patch
from launchbox_tools.runtime_checks import MutationBlockedError, ensure_safe_to_mutate, is_file_locked
from launchbox_tools.xml_repository import load_platforms

from test.support import LaunchBoxTestCase


class RuntimeChecksTests(LaunchBoxTestCase):
    def test_ensure_safe_to_mutate_blocks_when_launchbox_running(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=True):
                with self.assertRaises(MutationBlockedError) as context:
                    ensure_safe_to_mutate([platform.database_xml])

            self.assertEqual(context.exception.reason, "launchbox_running")

    def test_ensure_safe_to_mutate_blocks_when_file_locked(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.runtime_checks.is_file_locked", return_value=True):
                    with self.assertRaises(MutationBlockedError) as context:
                        ensure_safe_to_mutate([platform.database_xml])

            self.assertEqual(context.exception.reason, "files_locked")
            self.assertEqual(context.exception.locked_files, [platform.database_xml])

    def test_is_file_locked_returns_false_for_unlocked_file(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platform = load_platforms(root)[0]

            self.assertFalse(is_file_locked(platform.database_xml))

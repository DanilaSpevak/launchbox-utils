"""Shared test fixtures for temporary LaunchBox trees."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from launchbox_tools.operation_lifecycle import OperationControl


def create_directory_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or result.stdout.strip() or "mklink /J failed")


def remove_directory_junction(path: Path) -> None:
    if path.exists():
        os.rmdir(path)


class CancelAfterCheckpoints(OperationControl):
    def __init__(self, checkpoint_count: int) -> None:
        super().__init__()
        self.checkpoint_count = checkpoint_count
        self.checkpoint_calls = 0

    def checkpoint(self) -> None:
        self.checkpoint_calls += 1
        if self.checkpoint_calls == self.checkpoint_count:
            self.request_cancel()
        super().checkpoint()


class LaunchBoxTestCase(unittest.TestCase):
    def make_root(self) -> tempfile.TemporaryDirectory:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        (root / "Data" / "Platforms").mkdir(parents=True)
        return temp_dir

    def write_platforms_xml(self, root: Path, folder: str = "Games/NES") -> None:
        (root / "Data" / "Platforms.xml").write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<ArrayOfPlatform>
  <Platform>
    <Name>Nintendo Entertainment System</Name>
    <Folder>{folder}</Folder>
  </Platform>
</ArrayOfPlatform>
""",
            encoding="utf-8",
        )

    def write_games_xml(self, root: Path, games: list[tuple[str, str]]) -> None:
        game_xml = "\n".join(
            f"""  <Game>
    <Title>{title}</Title>
    <ApplicationPath>{application_path}</ApplicationPath>
  </Game>"""
            for title, application_path in games
        )
        (root / "Data" / "Platforms" / "Nintendo Entertainment System.xml").write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
{game_xml}
</LaunchBox>
""",
            encoding="utf-8",
        )

    def write_games_xml_raw(self, root: Path, body: str) -> None:
        (root / "Data" / "Platforms" / "Nintendo Entertainment System.xml").write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
{body}
</LaunchBox>
""",
            encoding="utf-8",
        )

    def write_two_platforms_xml(self, root: Path) -> None:
        (root / "Data" / "Platforms.xml").write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<ArrayOfPlatform>
  <Platform>
    <Name>Nintendo Entertainment System</Name>
    <Folder>Games/NES</Folder>
  </Platform>
  <Platform>
    <Name>Sega Genesis</Name>
    <Folder>Games/Genesis</Folder>
  </Platform>
</ArrayOfPlatform>
""",
            encoding="utf-8",
        )

    def write_platform_games_xml_raw(self, root: Path, platform_name: str, body: str) -> None:
        (root / "Data" / "Platforms" / f"{platform_name}.xml").write_text(
            f"""<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
{body}
</LaunchBox>
""",
            encoding="utf-8",
        )

    def duplicate_additional_app_xml(self, game_id: str, keep_title: str, remove_title: str, path: str) -> str:
        return f"""  <AdditionalApplication>
    <GameID>{game_id}</GameID>
    <Name>{keep_title}</Name>
    <ApplicationPath>{path}</ApplicationPath>
  </AdditionalApplication>
  <AdditionalApplication>
    <GameID>{game_id}</GameID>
    <Name>{remove_title}</Name>
    <ApplicationPath>{path}</ApplicationPath>
  </AdditionalApplication>"""

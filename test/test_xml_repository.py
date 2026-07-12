from pathlib import Path
from launchbox_tools.xml_repository import load_platforms

from test.support import LaunchBoxTestCase


class XmlRepositoryTests(LaunchBoxTestCase):
    def test_load_platforms_resolves_relative_folder(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")

            platforms = load_platforms(root)

            self.assertEqual(len(platforms), 1)
            self.assertEqual(platforms[0].name, "Nintendo Entertainment System")
            self.assertEqual(platforms[0].folder, (root / "Games" / "NES").resolve(strict=False))
            self.assertEqual(
                platforms[0].database_xml,
                root / "Data" / "Platforms" / "Nintendo Entertainment System.xml",
            )

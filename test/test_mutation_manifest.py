from pathlib import Path
from unittest.mock import patch
from launchbox_tools.operations.path_replacement import run_path_replacement
from launchbox_tools.models import MutationFileResult, MutationOutcome, MutationRunResult, MutationState
from launchbox_tools.mutation_manifest import write_mutation_manifest

from test.support import LaunchBoxTestCase


class MutationManifestTests(LaunchBoxTestCase):
    def test_manifest_write_failure_does_not_change_committed_mutation_state(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.mutation_manifest.Path.write_text",
                    side_effect=OSError("manifest denied"),
                ):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "NES",
                        root / "Games" / "SNES",
                        apply_changes=True,
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
            self.assertTrue(all(file.state == MutationState.COMMITTED for file in run_result.files))
            self.assertEqual(run_result.manifest_error, "manifest denied")
            self.assertIsNone(run_result.manifest_path)
            self.assertFalse(list(root.rglob("manifest.json.tmp")))
            self.assertFalse(list(root.rglob("manifest.json")))

    def test_manifest_write_failure_without_message_uses_exception_type(self) -> None:
        run_result = MutationRunResult([], MutationOutcome.SUCCESS)

        with patch("launchbox_tools.mutation_manifest.Path.mkdir"):
            with patch(
                "launchbox_tools.mutation_manifest.Path.write_text",
                side_effect=OSError(),
            ):
                with patch("launchbox_tools.mutation_manifest.Path.unlink"):
                    write_mutation_manifest(run_result, Path("C:/Backups/run"), "test_operation", [])

        self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
        self.assertEqual(run_result.manifest_error, "OSError")
        self.assertIsNone(run_result.manifest_path)

    def test_manifest_replace_failure_preserves_existing_manifest_and_cleans_temp(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "Data" / "Backups" / "run"
            backup_root.mkdir(parents=True)
            manifest_path = backup_root / "manifest.json"
            manifest_path.write_text("previous manifest\n", encoding="utf-8")
            run_result = MutationRunResult(
                [],
                MutationOutcome.SUCCESS,
                files=[MutationFileResult(root / "database.xml", MutationState.COMMITTED)],
            )

            with patch(
                "launchbox_tools.mutation_manifest.os.replace",
                side_effect=OSError("manifest replace denied"),
            ):
                write_mutation_manifest(run_result, backup_root, "test_operation", [])

            self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(run_result.files[0].state, MutationState.COMMITTED)
            self.assertEqual(run_result.manifest_error, "manifest replace denied")
            self.assertIsNone(run_result.manifest_path)
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), "previous manifest\n")
            self.assertFalse((backup_root / "manifest.json.tmp").exists())

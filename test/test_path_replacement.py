import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID
from unittest.mock import patch
from launchbox_tools.operations.path_replacement import (
    _PlannedFileIndex,
    build_replacement_value,
    run_path_replacement,
)
from launchbox_tools.models import (
    MutationFileResult,
    MutationOutcome,
    MutationState,
    PathReplacement,
    PlatformInfo,
)
from launchbox_tools.operation_lifecycle import OperationCancelled, OperationControl
from launchbox_tools.xml_repository import child_text, local_name, parse_xml

from test.support import CancelAfterCheckpoints, LaunchBoxTestCase


class PathReplacementTests(LaunchBoxTestCase):
    def test_planned_file_index_propagates_late_file_error(self) -> None:
        platform = PlatformInfo("Platform", Path("Games"), Path("Platform.xml"))
        first = PathReplacement(
            platform=platform,
            xml_path=Path("Platform.xml"),
            entry_type="Game",
            title="First",
            old_value="old",
            new_value="new",
        )
        second = PathReplacement(
            platform=platform,
            xml_path=Path("Platform.xml"),
            entry_type="Game",
            title="Second",
            old_value="old",
            new_value="",
            state=MutationState.FAILED,
            error="invalid replacement",
        )
        other = PathReplacement(
            platform=platform,
            xml_path=Path("Other.xml"),
            entry_type="Game",
            title="Other",
            old_value="old",
            new_value="new",
        )

        index = _PlannedFileIndex()
        index.record_replacement(first)
        index.record_replacement(second)
        index.record_replacement(other)

        self.assertEqual([item.path.name for item in index.files], ["Platform.xml", "Other.xml"])
        self.assertEqual(index.files[0].state, MutationState.FAILED)
        self.assertEqual(index.files[0].error, "invalid replacement")
        self.assertEqual(first.state, MutationState.FAILED)
        self.assertEqual(second.state, MutationState.FAILED)
        self.assertEqual(other.state, MutationState.PLANNED)

    def test_planned_file_index_mass_errors_are_linear(self) -> None:
        class ReplacementProbe:
            state_bindings = 0

            def __init__(self, index: int) -> None:
                self.xml_path = Path("Platform.xml")
                self.error = f"invalid replacement {index}"

            def _bind_state_source(self, _file_result: MutationFileResult) -> None:
                ReplacementProbe.state_bindings += 1

        index = _PlannedFileIndex()
        replacement_count = 1_000

        for item_index in range(replacement_count):
            index.record_replacement(ReplacementProbe(item_index))  # type: ignore[arg-type]

        self.assertEqual(ReplacementProbe.state_bindings, replacement_count)
        self.assertEqual(index.files[0].state, MutationState.FAILED)
        self.assertEqual(index.files[0].error, "invalid replacement 999")

    def test_path_replacement_state_binding_preserves_dataclass_behavior(self) -> None:
        platform = PlatformInfo("Platform", Path("Games"), Path("Platform.xml"))
        replacement = PathReplacement(
            platform=platform,
            xml_path=Path("Platform.xml"),
            entry_type="Game",
            title="Game",
            old_value="old",
            new_value="new",
        )
        file_result = MutationFileResult(Path("Platform.xml"))

        replacement._bind_state_source(file_result)
        file_result.state = MutationState.FAILED

        self.assertEqual(replacement.state, MutationState.FAILED)
        self.assertEqual(asdict(replacement)["state"], MutationState.FAILED)
        self.assertIn("state=<MutationState.FAILED", repr(replacement))
        cloned = replace(replacement)
        self.assertEqual(cloned.state, MutationState.FAILED)
        self.assertNotIn("_state_source", cloned.__dict__)
        self.assertEqual(replacement, cloned)

        replacement.state = MutationState.PLANNED

        self.assertEqual(replacement.state, MutationState.PLANNED)
        self.assertNotIn("_state_source", replacement.__dict__)

    def test_path_replacement_cancelled_after_scan_uses_incremental_plan(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            xml_paths = [
                root / "Data" / "Platforms.xml",
                root / "Data" / "Platforms" / "Nintendo Entertainment System.xml",
            ]
            original_contents = {path: path.read_bytes() for path in xml_paths}

            from launchbox_tools.operations import path_replacement as path_module

            real_collect = path_module._collect_application_path_replacements

            def cancel_after_collect(*args, **kwargs):
                collected = real_collect(*args, **kwargs)
                control = kwargs.get("control") or args[-1]
                planned_files = args[-2]
                platform = args[0]
                planned_files.record_error(
                    platform.database_xml,
                    "late scan error",
                )
                if control is not None:
                    control.request_cancel()
                return collected

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.operations.path_replacement._collect_application_path_replacements",
                    side_effect=cancel_after_collect,
                ):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "NES",
                        root / "Games" / "SNES",
                        apply_changes=True,
                        control=OperationControl(),
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.CANCELLED)
            self.assertEqual({path: path.read_bytes() for path in xml_paths}, original_contents)
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "cancelled")
            files_by_path = {item["path"]: item for item in manifest["files"]}
            self.assertIn("failed", {item["state"] for item in manifest["files"]})
            for change in manifest["changes"]:
                self.assertEqual(change["state"], files_by_path[change["xml_path"]]["state"])
            for result in run_result.results:
                for replacement in result.replacements:
                    self.assertEqual(
                        replacement.state.value,
                        files_by_path[str(replacement.xml_path)]["state"],
                    )
            self.assertFalse(list((root / "Data").rglob("*.tmp")))

    def test_cancelled_dry_run_does_not_rebind_states_after_cancel(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(
                root,
                [
                    ("First", "Games/NES/first.zip"),
                    ("Second", "Games/NES/second.zip"),
                ],
            )
            control = OperationControl()
            bindings_after_cancel = 0
            iterations_after_cancel = 0

            class ReplacementList(list[PathReplacement]):
                def __iter__(self):
                    nonlocal iterations_after_cancel
                    if control.snapshot().cancel_requested:
                        iterations_after_cancel += 1
                    return super().__iter__()

            from launchbox_tools.operations import path_replacement as path_module

            real_collect = path_module._collect_application_path_replacements
            real_bind = PathReplacement._bind_state_source

            def track_state_binding(
                replacement: PathReplacement,
                file_result: MutationFileResult,
            ) -> None:
                nonlocal bindings_after_cancel
                if control.snapshot().cancel_requested:
                    bindings_after_cancel += 1
                real_bind(replacement, file_result)

            def cancel_after_collect(*args, **kwargs):
                collected = real_collect(*args, **kwargs)
                planned_files = args[-2]
                platform = args[0]
                result = args[1]
                result.replacements = ReplacementList(result.replacements)
                planned_files.record_error(platform.database_xml, "late scan error")
                control.request_cancel()
                return collected

            with (
                patch.object(
                    PathReplacement,
                    "_bind_state_source",
                    new=track_state_binding,
                ),
                patch.object(
                    path_module,
                    "_collect_application_path_replacements",
                    side_effect=cancel_after_collect,
                ),
            ):
                run_result = run_path_replacement(
                    root,
                    root / "Games" / "NES",
                    root / "Games" / "SNES",
                    apply_changes=False,
                    control=control,
                )

            self.assertEqual(run_result.outcome, MutationOutcome.CANCELLED)
            self.assertIsNone(run_result.manifest_path)
            self.assertEqual(bindings_after_cancel, 0)
            self.assertEqual(iterations_after_cancel, 0)
            files_by_path = {file_result.path: file_result for file_result in run_result.files}
            for result in run_result.results:
                for replacement in result.replacements:
                    self.assertEqual(
                        replacement.state,
                        files_by_path[replacement.xml_path.resolve(strict=False)].state,
                    )

    def test_path_replacement_late_cancel_wins_before_failed_manifest(self) -> None:
        class CancelAtFinalizeControl(OperationControl):
            def begin_finalize(self) -> None:
                self.request_cancel()
                super().begin_finalize()

        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            xml_paths = [
                root / "Data" / "Platforms.xml",
                root / "Data" / "Platforms" / "Nintendo Entertainment System.xml",
            ]
            original_contents = {path: path.read_bytes() for path in xml_paths}

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.operations.path_replacement._collect_application_path_replacements",
                    side_effect=OSError("scan failed"),
                ):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "NES",
                        root / "Games" / "SNES",
                        apply_changes=True,
                        control=CancelAtFinalizeControl(),
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.CANCELLED)
            self.assertEqual({path: path.read_bytes() for path in xml_paths}, original_contents)
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "cancelled")
            files_by_path = {item["path"]: item for item in manifest["files"]}
            for change in manifest["changes"]:
                self.assertEqual(change["state"], files_by_path[change["xml_path"]]["state"])
            self.assertFalse(list((root / "Data").rglob("*.tmp")))

    def test_path_replacement_cancelled_before_scan_writes_empty_consistent_manifest(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            control = OperationControl()
            control.request_cancel()

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                run_result = run_path_replacement(
                    root,
                    root / "Games" / "NES",
                    root / "Games" / "SNES",
                    apply_changes=True,
                    control=control,
                )

            self.assertEqual(run_result.outcome, MutationOutcome.CANCELLED)
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["changes"], [])
            self.assertEqual(manifest["files"], [])

    def test_path_replacement_cancelled_during_scan_keeps_manifest_files_consistent(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.operations.path_replacement._collect_application_path_replacements",
                    side_effect=OperationCancelled("Operation cancelled"),
                ):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "NES",
                        root / "Games" / "SNES",
                        apply_changes=True,
                        control=OperationControl(),
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.CANCELLED)
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["changes"]), 1)
            self.assertEqual(len(manifest["files"]), 1)
            self.assertEqual(manifest["changes"][0]["xml_path"], manifest["files"][0]["path"])
            self.assertEqual(manifest["changes"][0]["state"], "planned")
            self.assertEqual(manifest["files"][0]["state"], "planned")

    def test_path_replacement_scan_error_does_not_reserve_backup_root(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch(
                    "launchbox_tools.operations.path_replacement._collect_platform_folder_replacements",
                    side_effect=RuntimeError("scan failed"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "scan failed"):
                        run_path_replacement(
                            root,
                            root / "Games" / "NES",
                            root / "Games" / "SNES",
                            apply_changes=True,
                            control=OperationControl(),
                        )

            backup_parent = root / "Data" / "Backups"
            self.assertFalse(backup_parent.exists())

    def test_path_replacement_cancelled_after_stage_writes_manifest_without_commit(self) -> None:
        class CancelAtCommitControl(OperationControl):
            def begin_commit(self) -> None:
                self.request_cancel()
                super().begin_commit()

        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            xml_paths = [
                root / "Data" / "Platforms.xml",
                root / "Data" / "Platforms" / "Nintendo Entertainment System.xml",
            ]
            original_contents = {path: path.read_bytes() for path in xml_paths}

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                run_result = run_path_replacement(
                    root,
                    root / "Games" / "NES",
                    root / "Games" / "SNES",
                    apply_changes=True,
                    control=CancelAtCommitControl(),
                )

            self.assertEqual(run_result.outcome, MutationOutcome.CANCELLED)
            self.assertTrue(run_result.manifest_path.is_file())
            self.assertTrue(all(item.state == MutationState.PREPARED for item in run_result.files))
            self.assertEqual({path: path.read_bytes() for path in xml_paths}, original_contents)
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "cancelled")
            self.assertEqual({item["state"] for item in manifest["files"]}, {"prepared"})
            self.assertTrue(all(Path(item["backup_path"]).is_file() for item in manifest["files"]))
            self.assertFalse(list((root / "Data").rglob("*.tmp")))

    def test_build_replacement_value_preserves_absolute_paths(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            old_path = root / "OldRoms"
            new_path = root / "NewRoms"

            new_value, error = build_replacement_value(root, old_path, new_path, str(old_path / "NES" / "game.zip"))

            self.assertIsNone(error)
            self.assertEqual(new_value, str(new_path / "NES" / "game.zip"))

    def test_build_replacement_value_preserves_relative_paths_and_forward_slashes(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)

            new_value, error = build_replacement_value(
                root,
                root / "Games" / "NES",
                root / "Games" / "SNES",
                "Games/NES/game.zip",
            )

            self.assertIsNone(error)
            self.assertEqual(new_value, "Games/SNES/game.zip")

    def test_build_replacement_value_does_not_match_similar_prefix(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)

            new_value, error = build_replacement_value(
                root,
                root / "Games" / "ROM",
                root / "Games" / "NewROM",
                "Games/ROMs/game.zip",
            )

            self.assertIsNone(new_value)
            self.assertIsNone(error)

    def test_path_replacement_dry_run_reports_without_changing_xml(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            xml_path = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            before = xml_path.read_text(encoding="utf-8")

            results = run_path_replacement(root, root / "Games" / "NES", root / "Games" / "SNES")

            self.assertEqual(results.outcome, MutationOutcome.DRY_RUN)
            self.assertEqual(sum(len(result.replacements) for result in results.results), 2)
            self.assertEqual(xml_path.read_text(encoding="utf-8"), before)
            self.assertTrue(all(file.state == MutationState.PLANNED for file in results.files))
            self.assertTrue(
                all(
                    replacement.state == MutationState.PLANNED
                    for result in results.results
                    for replacement in result.replacements
                )
            )

    def test_path_replacement_apply_updates_entries_platform_folder_and_creates_backups(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml_raw(
                root,
                """  <Game>
    <Title>Main Game</Title>
    <ApplicationPath>Games/NES/main.zip</ApplicationPath>
  </Game>
  <AdditionalApplication>
    <GameID>game-1</GameID>
    <Name>Extra</Name>
    <ApplicationPath>Games/NES/extra.zip</ApplicationPath>
  </AdditionalApplication>""",
            )
            original_hashes = {
                str(path.resolve(strict=False)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    root / "Data" / "Platforms.xml",
                    root / "Data" / "Platforms" / "Nintendo Entertainment System.xml",
                )
            }

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                results = run_path_replacement(root, root / "Games" / "NES", root / "Games" / "SNES", apply_changes=True)

            platforms_xml = parse_xml(root / "Data" / "Platforms.xml")
            self.assertEqual(child_text(next(platforms_xml.iter("Platform")), "Folder"), "Games/SNES")
            platform_xml = parse_xml(root / "Data" / "Platforms" / "Nintendo Entertainment System.xml")
            paths = [child_text(element, "ApplicationPath") for element in platform_xml if local_name(element.tag) in {"Game", "AdditionalApplication"}]
            self.assertEqual(paths, ["Games/SNES/main.zip", "Games/SNES/extra.zip"])
            self.assertEqual(results.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(sum(len(result.replacements) for result in results.results), 3)
            self.assertTrue(any(result.backup_paths for result in results.results))
            self.assertTrue(all(file.state == MutationState.COMMITTED for file in results.files))
            self.assertTrue(results.manifest_path.is_file())
            manifest = json.loads(results.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["run_id"], results.run_id)
            UUID(results.run_id)
            self.assertEqual(manifest["outcome"], "success")
            self.assertEqual({item["state"] for item in manifest["files"]}, {"committed"})
            self.assertEqual({item["state"] for item in manifest["changes"]}, {"committed"})
            for item in manifest["files"]:
                self.assertEqual(item["source_sha256"], original_hashes[item["path"]])
                self.assertEqual(
                    hashlib.sha256(Path(item["backup_path"]).read_bytes()).hexdigest(),
                    item["source_sha256"],
                )

    def test_path_replacement_apply_without_changes_still_writes_manifest(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                run_result = run_path_replacement(
                    root,
                    root / "Games" / "Missing",
                    root / "Games" / "New",
                    apply_changes=True,
                )

            self.assertEqual(run_result.outcome, MutationOutcome.SUCCESS)
            self.assertEqual(run_result.files, [])
            self.assertTrue(run_result.manifest_path.is_file())
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"], [])
            self.assertEqual(manifest["changes"], [])

    def test_path_replacement_same_second_apply_uses_distinct_backup_roots(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            fixed_datetime = patch("launchbox_tools.operations.path_replacement.datetime")

            with fixed_datetime as datetime_mock:
                datetime_mock.now.return_value.strftime.return_value = "20260712-120000"
                with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                    first = run_path_replacement(
                        root,
                        root / "Games" / "Missing",
                        root / "Games" / "New",
                        apply_changes=True,
                    )
                    first_manifest = first.manifest_path.read_text(encoding="utf-8")
                    second = run_path_replacement(
                        root,
                        root / "Games" / "Missing",
                        root / "Games" / "New",
                        apply_changes=True,
                    )

            self.assertNotEqual(first.run_id, second.run_id)
            UUID(first.run_id)
            UUID(second.run_id)
            self.assertEqual(
                first.manifest_path.parent.name,
                f"PathReplacement-20260712-120000-{first.run_id}",
            )
            self.assertEqual(
                second.manifest_path.parent.name,
                f"PathReplacement-20260712-120000-{second.run_id}",
            )
            self.assertEqual(first.manifest_path.read_text(encoding="utf-8"), first_manifest)
            self.assertTrue(second.manifest_path.is_file())

    def test_path_replacement_rolls_back_all_xml_after_late_commit_failure(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_platforms_xml(root, "Games/NES")
            self.write_games_xml(root, [("Game", "Games/NES/game.zip")])
            platforms_xml = root / "Data" / "Platforms.xml"
            games_xml = root / "Data" / "Platforms" / "Nintendo Entertainment System.xml"
            originals = {platforms_xml: platforms_xml.read_bytes(), games_xml: games_xml.read_bytes()}
            commit_calls = 0

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated late commit failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=fail_second_commit):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "NES",
                        root / "Games" / "SNES",
                        apply_changes=True,
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.ROLLED_BACK)
            self.assertEqual(platforms_xml.read_bytes(), originals[platforms_xml])
            self.assertEqual(games_xml.read_bytes(), originals[games_xml])
            self.assertEqual(
                {file.state for file in run_result.files},
                {MutationState.ROLLED_BACK, MutationState.FAILED},
            )
            self.assertFalse(
                any(
                    replacement.state == MutationState.COMMITTED
                    for result in run_result.results
                    for replacement in result.replacements
                )
            )
            self.assertTrue(any(result.backup_paths for result in run_result.results))
            manifest = json.loads(run_result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "rolled_back")
            self.assertEqual(
                {item["state"] for item in manifest["files"]},
                {"rolled_back", "failed"},
            )

    def test_path_replacement_rolls_back_platforms_xml_name_collision(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            platforms_xml = root / "Data" / "Platforms.xml"
            platform_xml = root / "Data" / "Platforms" / "Platforms.xml"
            platforms_xml.write_text(
                "<ArrayOfPlatform><Platform><Name>Platforms</Name><Folder>Games/Old</Folder>"
                "</Platform></ArrayOfPlatform>",
                encoding="utf-8",
            )
            platform_xml.write_text(
                "<LaunchBox><Game><Title>Game</Title><ApplicationPath>Games/Old/game.zip</ApplicationPath>"
                "</Game></LaunchBox>",
                encoding="utf-8",
            )
            originals = {
                platforms_xml: platforms_xml.read_bytes(),
                platform_xml: platform_xml.read_bytes(),
            }
            commit_calls = 0

            def fail_second_commit(stage_path: Path, destination: Path) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("simulated second commit failure")
                stage_path.replace(destination)

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                with patch("launchbox_tools.safe_write._commit_staged_file", side_effect=fail_second_commit):
                    run_result = run_path_replacement(
                        root,
                        root / "Games" / "Old",
                        root / "Games" / "New",
                        apply_changes=True,
                    )

            self.assertEqual(run_result.outcome, MutationOutcome.ROLLED_BACK)
            self.assertEqual(platforms_xml.read_bytes(), originals[platforms_xml])
            self.assertEqual(platform_xml.read_bytes(), originals[platform_xml])
            self.assertEqual(
                {file.state for file in run_result.files},
                {MutationState.ROLLED_BACK, MutationState.FAILED},
            )
            self.assertFalse(
                any(
                    replacement.state == MutationState.COMMITTED
                    for result in run_result.results
                    for replacement in result.replacements
                )
            )
            backup_paths = [path for result in run_result.results for path in result.backup_paths]
            self.assertEqual(len(backup_paths), 2)
            self.assertEqual(len(set(backup_paths)), 2)
            self.assertEqual({path.read_bytes() for path in backup_paths}, set(originals.values()))
            self.assertFalse(list(root.rglob("*.stage.tmp")))
            self.assertFalse(list(root.rglob("*.rollback.tmp")))

    def test_path_replacement_can_filter_platform(self) -> None:
        with self.make_root() as temp_dir:
            root = Path(temp_dir)
            self.write_two_platforms_xml(root)
            self.write_platform_games_xml_raw(
                root,
                "Nintendo Entertainment System",
                """  <Game>
    <Title>NES Game</Title>
    <ApplicationPath>Games/NES/game.zip</ApplicationPath>
  </Game>""",
            )
            self.write_platform_games_xml_raw(
                root,
                "Sega Genesis",
                """  <Game>
    <Title>Genesis Game</Title>
    <ApplicationPath>Games/Genesis/game.zip</ApplicationPath>
  </Game>""",
            )

            with patch("launchbox_tools.runtime_checks.is_launchbox_process_running", return_value=False):
                results = run_path_replacement(
                    root,
                    root / "Games" / "NES",
                    root / "Games" / "NewNES",
                    platform_filter="Nintendo Entertainment System",
                    apply_changes=True,
                )

            self.assertEqual(len(results.results), 1)
            genesis_xml = parse_xml(root / "Data" / "Platforms" / "Sega Genesis.xml")
            self.assertEqual(child_text(next(genesis_xml.iter("Game")), "ApplicationPath"), "Games/Genesis/game.zip")

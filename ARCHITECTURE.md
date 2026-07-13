# Architecture

This project is a small Python utility suite for auditing and maintaining LaunchBox data.

> **Disclaimer:** This project is not affiliated with, endorsed by, or sponsored by LaunchBox.

## Goals

- Keep LaunchBox database operations understandable and testable.
- Keep read-only analysis separate from operations that modify XML.
- Reuse the same business logic from CLI and GUI.
- Avoid external runtime dependencies where practical.

## Architecture Decisions

Proposed and accepted architectural decisions are recorded in
[`docs/decisions`](docs/decisions/README.md). A proposed decision is not an
implementation commitment and should be added to the roadmap only after it is
accepted.

## High-Level Structure

```text
launchbox_utils.py              # CLI entrypoint
launchbox_tools/
  cli.py                        # command-line interface
  config.py                     # INI configuration loading and saving
  models.py                     # shared dataclasses
  paths.py                      # path normalization and report folder naming
  xml_repository.py             # LaunchBox XML reading helpers
  runtime_checks.py             # LaunchBox process and XML file lock checks
  mutation_lock.py              # per-installation interprocess apply lock
  operation_lifecycle.py        # operation phases and pre-commit cancellation
  safe_write.py                 # backup and safe XML write helpers
  operations/
    audit.py                    # read-only ROM audit
    dedupe_additional_apps.py   # AdditionalApplication dedupe operation
    path_replacement.py         # bulk ROM path replacement operation
  reports/
    audit_reports.py            # audit report writers
    dedupe_reports.py           # dedupe report writers
    path_replacement_reports.py # path replacement report writers
  gui/
    app.py                      # Tkinter GUI
    translations.py             # RU/EN UI strings
```

## Module Responsibilities

- `cli.py` parses command-line arguments and calls operations. It should not contain business logic.
- `gui/app.py` collects UI input, starts background workers, and displays logs. It should not duplicate operation logic.
- `operations/*` modules decide what should happen.
- `reports/*` modules decide how results are written to files.
- `xml_repository.py` owns LaunchBox XML reading helpers.
- `runtime_checks.py` owns pre-mutation safety checks for LaunchBox process state and XML file locks.
- `mutation_lock.py` owns the installation-wide interprocess apply lock.
- `operation_lifecycle.py` owns thread-safe operation phases, cooperative cancellation, and the irreversible commit boundary.
- `safe_write.py` owns backup and safe XML replacement behavior.
- `mutation_manifest.py` writes the final apply manifest independently from user reports.
- `config.py` owns `launchbox_utils.ini` parsing and saving.

## Configuration

The real local config file is:

```text
launchbox_utils.ini
```

It is intentionally ignored by git. The committed example is:

```text
launchbox_utils.example.ini
```

Current format:

```ini
[paths]
launchbox_root = D:\Games\LaunchBox
output_dir = AuditReports
```

CLI overrides have priority over INI values:

- `--root` overrides `launchbox_root`.
- `--output` overrides `output_dir`.
- `--config` selects another INI file.

Relative `output_dir` values are resolved from `launchbox_root`.

## LaunchBox Data Model Notes

- Platform metadata is read from `Data/Platforms.xml`.
- Platform ROM folders are read from each platform's `Folder` tag.
- Per-platform game databases are read from `Data/Platforms/<PlatformName>.xml`.
- Main game paths are stored in `<Game><ApplicationPath>`.
- Additional application paths are stored in `<AdditionalApplication><ApplicationPath>`.
- Additional applications are associated with games by `GameID`.

Path replacement edits the three path-bearing fields above: platform `Folder`, main game `ApplicationPath`, and additional application `ApplicationPath`. Absolute database values stay absolute; relative values are rewritten relative to the LaunchBox root.

For Additional Apps dedupe, records are first grouped where both values match:

- `GameID`
- normalized `ApplicationPath`

Within each group, the complete `<AdditionalApplication>` XML content is canonicalized. Field order, formatting-only whitespace between XML elements, known boolean casing, `GameID` casing, and normalized path spelling do not create distinct variants. Whitespace inside field and attribute values remains significant. All other content, including names, command lines, emulator settings, attributes, nested elements, repeated elements, and unknown future fields, remains significant.

Only repeated canonical variants are automatic duplicates. If a group contains multiple canonical variants, one representative of each is preserved and the group is reported as ambiguous for manual review. For `A, A, B`, only the second `A` is removable.

## Safety Rules For XML-Modifying Operations

Any operation that modifies LaunchBox XML must follow these rules:

- Dry-run must be available and should be the default user-facing mode.
- Apply must be explicit.
- Before apply, call `ensure_safe_to_mutate()` from `runtime_checks.py`.
- A backup must be created before writing XML.
- XML should be written to a temporary file first.
- The temporary XML must be parsed successfully before replacing the original file.
- Only the intended XML elements should be changed.

Multi-file mutations use the shared transaction executor in `safe_write.py`: plan and validate all serialized XML, back up every destination, stage and parse every temporary file, then commit with atomic replacement. Every apply run receives a UUID and exclusively creates a backup root named `<Operation>-<timestamp>-<uuid>`. Each destination in a transaction receives a numbered backup subdirectory (`0001`, `0002`, and so on), and backup files are created exclusively, so an existing backup is never replaced. Stage, rollback, and manifest temporary files include the run UUID and are removed in `finally`. If a later commit fails, already committed files are restored from backup in reverse order.

Only one mutation may run for a LaunchBox installation at a time. Apply operations acquire a non-blocking OS lock on `Data/.launchbox-utils-mutation.lock` before reading XML and hold it through safety checks, backup, commit or rollback, manifest writing, and cleanup. The lock metadata records the operation, run UUID, PID, and UTC start time. A competing apply fails immediately with `MutationBlockedError(reason="mutation_in_progress")`; dry-run operations do not acquire this lock. The lock file is retained, while the OS releases the lock automatically when the owning process exits.

`replace-paths` treats all changed XML files as one transaction. Additional Apps dedupe treats each platform XML as an independent transaction, so one failed platform does not undo successful changes to another platform. Mutation runs expose `dry_run`, `success`, `partial`, `failed`, `rolled_back`, or `cancelled`.

Long-running operations may receive an optional `OperationControl`. It publishes `scan`, `stage`, `commit`, `rollback`, `finalize`, and `finished` phases and provides cooperative cancellation checkpoints. XML parsing, entry analysis, and serialization check at least every 256 events or fragments; hashing, backup copies, and staged writes check every 1 MiB. Cancellation and the start of commit use the same lock: a cancellation request either wins before the first XML replacement or is rejected permanently once commit starts. A cancelled staged transaction keeps verified backups, removes temporary files, leaves XML unchanged, preserves `planned` / `prepared` file states, and records `outcome: cancelled` in the apply manifest.

`MutationState` is the only source of truth for individual files and changes: `planned` before preparation, `prepared` after backup/stage validation, `committed` after atomic replacement, `failed` for a failed file step, and `rolled_back` after successful restoration. A schema-version 2 `manifest.json` in the apply backup root records the run UUID, outcome, file states, original paths and SHA-256 hashes, backup paths, diagnostics, and operation-specific changes. Each hash is verified against the exclusive backup before staging begins. Manifest writer failures are reported separately and never rewrite the known XML mutation state.

Use `safe_write.py` for backup and safe replacement rather than writing XML directly.

### Mutation Safety Checks

`runtime_checks.py` enforces the rule that LaunchBox must not be running and database XML must not be locked before apply.

Checks run after the apply operation has acquired the installation-wide mutation lock and then continue in two layers:

1. **Operation orchestrator** — `run_additional_apps_dedupe(..., apply_changes=True)` calls `ensure_safe_to_mutate()` once before processing platforms.
2. **Write layer** — the transaction executor calls `ensure_safe_to_mutate()` before backup and again immediately before commit. This protects future callers that bypass the orchestrator.

`ensure_safe_to_mutate(xml_paths)` aborts with `MutationBlockedError` when either condition is true:

- a LaunchBox process is running (`LaunchBox.exe` or `LaunchBox Big Box.exe`, detected via `tasklist` on Windows);
- any target XML file cannot be opened exclusively because another process holds a lock.

Read-only operations (`audit`, dedupe dry-run) do not call these checks.

Entry points surface the error consistently:

- **CLI** — catches `MutationBlockedError`, prints to stderr, exits with code 1.
- **GUI** — runs the check before the apply confirmation dialog; a lock race detected later by the worker is logged as a localized error without a traceback.

New write operations should call `ensure_safe_to_mutate()` at the orchestrator level and rely on `safe_write.py` for defense in depth.

## Reports

Reports are written under the configured output directory.

Audit reports:

```text
summary.csv
<PlatformName>/missing_on_disk.txt
<PlatformName>/not_in_database.txt
```

Dedupe reports:

```text
duplicate_additional_apps.csv
<PlatformName>/duplicate_additional_apps.txt
```

Path replacement reports:

```text
path_replacements.csv
<PlatformName>/path_replacements.txt
```

The `--only-with-findings` mode should avoid creating detail files for clean platforms and remove stale generated detail files where appropriate.

Mutation CSV/TXT reports use `state`, not an independent `applied` flag, and repeat the manifest path or manifest error so CLI, GUI, reports, and recovery metadata describe the same result.

## GUI Notes

- GUI uses built-in `tkinter`.
- Interface languages are Russian and English.
- Texts live in `gui/translations.py`.
- Long operations run in a background thread.
- GUI workers are non-daemon threads, so process exit cannot interrupt commit or rollback.
- Worker logs are passed to the UI via `queue.Queue`.
- `WM_DELETE_WINDOW` closes immediately while idle. During `scan` or `stage`, it asks for confirmation, requests cooperative cancellation, and closes only after the worker exits. During commit, rollback, or finalization, the close request is deferred and the window closes automatically only after the protected worker finishes.
- Path edits in the GUI are saved back to `launchbox_utils.ini`.
- Apply operations run `ensure_safe_to_mutate()` on the UI thread before confirmation and before starting the worker.

## Adding New Operations

Recommended flow:

1. Add or extend dataclasses in `models.py` if needed.
2. Add XML reading helpers in `xml_repository.py` only if they are shared.
3. Add operation logic in `launchbox_tools/operations/<operation>.py`.
4. Add report writing in `launchbox_tools/reports/<operation>_reports.py`.
5. Add CLI wiring in `cli.py`.
6. Add GUI controls in `gui/app.py` only as a thin wrapper.
7. Add tests to the matching subsystem module under `test/` (for example,
   `test_audit.py` or `test_safe_write.py`). Keep shared LaunchBox tree and XML
   builders in `test/support.py`.

For write operations, follow the safety rules above before exposing apply mode. Call `ensure_safe_to_mutate()` from the operation orchestrator and route writes through `safe_write.py`.

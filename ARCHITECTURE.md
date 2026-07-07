# Architecture

This project is a small Python utility suite for auditing and maintaining LaunchBox data.

## Goals

- Keep LaunchBox database operations understandable and testable.
- Keep read-only analysis separate from operations that modify XML.
- Reuse the same business logic from CLI and GUI.
- Avoid external runtime dependencies where practical.

## High-Level Structure

```text
launchbox_utils.py              # CLI entrypoint
launchbox_tools/
  cli.py                        # command-line interface
  config.py                     # INI configuration loading and saving
  models.py                     # shared dataclasses
  paths.py                      # path normalization and report folder naming
  xml_repository.py             # LaunchBox XML reading helpers
  safe_write.py                 # backup and safe XML write helpers
  operations/
    audit.py                    # read-only ROM audit
    dedupe_additional_apps.py   # AdditionalApplication dedupe operation
  reports/
    audit_reports.py            # audit report writers
    dedupe_reports.py           # dedupe report writers
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
- `safe_write.py` owns backup and safe XML replacement behavior.
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

For Additional Apps dedupe, duplicates are records where both values match:

- `GameID`
- normalized `ApplicationPath`

The first matching entry is kept; later entries are treated as duplicates.

## Safety Rules For XML-Modifying Operations

Any operation that modifies LaunchBox XML must follow these rules:

- Dry-run must be available and should be the default user-facing mode.
- Apply must be explicit.
- LaunchBox should be closed before apply.
- A backup must be created before writing XML.
- XML should be written to a temporary file first.
- The temporary XML must be parsed successfully before replacing the original file.
- Only the intended XML elements should be changed.

Use `safe_write.py` for backup and safe replacement rather than writing XML directly.

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

The `--only-with-findings` mode should avoid creating detail files for clean platforms and remove stale generated detail files where appropriate.

## GUI Notes

- GUI uses built-in `tkinter`.
- Interface languages are Russian and English.
- Texts live in `gui/translations.py`.
- Long operations run in a background thread.
- Worker logs are passed to the UI via `queue.Queue`.
- Path edits in the GUI are saved back to `launchbox_utils.ini`.

## Adding New Operations

Recommended flow:

1. Add or extend dataclasses in `models.py` if needed.
2. Add XML reading helpers in `xml_repository.py` only if they are shared.
3. Add operation logic in `launchbox_tools/operations/<operation>.py`.
4. Add report writing in `launchbox_tools/reports/<operation>_reports.py`.
5. Add CLI wiring in `cli.py`.
6. Add GUI controls in `gui/app.py` only as a thin wrapper.
7. Add tests in `test/test_launchbox_utils.py` or split tests by operation when the file becomes too large.

For write operations, follow the safety rules above before exposing apply mode.

# Contributing

Thank you for your interest in LaunchBox Utils.

## Before You Start

- This project targets **Windows** and **Python 3.10+**.
- Runtime dependencies are limited to the Python standard library.
- Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for module boundaries and safety rules before changing XML-mutating code.
- Review the existing [architecture decision records](docs/decisions/README.md) before proposing significant architectural or product changes.
- Follow the [roadmap planning and acceptance rules](docs/roadmap-workflow.md) before starting P0 or cross-cutting work.

## Development Setup

```powershell
git clone https://github.com/DanilaSpevak/launchbox-utils.git
cd launchbox-utils
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy launchbox_utils.example.ini launchbox_utils.ini
```

Edit `launchbox_utils.ini` with your local LaunchBox paths for manual testing.

## Running Tests

```powershell
python -m unittest discover -s test -p "test_*.py" -v
```

Tests use temporary fake LaunchBox trees and must not modify a real database.

## Pull Requests

1. Keep changes focused and match existing style.
2. Add or update tests for behavior changes.
3. Update README or ARCHITECTURE when user-facing behavior changes.
4. Record significant new architectural or product directions as a `Proposed` ADR; add implementation work to the roadmap only after the decision is `Accepted`.
5. For P0 and cross-cutting changes, include the agreed invariants, scope, implementation slices, and acceptance evidence described in the [roadmap workflow](docs/roadmap-workflow.md).
6. Mark a roadmap item `[x]` only after acceptance criteria and required validation pass; completing the main implementation is not sufficient.
7. Do not commit `launchbox_utils.ini`, report output, or backup folders.

## XML Mutation Rules

Any operation that modifies LaunchBox XML must:

- default to dry-run
- create backups before apply
- block apply when LaunchBox is running or database files are locked
- write XML atomically with post-write validation

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for details.

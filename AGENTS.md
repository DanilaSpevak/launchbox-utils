# Project agent contract

- Read `ARCHITECTURE.md` before changing XML, filesystem mutation, process,
  cancellation, recovery, or GUI lifecycle behavior.
- Follow `docs/roadmap-workflow.md` for roadmap selection, design gates, review
  classification, acceptance, and `[x]` semantics.
- For autonomous roadmap work, use the project skill
  `$launchbox-autonomous-roadmap` from
  `.agents/skills/launchbox-autonomous-roadmap/`.
- Never mutate real LaunchBox data. Tests and smoke checks must use temporary
  fake roots unless the owner explicitly supplies and authorizes another target.
- Keep one write-capable implementer per worktree. Preserve unrelated user
  changes; never stash, reset, amend, or include them in task commits.
- Work only in the task-branch family defined by the roadmap workflow after the
  one-time bootstrap: `agent/<work_item_id>`, implementation
  `agent/<work_item_id>-refresh-<N>`, or audit
  `agent/<work_item_id>-audit-refresh-<N>`. Push and maintain a draft PR only
  when the current prompt/Goal explicitly grants task-branch push authority;
  otherwise enter `decision_required`. Never merge, tag, or release.
- Before review, run focused checks plus:

  ```powershell
  python -m unittest discover -s test -p "test_*.py" -v
  python -m compileall -q launchbox_tools test
  git diff --check
  ```

- P0 and cross-cutting candidates require a fresh independent read-only reviewer
  and the adversarial acceptance evidence defined by the roadmap workflow.
- Stop at `decision_required`; ask one concrete owner question with minimal
  mutually exclusive options and their consequences.

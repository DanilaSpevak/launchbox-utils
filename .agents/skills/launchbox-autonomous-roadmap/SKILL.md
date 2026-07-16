---
name: launchbox-autonomous-roadmap
description: Orchestrate independently reviewed LaunchBox Utils roadmap work from the highest-priority eligible item, including an upper unprepared item that needs a design plan, through validation, review-fix commits, acceptance, and PR handoff. Use when asked to work autonomously from ROADMAP.md, continue the next P0-P3 task, run an implementer/reviewer loop, execute an audit milestone, or resume a work item whose PR state follows docs/roadmap-workflow.md.
---

# LaunchBox Autonomous Roadmap

## Establish authority

1. Read `AGENTS.md`, `ROADMAP.md`, `ARCHITECTURE.md`,
   `CONTRIBUTING.md`, and `docs/roadmap-workflow.md` completely.
2. Before selecting new work, reconcile any current `work_item_id` in the durable
   Goal. Treat its PR state block as authoritative. If the sole terminal PR in a
   valid superseded chain is owner-merged from `awaiting_merge`, verify canonical
   ancestry, `accepted_code_sha`, closeout and `[x]`, record `integrated` in the
   merged PR, and only then clear the current item and select another. Any
   mismatch is `decision_required`.
3. When resuming an existing item, treat the PR state block as authoritative.
   Verify the remote claim, exact `base_sha`, branch, previous state, roles, and
   review history before acting. A new `unclaimed` item has no PR state yet.
4. Verify that the current prompt or Goal explicitly authorizes task-branch push
   and draft-PR actions before contacting GitHub. Triggering this skill alone
   grants no remote authority.
5. Stop with `decision_required` for every condition named by the workflow.
   Never invent a requirement, broaden scope, merge, tag, release, or touch real
   LaunchBox data.

## Prepare and claim

1. Select the uppermost eligible `[ ]` item, even when it still needs an
   execution plan/design gate. Never skip it for a lower item already marked
   ready. Respect enablers and suspended-audit exceptions.
2. Derive `work_item_id` from the exact bold title between the first `**...**` on
   the roadmap item line. Set `<priority>` to the lowercase exact `P0`-`P3`
   section token. Lowercase the title, take every maximal `[a-z0-9]+` run in
   order, and join those runs with `-` after `<priority>-`. For example,
   the current hard-link P0 becomes
   `p0-mutation-lock-mutable-hard-link-handle-race`. If the title has no ASCII
   run, use `<priority>-item-<first 12 hex of SHA-256(exact UTF-8 title bytes)>`.
   If a slug collides with a different exact title, append the first 12 hex of
   that title's digest; if that still collides, append the full 64-hex digest.
   Duplicate identical titles in one priority are `decision_required`. Use this
   algorithm for dependency IDs too.
3. With explicit remote authority, query the complete branch/PR family before
   choosing new versus resume: `agent/<work_item_id>`, implementation
   `-refresh-<N>`, and audit `-audit-refresh-<N>` branches, plus all PRs whose
   head matches one of them.
   - no related branch and no related PR means a new claim;
   - exactly one open draft PR targeting the canonical default branch means
     resume only when its head branch exists, its authoritative state has
     matching `work_item_id`, `branch`, `base_sha`, claimant and valid history,
     and every older related PR is closed and explicitly links forward as
     superseded toward this active PR;
   - a sole terminal merged PR is handled only by the durable-Goal reconciliation
     in Establish authority; without that current Goal identity it is
     `decision_required`, not a new claim;
   - every other combination, including an orphan branch/PR, multiple active
     PRs, non-draft active PR, unsuperseded closed PR, broken superseded chain,
     or mismatched state, is `decision_required`.
   On resume, continue to the isolated-worktree step on the authoritative state's
   exact branch; do not recreate the claim. Dispatch from the recorded status and
   execute only its permitted next transition: never replay preparation,
   implementation, review, or closeout steps already represented by state.
4. For a new claim, render the appropriate template from this skill's own
   `assets/` directory in memory or outside the repository as `unclaimed`. Read
   canonical default-branch HEAD from the remote API immediately before claim,
   store it as `base_sha`, and use the GitHub connector create-branch API to
   create `agent/<work_item_id>` from that exact SHA. Do not use a local tracking
   ref or replace this atomic create with a check-then-push sequence. Existing
   branch or missing connector authority is `decision_required`.
5. Before any repository write, attach a clean isolated writer worktree to the
   exact task branch. For a new claim, create a local tracking branch at the
   remote claim SHA and require `HEAD == base_sha`, the exact upstream, and a
   clean worktree. For resume, fetch the exact remote branch, attach its local
   tracking branch in an isolated worktree, and verify remote HEAD and the commit
   graph against status-dependent `design_plan_sha`, `candidate_sha`, and
   `accepted_code_sha`. A branch already attached elsewhere is usable only when
   that worktree is the clean isolated task worktree for the current writer;
   otherwise stop with `decision_required`.
6. For a new claim, materialize the plan as
   `docs/plans/<work_item_id>.md`, change its bootstrap snapshot to `preparing`,
   set `claimed_by` to `goal:<thread-id>/orchestrator:<agent-name>`,
   commit the plan, push it, and open a draft PR. The plan block freezes as a
   bootstrap snapshot; the PR state block becomes authoritative. Re-query by
   exact head/base and require this to be the sole open draft PR before any
   further transition.
7. For an implementation item, obtain an independent design verdict for the
   exact plan commit as a tamper-evident PR review/comment. Record the commit as
   `design_plan_sha`, plus the verdict's external ID and SHA-256 of exact UTF-8
   bytes. Re-read the external verdict and verify its hash immediately before
   `preparing → ready`. Any plan change while `preparing` requires a new design
   review; a plan change after `ready` is a Specification gap. For an audit item,
   use the audit plan, record the new auditor in authoritative state, and route
   `preparing → auditing`; do not pass through `ready` or assign an implementer.
8. Keep the draft PR state block synchronized after every transition. Before the
   first product-code commit, record the assigned implementer there and complete
   `ready → implementing`.

## Implement and review

1. Assign one write-capable implementer. Make a focused implementation commit,
   run the specified pre-review gate, and record the candidate SHA.
2. Assign a fresh read-only reviewer that is absent from all author roles and
   prior review rounds. Review the complete `base_sha..candidate_sha` range.
3. Classify every finding. Return Blocker/Regression to the implementer and make
   one `fix(review-N): ...` commit per review round. Record reviewer identity,
   candidate SHA, immutable verdict ID, and verdict in `review_history`.
4. Stop after five rounds, after the same defect returns twice, or on any
   Specification gap. Do not hide Hardening/Refactor in the current candidate.

## Validate and close out

Run at minimum:

```powershell
python -m unittest discover -s test -p "test_*.py" -v
python -m compileall -q launchbox_tools test
git diff --check
```

Add real Windows, process, or hidden-Tk checks required by the work-item plan.
Before `accepted` or closeout, read PR CI for the exact `candidate_sha` and
require successful, non-skipped Windows matrix jobs for Python 3.10, 3.11, 3.12,
and 3.13. A stale/mismatched SHA, missing, skipped, cancelled, inaccessible, or
failed required job blocks acceptance; handle unavailable or repeatedly
infrastructure-failing CI through `decision_required` as defined by the workflow.

After a positive independent verdict and green CI, verify that canonical `main`
still equals `base_sha`. Copy `assets/acceptance-report-template.md` to
`docs/plans/<work_item_id>-acceptance.md`; on implementation refresh `<N>`, use
`docs/plans/<work_item_id>-refresh-<N>-acceptance.md`. Never overwrite historical
evidence. Record the exact verdict bytes, external ID, and SHA-256. A
metadata-only closeout may set `[x]` and move the PR to `awaiting_merge`; it must
not change verified behavior.

Do not start the next item until the accepted result is integrated into
canonical `main`. Never perform the merge yourself.

## Handle drift and audits

- For implementation baseline drift, follow the replacement-branch/PR protocol
  in the workflow. Create a refreshed plan commit, clear candidate, design,
  verdict and review fields, set `preparing`, and obtain a new independent design
  verdict before `ready → implementing` and transfer of task commits.
  A superseded PR is the only permitted second historical PR: it uses a distinct
  refresh branch and must be closed with a link to the sole new authoritative
  draft PR after state transfer.
- For audit work items, copy `assets/audit-work-item-template.md`, use
  `auditing → recording → reviewing`, and close with
  `assets/audit-acceptance-report-template.md` at
  `docs/plans/<work_item_id>-audit-acceptance.md`; on audit refresh `<N>`, use
  `docs/plans/<work_item_id>-audit-refresh-<N>-acceptance.md`. Never overwrite
  historical evidence. Use auditor and recorder roles, not an implementer.
  Record the new auditor before entering `auditing` and the recorder before
  `auditing → recording`. Keep remediation findings proposed until the owner
  accepts their roadmap priority.
- To repeat a suspended audit after its accepted remediation is integrated, use
  the workflow's audit-refresh protocol. Never route an audit through
  `implementing` or reuse the implementation drift reset.
- Choose refresh `N` deterministically as one plus the greatest numeric suffix
  already present for that exact refresh family across remote branches and PRs.
  The empty-family maximum is `0`; suffixes are decimal `[1-9][0-9]*` without
  leading zeros. Malformed suffixes are `decision_required`. Atomic create
  remains the final collision guard.
- Keep every helper process bounded by a timeout and clean up only resources
  created by the current work item.

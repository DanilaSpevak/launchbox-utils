## Result

<!-- State the accepted roadmap result, not an implementation recap. -->

## Autonomous work-item state

<!-- This is the authoritative live state after the first plan commit. -->

```yaml
work_item_id: <stable-id>
work_item_type: implementation
status: preparing
depends_on: []
base_sha: <commit>
candidate_sha: null
accepted_code_sha: null
branch: agent/<stable-id>
review_round: 0
claimed_by: <orchestrator-id>
implementer: null
recorder: null
design_reviewer: null
design_plan_sha: null
design_verdict_id: null
design_verdict_sha256: null
review_history: []
auditor: null
decision_required: null
resume_state: null
verdict_id: null
verdict_sha256: null
```

## Contract and scope

- Execution plan: `<exact variant path>`
  - initial: `docs/plans/<work_item_id>.md`
  - implementation refresh: `docs/plans/<work_item_id>-refresh-<N>.md`
  - audit refresh/repeat: `docs/plans/<work_item_id>-audit-refresh-<N>.md`
- In scope:
- Out of scope:
- Blocking invariants:

For `work_item_type: audit`, use the audit templates packaged with
`$launchbox-autonomous-roadmap` and the `auditing → recording → reviewing` path.

## Validation

- [ ] Focused tests
- [ ] Full unittest discovery
- [ ] `compileall`
- [ ] `git diff --check`
- [ ] Required Windows / process / GUI checks
- [ ] PR CI for exact `candidate_sha`: Windows Python 3.10
- [ ] PR CI for exact `candidate_sha`: Windows Python 3.11
- [ ] PR CI for exact `candidate_sha`: Windows Python 3.12
- [ ] PR CI for exact `candidate_sha`: Windows Python 3.13
- [ ] Independent acceptance verdict

## Findings and follow-ups

- Blocker:
- Regression:
- Specification gap:
- Hardening:
- Refactor:

## Integration boundary

- [ ] Canonical `main` still equals `base_sha`
- [ ] Exact verdict ID and SHA-256 recorded
- [ ] Closeout is metadata-only
- [ ] Merge is left to the project owner

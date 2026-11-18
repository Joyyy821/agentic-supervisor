# Agentic MAPF supervisor maintenance — 2026-11-18

The existing project is reconciled and **paused for M2b review**. This maintenance
does not complete M2b's remaining recovery/control tests or authorize M3.

| Item | Verified result |
| --- | --- |
| Run | `b0b92d1f-958e-4e3b-b65c-ddc91876c311` |
| Native high-level thread | `01a18bc4-bef1-7392-a8cf-af63cf5ed4d5` |
| Runtime | `/data/yangjiao/agentic-supervisor/amapf` |
| Native binary | `/data/yangjiao/agentic-supervisor/tools/codex/0.166.0/package/bin/codex` |
| Ledger | `paused`, desired `paused`, `AMAPF-M2b/review` |
| Policy | Advisory project tokens; `auto_resume_on_usage_reset=true` |
| Native thread/goal | Idle / paused; latest completed turn remains `01a1f01d-a6fb-7b33-b8c6-e187854b7895` |
| Owned processes | App Server PID 60226 and TUI PID 67493 preserved; worker recovered as PID 2443055 |
| Locks | Worker flock held by recovered worker; lifecycle lock is owner-only (0600) and available for real synchronization |
| Low-level supervisor gate | `desired_status=paused`, `review_gate=true`, `reason=supervisor_stop_or_review` |

## Diagnosis and prerequisites

Before maintenance, the worker pane was dead; its retained log contained a
`KeyboardInterrupt` and the ledger's last worker-failure event dated to
2026-11-02 UTC. Native TUI work continued in the same thread through later
milestones while bookkeeping remained M1a/review, `recovering`, desired `paused`.
The absent lifecycle-lock file was not evidence of a broken OS lock or authority
to resume. Normal tested supervisor lock acquisition created the real lock;
no dummy file or forced active authorization was used.

Host-side read-only process/container inspection found no project experiment
running. Native reads confirmed idle thread, paused goal and terminal recent
turns. No pending approval, queued lifecycle control, uncertain attempt or
unfinished recorded side effect was found. The original App Server/TUI remained
alive, so neither was restarted or replaced.

## Changes and evidence

Private evidence root:
`/data/yangjiao/agentic-supervisor/tests/maintenance-20261118-eeefba45/`.
SQLite backup API snapshots in `before/` include supervisor state, native goal
and thread metadata, plus the original rollout, creation config, native config
and worker log. `backup-manifest.json` records hashes. No authentication file
was copied. Backups and reports are private; do not publish their raw contents.

The new generic `reconcile` command was tested before use. `checkpoint.json`
references M1a audit, scoped M1b bridge validation, M2a readiness, and current
M2b reports, with hashes and a late-recording event. Physical-safety unknowns
remain unknown. M2b is explicitly a review checkpoint, not a completed stage.
Obsolete M1a-only acceptance checks were archived in the reconciliation event
and removed from the effective config; no experiment or validation was replayed.
The original run-creation config remains unchanged.

Commands executed from `/home/yangjiao/workspace/agentic-supervisor`:

```sh
export AGENTIC_SUPERVISOR_HOME=/data/yangjiao/agentic-supervisor/amapf
ASUP=/home/yangjiao/workspace/agentic-supervisor/.runtime/agentic-supervisor/bin/agentic-supervisor
RUN=b0b92d1f-958e-4e3b-b65c-ddc91876c311
EVIDENCE=/data/yangjiao/agentic-supervisor/tests/maintenance-20261118-eeefba45
"$ASUP" reconcile "$RUN" --checkpoint "$EVIDENCE/checkpoint.json"
"$ASUP" policy "$RUN" --unbound-tokens --auto-resume on
"$ASUP" recover "$RUN"
```

`reconciled-status.json`, `policy-status.json` and two observations in
`verification.json` record success. The monitoring worker retained pause/review,
and native reads showed no new turn. Original controls/attempts remain unchanged;
the rollout retains the entire original byte prefix with only a native
`thread_goal_updated` record appended when recovery reaffirmed the existing
pause. `preservation.json` records these checks. No Isaac, model work or quota
exhaustion test was launched.

## Validation

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  AGENTIC_SUPERVISOR_TEST_ROOT=/data/yangjiao/agentic-supervisor/tests/mr-1811 \
  python3 -m unittest discover -s tests -v
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  AGENTIC_SUPERVISOR_CODEX=/data/yangjiao/agentic-supervisor/tools/codex/0.166.0/package/bin/codex \
  AGENTIC_SUPERVISOR_PROTOCOL_TEST=1 \
  AGENTIC_SUPERVISOR_TEST_ROOT=/data/yangjiao/agentic-supervisor/tests/mr-1811 \
  python3 -m unittest tests.integration.test_protocol_contract -v
```

Regression: 68 tests, 66 passed and two opt-in tests skipped. The separately
enabled native protocol test passed without model requests. Coverage includes
manual pause, pending approvals, review gates, uncertain delivery, actual fake
worker process restart, and at-most-once continuation dispatch. Eight new
checkpoint tests cover refusal/idempotency/evidence and migration/recovery.
Artifact directories are outside Git. Source/diff and Markdown link checks
passed. The MAPF implementation and primary checkout were untouched.

## Accounting and remaining constraints

Supervisor goal accounting remains 293,107 tokens and 505.59074783325195 active
seconds; the existing time ceiling remains 10,800 seconds. Later direct native
work was outside that goal's accounting. Native rollout usage at
2026-11-18T19:28:58Z separately reports 226,693,037 total tokens (including
222,192,896 cached input tokens). These have different scopes; neither history
was overwritten or falsely assigned to past milestone stages. The late
checkpoint documents this gap and starts a new reporting baseline.

The paused native goal still describes historical M1a and retains its dormant
320,000-token cap. Per the supported policy procedure, the next **authorized**
resume installs the then-current milestone and `tokenBudget=null` on this same
thread. No cap can terminate work while it remains paused. Existing sandbox,
`on-request` approvals and `auto_review` remain unchanged. External writable
artifacts still use the original M1a scope and per-run artifacts; additional
external writes need normal scoped authorization. Do not infer broader access
from the new milestone name.

Automatic recovery is configured, but intentionally ineligible during review.
Project token policy does not remove low-level episode/action/time bounds.
Later it requires an authorized active milestone, a genuine provider usage
limit, explicit provider availability, no approval/uncertain effects, and the
same thread. This maintenance does not prove the M2b low-level
fresh-episode/GPU-release protocol end-to-end. The documented native-TUI race
also remains: separate native clients cannot transact atomically with the
supervisor lifecycle lock. Review those gates before enabling live recovery.

## Inspect and later authorize continuation

Inspect or attach now without resuming:

```sh
export AGENTIC_SUPERVISOR_HOME=/data/yangjiao/agentic-supervisor/amapf
ASUP=/home/yangjiao/workspace/agentic-supervisor/.runtime/agentic-supervisor/bin/agentic-supervisor
RUN=b0b92d1f-958e-4e3b-b65c-ddc91876c311
"$ASUP" status "$RUN"
"$ASUP" events "$RUN" --follow
TERM=screen-256color "$ASUP" attach "$RUN"
```

After human review and authorization, assign a new generation of M2b and resume
through the supervisor, preserving this run/thread. For example:

```sh
"$ASUP" milestone "$RUN" AMAPF-M2b --objective "Validate the remaining M2b quota-recovery protocol against the reconciled supervisor using simulated provider resets. Preserve prior evidence, do not exhaust real quota, and stop for review."
"$ASUP" resume "$RUN"
```

These continuation commands were **not executed**. Do not merely type a new task
in the native TUI while its ledger remains paused/review; doing so can reproduce
the divergence. Normal `pause` remains available. `stop` is terminal.

Rollback of code can use the prior `0a4ae6b` in another coordinated maintenance
window; it understands the migrated advisory policy and existing schema. Do not
restart a pre-feature worker, restore the stale backup over a live database, or
rewind Codex history. Native/ledger state must be reconciled before any restore.

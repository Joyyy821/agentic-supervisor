# Optional token warnings and provider usage recovery

Reviewed on 2026-11-13 using isolated fixtures and the installed Codex CLI
protocol. The active Agentic MAPF runtime and its database were not restarted,
edited or migrated as part of this review.

## Contract and behavior

`codex app-server generate-json-schema --experimental --out
/data/yangjiao/agentic-supervisor/tests/policy-schema-20261113` confirmed:
nullable native `ThreadGoal.tokenBudget`, structured `TurnError.codexErrorInfo`
including `usageLimitExceeded`, `rateLimitExceeded` and
`contextWindowExceeded`, `ThreadGoalStatus.usageLimited`, and
`account/rateLimits/read` with `ordinaryUsageAllowed`, `rateLimitReachedType`
and `resetsAt`. Official [App Server documentation](https://learn.chatgpt.com/docs/app-server)
describes the account rate-limit read and reset fields. The runtime does not
call reset-credit redemption, purchase, email, or reserve fallback methods.

New runs set the native goal's `tokenBudget` to null. An optional
`token_warning_threshold` gives advisory event warnings at each crossed
multiple; absent threshold warns every 100,000 tokens. `token_budget` remains
accepted in new JSON as a deprecated alias for this warning threshold.
Historical run snapshots retain their original hard limit and `status` labels
them `legacy_hard_limit`; they are not silently migrated. An explicit `policy`
operation can migrate a confirmed paused, idle run with its worker stopped.
The elapsed-time budget remains enforced. Cumulative token/stage accounting,
Git checkpoints, validation attempts and review gates remain.

With `auto_resume_on_usage_reset: true` on a new Codex 0.166.0 run, an
explicit provider rate/usage error plus backend-classified temporary limit
enters durable `usage_wait`. Polling is spaced 5–60 minutes or until a reported
reset (with a minimum 60-second delay). Only `ordinaryUsageAllowed: true`
permits same-thread continuation. Credit depletion, spend control, context
window, authentication, arbitrary errors, manual pause, completed/review
milestones and uncertain effects do not auto-resume. Supervisor pause/stop is
serialized against automatic dispatch. A lost acknowledgement becomes an
`outcome_unknown` control and is not automatically replayed. Independent TUI
actions are not atomic with the supervisor's final native status check.

## Verification

Command from the isolated supervisor worktree:

```sh
AGENTIC_SUPERVISOR_TEST_ROOT=/data/yangjiao/agentic-supervisor/tests/i7 \
  AGENTIC_SUPERVISOR_CODEX=/data/yangjiao/agentic-supervisor/tools/codex/0.166.0/package/bin/codex \
  AGENTIC_SUPERVISOR_PROTOCOL_TEST=1 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -q
```

Result: 60 tests ran, 59 passed, and the opt-in authenticated coding smoke test
was skipped. The same suite passed with the pinned 0.144.4 executable and test
root `/data/yangjiao/agentic-supervisor/tests/i6` (60 tests, one skip). Both
runs included the real no-model App Server contract test. The regression suite
exercises provider exhaustion/reset, a real worker-process
restart with mocked transport, manual pause during a quota read, a pending native
approval, review gates, preserved queued instructions, legacy migration, and
an acknowledgement loss that is never replayed. It cannot guarantee exactly-once
effects outside the supervisor. No real account quota was exhausted. Generated
fixtures are under `/data/yangjiao/agentic-supervisor/tests/`; no generated
data is tracked. A separate no-model native protocol test passed against both
0.166.0 and 0.144.4, including a 100-token goal changed to `tokenBudget: null`
and read back as unbounded. This validates the protocol operation, not a live
quota reset.

## Adoption and limits

A new configuration file does not change an existing run snapshot. New generic
projects can omit token fields and opt in to automatic provider reset recovery.
`pause RUN_ID` cancels a pending wait, and `status`/`events` show the wait and
poll history. The exact paused-run migration procedure and rollback caveat are
in [operations](operations.md#changing-policy-on-an-existing-run).

The account permission flag can be absent; in that case, the worker waits
with spaced reads and does not infer recovery from elapsed time or percentages.
Native goal edits in the TUI are detected by its `updatedAt` counter when
available. App Server transport and provider fields are version-sensitive;
only 0.166.0 enables this opt-in mechanism. Recovery cannot certify completion
of arbitrary external effects, and a continuation acknowledged during a
disconnect may require manual resolution.

The read-only ledger for the previously recorded Agentic MAPF run showed thread
`01a18bc4-bef1-7392-a8cf-af63cf5ed4d5`, M1a/review, desired paused, a legacy
320,000-token cap and no uncertain controls or attempts at the time of review.
That record is not proof of which session is executing M2a. Migration is
technically supported on the same run and thread only after the documented
pause, worker-stop and native-idle checks; it was not performed here.

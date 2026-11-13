# Optional token warnings and provider usage recovery

Verified on 2026-11-13 using the installed Codex CLI 0.166.0 schema. This is
an isolated feature branch; the active Agentic MAPF runtime and its database
were not restarted, edited or migrated.

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
Historical run snapshots retain their original value and `status` labels
them `legacy_hard_limit`; they are not silently migrated. The elapsed-time
budget remains enforced. Cumulative token/stage accounting, Git checkpoints,
validation attempts and review gates remain.

With `auto_resume_on_usage_reset: true` on a new Codex 0.166.0 run, an
explicit provider rate/usage error plus backend-classified temporary limit
enters durable `usage_wait`. Polling is spaced 5–60 minutes or until a reported
reset (with a minimum 60-second delay). Only `ordinaryUsageAllowed: true`
permits same-thread continuation. Credit depletion, spend control, context
window, authentication, arbitrary errors, manual pause, completed/review
milestones and uncertain effects do not auto-resume. A lost continuation
acknowledgement is reconciled against native turns before any new dispatch.

## Verification

Command from the isolated supervisor worktree:

```sh
AGENTIC_SUPERVISOR_TEST_ROOT=/data/yangjiao/agentic-supervisor/tests/u13 \
  PYTHONPATH=src python3 -m unittest discover -s tests -q
```

Result: 45 tests run, 43 passed, two opt-in live Codex tests skipped. The
simulated integration cases cover provider exhaustion/reset, worker restart,
same-thread continuation exactly once, manual pause, native goal edits,
review gates, approval-blocked goal, uncertain command effects and exclusion
of context/auth/session-budget errors. No real account quota was exhausted.
`git diff --check` and Python compilation passed. Tests and generated schema
are under `/data/yangjiao/agentic-supervisor/tests/`; no generated data is
tracked.

## Adoption and limits

Keep this branch separate while the existing M1b agent owns its run. Install
or merge it only during a coordinated pause/review; a new configuration file
does not change an existing run snapshot. New generic projects can omit all
token fields and opt in to automatic provider reset recovery. `pause RUN_ID`
cancels a pending wait, and `status`/`events` show the wait and poll history.

The account permission flag can be absent; in that case, the worker waits
with spaced reads and does not infer recovery from elapsed time or percentages.
Native goal edits in the TUI are detected by its `updatedAt` counter when
available. App Server transport and provider fields are version-sensitive;
only 0.166.0 enables this opt-in mechanism. Recovery cannot certify completion
of arbitrary external effects, and a continuation acknowledged during a
disconnect may require manual resolution.

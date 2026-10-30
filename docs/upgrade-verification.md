# Codex 0.166.0 and persistent-project verification

Verified on 2026-10-30. The original [M0 report](m0-verification.md) remains historical evidence for 0.144.4.

## Installation and protocol

- Official release: [rust-v0.166.0](https://github.com/openai/codex/releases/tag/rust-v0.166.0), reported stable/latest by the official GitHub API at installation.
- Installed full standalone package at `/data/yangjiao/agentic-supervisor/tools/codex/0.166.0/package/`; executable: `bin/codex`.
- Downloaded archive SHA-256 matches its published digest: `1d44b28bc4ac9937d7d3532264baa68a507ce364956a0d4ebb856f331cdee254`.
- The user-owned `~/.local/bin/codex` symlink was promoted only after compatibility tests passed. No sudo, system package changes, or VS Code extension changes.
- Rollback binary remains `/home/yangjiao/.codex/packages/standalone/releases/0.144.4-x86_64-unknown-linux-musl/bin/codex`. Prior default-link provenance is saved in `tools/codex/default-before-0.166.0.json`.
- Generated installed-version schemas are under `/data/yangjiao/agentic-supervisor/tests/u166/schema/`.
- Unix WebSocket initialization, two clients, goal controls, model discovery, sandboxed command execution and the native remote TUI work. New resume calls use `excludeTurns` and paginated turn summaries.
- Goal changes now carry explicit user/automatic origin. Observed 0.166.0 preserves token usage across objective replacement; supervisor accounting uses returned counters and supports older reset behavior. An early test double-counted that baseline; this was corrected and recorded explicitly in the test ledger before the final stage passed.
- Unknown releases are rejected. The resolved binary/version are retained per run and checked on recovery.

## Models and permissions

Authenticated `model/list` includes GPT-6.1 Sol, GPT-6 Astra, GPT-6.1 Terra and earlier models. `gpt-6.1-sol` supports `high`; both the smoke thread and project preflight returned that model/effort from App Server. There is no fallback configured for Agentic MAPF because the requested model is available.

Effective settings: `approvalPolicy=on-request`, `approvalsReviewer=auto_review`, workspace-write, sandbox network access false. Project-native web research is configured as `web_search=live`. External report directories are explicit writable roots. No full-access or never-approve mode was used.

The post-launch `doctor --config /data/yangjiao/agentic-mapf/agentic-mapf.json` check passed in the host runtime namespace. It attached two App Server clients to the existing run, confirmed the shared `budgetLimited` goal, and returned eight models including `gpt-6.1-sol` with High reasoning. A check from the restricted VS Code command sandbox could not reach the private Unix socket; this was a namespace limitation, not a lost agent session.

The live smoke requested an explicitly authorized marker file outside its ordinary writable roots. Native events recorded `item/autoApprovalReview/started` and `/completed`, and the marker was created without a human approval response. A separate sandboxed command successfully read source and wrote an allowed report, then received EROFS when writing an ungranted sibling path. Auto-review does not remove that boundary.

## Tests and evidence

All generated test material is under `/data/yangjiao/agentic-supervisor/tests/u166/`; none is tracked. The disposable project is unrelated to robotics.

| Verification | Actual result |
| --- | --- |
| Full regression suite with new native protocol opt-in | 36 tests: 35 passed, one legacy live-test entry skipped; bounded live scenario run separately |
| 0.166.0 two-client protocol contract | Passed |
| 0.144.4 two-client protocol contract | Passed; original authenticated M0 evidence retained |
| Authenticated GPT-6.1 Sol High coding | Passed; fixed `source.py`, preserved test file, independent `python3 -B test_greeting.py` passed |
| Milestone continuity | Audit → implementation → verification, same thread and project objective; each paused for review |
| Budget extension/accounting | Explicit +10,000 tokens and +600 seconds; final cumulative 19,641 tokens, cap 35,000; native and supervisor counters matched |
| Steering/queue | Live steering codeword `cedar` appeared in audit; FIFO paused instruction `queued-once` delivered once in the resumed turn |
| Native TUI | Two PTY client attach/detach cycles passed; owned session and thread survived |
| Worker restart | Passed; same thread, synthetic lost-ack control blocked resume until evidence resolution; no replay |
| App Server restart | Passed; same paused thread, usage and review state retained; test runtime then stopped |
| Config and artifacts | External reports written; model/effort/reviewer read back; original checkout stayed clean |
| Post-launch regression | 37 tests: 35 passed, two opt-in live/protocol tests skipped; includes idempotent budget-limit event handling |

Test run: `96ab50d8-df1e-4798-86d1-f14c767fc490`.
Native thread: `01a18bb5-daf3-7ce3-87b6-63746fe4a09d`.

Commands executed from the supervisor checkout included:

```sh
AGENTIC_SUPERVISOR_PROTOCOL_TEST=1 \
AGENTIC_SUPERVISOR_CODEX=/data/yangjiao/agentic-supervisor/tools/codex/0.166.0/package/bin/codex \
AGENTIC_SUPERVISOR_TEST_ROOT=/data/yangjiao/agentic-supervisor/tests/u166/final \
PYTHONDONTWRITEBYTECODE=1 .runtime/agentic-supervisor/bin/python -m unittest discover -s tests -t . -v

# Run separately against the preserved release as well:
AGENTIC_SUPERVISOR_PROTOCOL_TEST=1 \
AGENTIC_SUPERVISOR_CODEX=/home/yangjiao/.codex/packages/standalone/releases/0.144.4-x86_64-unknown-linux-musl/bin/codex \
AGENTIC_SUPERVISOR_TEST_ROOT=/data/yangjiao/agentic-supervisor/tests/u166/p \
.runtime/agentic-supervisor/bin/python -m unittest tests.integration.test_protocol_contract -v

.runtime/agentic-supervisor/bin/python /data/yangjiao/agentic-supervisor/tests/u166/live.py
.runtime/agentic-supervisor/bin/python /data/yangjiao/agentic-supervisor/tests/u166/recover_test.py
.runtime/agentic-supervisor/bin/python /data/yangjiao/agentic-supervisor/tests/u166/transport_sandbox.py
.runtime/agentic-supervisor/bin/python /data/yangjiao/agentic-supervisor/tests/u166/verify_queue.py
.runtime/agentic-supervisor/bin/python /data/yangjiao/agentic-supervisor/tests/u166/server_recovery.py
```

The live scripts are retained as evidence of this particular run, with fixed paths/IDs; do not replay them over existing test data. Logs include `regression-final.log`, `protocol-new.log`, `protocol-old.log`, `server-recovery.log`, `sandbox-result.json`, `attach-0.log`, `attach-1.log`, and the test SQLite ledger/reports.

One model-capacity error occurred during the audit. The test recovered on the same thread and completed within its budget. This does not prove continuous model capacity. The terminal test exercised client detach/reconnect, not a host reboot or closing the user's actual SSH login. No simulator, Docker resource or existing experiment was launched or changed.

## Agentic MAPF project preparation and launch

Application configuration is outside the supervisor core:

- Config: `/data/yangjiao/agentic-mapf/agentic-mapf.json`.
- Runtime: `/data/yangjiao/agentic-supervisor/amapf/`.
- Worktree: `/data/yangjiao/agentic-mapf-worktree`, branch `agentic-mapf/research`, initial revision `9b280de3889bb3c788b28e7156cf83e660434577`.
- Primary checkout `/home/yangjiao/workspace/mas-vln` was not switched or edited. Existing submodules were uninitialized and remain so; M1a must report that limitation and inspect declared pins/upstream sources without updating them.
- Full user-supplied task: `/data/yangjiao/agentic-mapf/m1a-task.md`, 29,814 bytes, SHA-256 `60e691072e46bf687615b94c34c2ed3949ffcf483a113476102917b62a3dafaf`.
- Reports: `/data/yangjiao/agentic-mapf/m1a/`.
- Initial limits: 80,000 tokens and 7,200 active seconds. Milestone AMAPF-M1a pauses for review; M1b requires explicit assignment/resume.

The obsolete empty M1a-specific runtime/config was archived under `preparation/m1a-legacy-20261030/`. Its credential symlink is no longer inside the agent-writable report directory. The new private Codex home uses the existing authentication through an auth-file symlink, with a separate history database; no credential payload was copied or printed.

The user authorized launch after verification, and M1a was started on 2026-10-30. The authenticated App Server thread returned `gpt-6.1-sol`, `high`, `on-request`, `auto_review`, and `workspaceWrite`; the run snapshot records the effective artifact roots and disabled network access. The complete attached task was submitted and initial repository-inspection commands ran in the dedicated worktree.

### Current M1a run state

- Run: `b0b92d1f-958e-4e3b-b65c-ddc91876c311`.
- Persistent Codex thread: `01a18bc4-bef1-7392-a8cf-af63cf5ed4d5`.
- Current status: `paused`; native goal status: `budgetLimited`.
- Native usage reported: 86,494 tokens against the 80,000-token limit; elapsed time reported: 50.18 seconds against 7,200 seconds. Native enforcement overshot the configured token budget by 6,494 tokens before the worker reconciled the limit.
- The milestone remains `AMAPF-M1a` / `active`, not `review`. No requested M1a report files were present under `/data/yangjiao/agentic-mapf/m1a/` at the last check. M1a is **not complete**, and M1b has not started.
- The saved `/data/yangjiao/agentic-mapf/launch-record.json` contains the launch settings and current observation. The supervisor runtime and Codex history must be preserved. Do not start a second run. Resuming requires an explicit budget extension because the token budget is exhausted; no extension has been made.
- The original worker repeatedly recorded the already-known native `budgetLimited` state. A code fix makes this transition/event idempotent. A controlled restart of only the worker, followed by `recover`, preserved the same run and thread, left the goal paused, and kept the App Server/TUI panes alive. The limit-notice count stayed at 9,035 over an 8-second check after recovery. No budget extension or new Codex turn was issued.

Use the existing run, never `start`, to inspect and reconnect:

```sh
cd /home/yangjiao/workspace/agentic-supervisor
source .runtime/agentic-supervisor/bin/activate
export AGENTIC_SUPERVISOR_HOME=/data/yangjiao/agentic-supervisor/amapf
agentic-supervisor status b0b92d1f-958e-4e3b-b65c-ddc91876c311
agentic-supervisor events b0b92d1f-958e-4e3b-b65c-ddc91876c311 --follow
TERM=screen-256color agentic-supervisor attach b0b92d1f-958e-4e3b-b65c-ddc91876c311
```

Queue steering for the next eligible turn without changing the exhausted budget:

```sh
agentic-supervisor send b0b92d1f-958e-4e3b-b65c-ddc91876c311 --queue "Your steering instruction"
```

After reviewing the current Codex thread and choosing an additional bounded allowance, explicitly extend the budget, then resume the same thread:

```sh
agentic-supervisor budget b0b92d1f-958e-4e3b-b65c-ddc91876c311 --add-tokens 80000 --add-seconds 7200
agentic-supervisor resume b0b92d1f-958e-4e3b-b65c-ddc91876c311
```

The extension command above is an example and has **not** been executed. When M1a eventually reaches its deliverables and configured checks, it should pause for human review; do not assign M1b until explicitly approved. See [operations](operations.md) for lifecycle and budget behavior.

## Rollback and limits

Pause active runs before maintenance. Each run recovers with its recorded binary, so changing the default does not migrate an existing run. To restore the previous default without removing 0.166.0:

```sh
ln -sfn /home/yangjiao/.codex/packages/standalone/releases/0.144.4-x86_64-unknown-linux-musl/bin/codex /home/yangjiao/.local/bin/codex
codex --version
```

Do not open a newer Codex history database with an older binary without validating native storage compatibility. Original M0 private-home data was preserved; new-version test/project homes are separate. Supervisor SQLite migration is additive.

No reboot autostart, unbounded budgets, exactly-once external effects, complete streamed-output reconstruction, or guaranteed automatic resolution of capacity/approval failures is claimed. Review pauses preserve the live thread; `stop` is terminal. Checkpoint hashes do not replace application commits or experiment provenance.

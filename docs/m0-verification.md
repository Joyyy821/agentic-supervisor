# M0 live verification

Date: 2026-10-29
Scope: generic Codex supervisor only. No M1 work or simulator runs.

## Environment

- `doctor` passed with Codex `0.144.4`, tmux `3.2a`, authenticated private Codex home, private Unix WebSocket transport, two App Server clients, and shared goal state. It reported `ready: true` and sent zero model turns.
- Python environment: `.runtime/agentic-supervisor`; private runtime/authentication root: `/data/yangjiao/agentic-supervisor`.
- Removed the old ignored `.runtime/venv-isolated` environment; the named `.runtime/agentic-supervisor` environment is the one used below.
- Disposable coding repository and all live-run artifacts: `/data/yangjiao/agentic-supervisor/tests/m0d-df4db6fc/`. No credentials were copied into it; the test Codex home is a directory symlink to the authenticated private home.
- The first mock-suite invocation used a nested default test directory and exceeded the host's Unix socket path limit. The default test root was shortened to ignored `.runtime/t`; the final suite then passed without a path override.

## Commands and results

| Check | Command or action | Result |
|---|---|---|
| Authenticated environment | `AGENTIC_SUPERVISOR_HOME=/data/yangjiao/agentic-supervisor .runtime/agentic-supervisor/bin/agentic-supervisor doctor` | Passed. `ready=true`, signed in, App Server probe passed, `model_turns_sent=0`. |
| Unit and mocked integration suite | `PYTHONDONTWRITEBYTECODE=1 .runtime/agentic-supervisor/bin/python -m unittest discover -s tests -v` | Passed: 26 tests; two opt-in live tests skipped by default. Outputs were under ignored `.runtime/t`. |
| Native App Server contract | `AGENTIC_SUPERVISOR_PROTOCOL_TEST=1 AGENTIC_SUPERVISOR_TEST_ROOT=/data/yangjiao/agentic-supervisor/tests/protocol-final .runtime/agentic-supervisor/bin/python -m unittest tests.integration.test_protocol_contract -v` | Passed: two clients shared one thread and paused goal; no model turn. Artifacts are under `/data/yangjiao/agentic-supervisor/tests/protocol-final/`. |
| Authenticated coding smoke | `AGENTIC_SUPERVISOR_LIVE_TEST=1 AGENTIC_SUPERVISOR_SMOKE_TOKEN_BUDGET=12000 AGENTIC_SUPERVISOR_TEST_ROOT=/data/yangjiao/agentic-supervisor/tests/m0d-df4db6fc AGENTIC_SUPERVISOR_HOME=/data/yangjiao/agentic-supervisor .runtime/agentic-supervisor/bin/python -m unittest tests.integration.test_live_smoke -v` | Passed in 535.7 seconds. Run `939c2692-e0f7-4d03-b5aa-347782e6c806`, thread `01a18b67-b0b5-7f60-a8b1-7fb17438a0ba`, used 10,899 of 12,000 tokens. The separate `greeting` check exited 0; `greeting.py` prints exactly `hello supervisor`; the fixture `README.md` remained unchanged. |
| Native TUI, steering, and approval | Attached to the run with `agentic-supervisor attach`; sent a live CLI instruction with `agentic-supervisor send`; reviewed and approved the displayed file-creation request in the TUI. | Passed. The send control was accepted (`50c407f9-17e9-4fb9-8739-6dd342c968d9`), and the generated module contains the requested concise docstring. The TUI showed the explicit approval before running the write; no automatic approval was enabled. |
| Terminal disconnect/reconnect | Attached to the owned tmux TUI, detached with `Ctrl-b`, `d`, and reattached to the same run. | Passed at the tmux terminal-client level. The run and pending approval remained in the same Codex thread; after reconnect and human approval the same run completed. The user kept the VS Code/SSH conversation open, so a full VS Code disconnect was not performed. |
| Worker restart and recovery | Killed only this run's owned worker pane, then ran `agentic-supervisor recover 939c2692-e0f7-4d03-b5aa-347782e6c806` with the run's runtime root. | Passed. Recovery reused the same thread ID, restored the worker/TUI, and the run completed without replaying the already completed task. |
| Uncertain-operation handling | Covered by the passing recovery/store tests `test_recover_preserves_pause_and_marks_inflight_work_unknown`, `test_interrupted_work_is_marked_unknown_and_never_requeued`, `test_attempt_requires_explicit_resolution`, and `test_uncertain_control_resolution_records_evidence_without_requeue`. | Passed in controlled tests. The live worker restart had no in-flight check attempt or dispatched control, so no real external side effect was forcibly interrupted. Resolve a real `outcome_unknown` operation only after inspecting its artifacts/process evidence. |

The final live run completed and its independent check passed. Earlier startup experiments exposed two issues that were fixed before the passing run: an empty thread must receive a short read-only bootstrap turn before worker resume, and the persistent TUI must retain its tmux PTY. One earlier bootstrap accidentally ran as an active goal and consumed 20,630 tokens without starting the coding task; that test-owned session was stopped. The final coding run stayed within its 12,000-token limit. The native approval gate was not bypassed.

## Start, attach, steer, and stop

From an SSH shell on the host:

```sh
cd /home/yangjiao/workspace/agentic-supervisor
source .runtime/agentic-supervisor/bin/activate
export AGENTIC_SUPERVISOR_HOME=/data/yangjiao/agentic-supervisor
agentic-supervisor doctor
agentic-supervisor start /path/to/project-config.json
agentic-supervisor list
```

Copy the run ID reported by `start` or `list`, then attach to its native Codex TUI:

```sh
TERM=screen-256color agentic-supervisor attach RUN_ID
```

Review approval prompts in that TUI. To leave the run active, press **Ctrl-b**, release, then press **d**. Disconnecting SSH also detaches its tmux client. After reconnecting, activate the environment and export the same runtime root, then run:

```sh
agentic-supervisor status RUN_ID
agentic-supervisor events RUN_ID --follow
TERM=screen-256color agentic-supervisor attach RUN_ID
```

From another shell, steer or control the same run:

```sh
agentic-supervisor send RUN_ID "Keep the public API unchanged and run the configured checks."
agentic-supervisor send RUN_ID --queue "After the current turn, summarize changes and test results."
agentic-supervisor pause RUN_ID
agentic-supervisor resume RUN_ID
agentic-supervisor stop RUN_ID
```

`stop` shuts down this run's owned processes while retaining its workflow database and artifacts. Use `pause` when work should remain recoverable. Do not use `stop` just to detach.

## Remaining limits

- The disconnect test exercised tmux client detachment/reconnection, not closing the user's entire VS Code SSH connection. The approval was pending during detachment, so the agent correctly waited until a human reconnected and approved; it then continued and completed on the same thread.
- No reboot autostart is configured. A host reboot, expired authentication, account limit, or unanswered approval can stop unattended progress.
- The live restart had no uncertain external operation in flight. The conservative `outcome_unknown` behavior is covered by controlled recovery/store tests, but arbitrary external effects still require evidence and explicit resolution.
- App Server remains experimental and this build accepts only the audited Codex `0.144.4`. That build accepts `approvals_reviewer = "auto_review"` in `CODEX_HOME/config.toml`, but M0 verified only the human TUI approval route; it did not behaviorally test auto-review. The supervisor does not provide a per-run reviewer field. Re-audit the protocol before upgrading Codex.
- The final smoke run's configured token limit was 12,000 and elapsed limit was 600 seconds; token budgets are operational limits, not exact billing caps.

No files in `mas-vln` or pre-existing experiment storage were modified. The only tracked changes for M0 remain in `agentic-supervisor`.

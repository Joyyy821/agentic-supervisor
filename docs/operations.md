# Operations

## Host setup

Install Python 3.10+, Codex CLI, and tmux. Choose a private runtime root outside the target checkout. Keep the root short enough for the host's Unix socket path limit. Set `AGENTIC_SUPERVISOR_HOME` to that path. Run `agentic-supervisor login` once; on a headless server use Codex device login or its SSH callback method. Then run `agentic-supervisor doctor`.

The current M0 deployment uses tmux and does not install user services or modify system lingering. Runs survive terminal/SSH disconnection, not reboot or host failure.

## Start, attach, and reconnect

Create a JSON config with a repository, objective, optional context files, budgets, approved environment-variable names, writable artifact paths, and named checks. From the supervisor checkout, activate the isolated environment and select a private runtime root:

```sh
cd /path/to/agentic-supervisor
source .runtime/agentic-supervisor/bin/activate
export AGENTIC_SUPERVISOR_HOME=/data/yangjiao/agentic-supervisor
agentic-supervisor doctor
agentic-supervisor start /path/to/project-config.json
agentic-supervisor list
```

Copy the run ID printed by `start` or `list`. Attach to its native Codex TUI:

```sh
TERM=screen-256color agentic-supervisor attach RUN_ID
```

The TUI, App Server, and supervisor worker run in an Agentic Supervisor-owned tmux session. Detach with `Ctrl-b`, then `d`; this closes only the terminal client. Closing SSH also disconnects the tmux client, while the owned run keeps running. After reconnecting, restore the same environment and attach to the same run ID:

```sh
cd /path/to/agentic-supervisor
source .runtime/agentic-supervisor/bin/activate
export AGENTIC_SUPERVISOR_HOME=/data/yangjiao/agentic-supervisor
agentic-supervisor status RUN_ID
TERM=screen-256color agentic-supervisor attach RUN_ID
```

Do not run `stop` when you only intend to disconnect. The runtime survives terminal and SSH loss but does not start automatically after a host reboot.

Use `status`, `events RUN_ID --follow`, and `list` for operational state. Steer the existing agent from another shell with `agentic-supervisor send RUN_ID "instruction"`; it writes to the active turn when possible and otherwise records a follow-up or queued instruction. The native TUI remains the place to review and answer Codex approval prompts and to use native goal controls.

## Controls

- `send RUN TEXT` steers an active turn using its current turn ID. If idle, it starts a follow-up. `--queue` writes a FIFO control to SQLite.
- `pause` persists pause intent, pauses the native goal, and interrupts the active turn. Wait for status to report the confirmed state.
- `interrupt` stops an active turn but may leave the goal active.
- `resume` reuses the stored thread, objective, and accumulated supervisor usage.
- `stop` pauses and shuts down only the process group/session recorded for that run.
- `recover` reconciles the owned tmux panes, App Server thread, event history, and pending controls before continuing.
- Once a restart has marked an outcome `outcome_unknown`, `resolve RUN ATTEMPT --type attempt --outcome succeeded|failed|not_run --evidence TEXT` records command evidence. For an uncertain control, use `--type control --outcome accepted|rejected|not_applied`. Resolution updates the ledger only and never retries the operation.

`pause` is a cancellation and checkpoint request, not OS process suspension or rollback. Confirm its observed state with `status`; a separate command already launched outside Codex may need operator inspection. `stop` shuts down only this run's owned processes and keeps its database and artifacts for review.

The supervisor keeps `approval_policy = "on-request"` and a sandbox policy for each run; it does not expose a per-run reviewer field. Codex 0.144.4 accepts `approvals_reviewer = "auto_review"` in the private Codex home `config.toml`. This routes eligible on-request prompts through automatic review while retaining `sandbox_mode = "workspace-write"`; the setting is shared by runs using that Codex home. The M0 live test verified the human TUI route, not auto-review behavior. Recheck the setting after a Codex upgrade, and do not replace it with `approval_policy = "never"` or unrestricted sandboxing. See the [Codex approval profiles](https://learn.chatgpt.com/docs/sandboxing).

## Failures and data

Authentication, account limits, approval prompts, protocol errors, and elapsed-time expiry can block autonomous work. Recover the same run after correcting the cause; do not create a replacement run to bypass uncertain state.

SQLite is under the runtime root. Large outputs and per-run snapshots go under `runs/<run-id>/`. Credentials live in `codex-home/` with owner-only access. Never copy these into a repository or issue tracker. Backups should include the SQLite database and relevant run directories while no writer is active; App Server session files under `codex-home/` are required to resume native history.

Recovery cannot determine whether an external process completed if it died between doing work and recording a result. Inspect output files, process state, and project artifacts, then explicitly resolve the attempt. Budgets reduce runaway work but are not exact billing limits.

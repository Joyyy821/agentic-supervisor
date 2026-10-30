# Operations

## Host setup

Install Python 3.10+, Codex CLI, and tmux. Choose a private runtime root outside the target checkout. Keep the root short enough for the host's Unix socket path limit. Set `AGENTIC_SUPERVISOR_HOME` to that path. Run `agentic-supervisor login` once; on a headless server use Codex device login or its SSH callback method. Then run `agentic-supervisor doctor`.

The current M0 deployment uses tmux and does not install user services or modify system lingering. Runs survive terminal/SSH disconnection, not reboot or host failure.

## Start and reconnect

Create a JSON config with a repository, objective, optional context files, budgets, approved environment-variable names, writable artifact paths, and named checks. Start it with `agentic-supervisor start CONFIG`. The command reports the run ID. Use `agentic-supervisor attach RUN_ID` to open the persistent native TUI; detach using tmux's detach shortcut and reconnect with the same command.

Use `status`, `events --follow`, and `list` for operational state. The native TUI remains the place to answer Codex approval prompts and discuss the ongoing task.

## Controls

- `send RUN TEXT` steers an active turn using its current turn ID. If idle, it starts a follow-up. `--queue` writes a FIFO control to SQLite.
- `pause` persists pause intent, pauses the native goal, and interrupts the active turn. Wait for status to report the confirmed state.
- `interrupt` stops an active turn but may leave the goal active.
- `resume` reuses the stored thread, objective, and accumulated supervisor usage.
- `stop` pauses and shuts down only the process group/session recorded for that run.
- `recover` reconciles the owned tmux panes, App Server thread, event history, and pending controls before continuing.
- Once a restart has marked an outcome `outcome_unknown`, `resolve RUN ATTEMPT --type attempt --outcome succeeded|failed|not_run --evidence TEXT` records command evidence. For an uncertain control, use `--type control --outcome accepted|rejected|not_applied`. Resolution updates the ledger only and never retries the operation.

## Failures and data

Authentication, account limits, approval prompts, protocol errors, and elapsed-time expiry can block autonomous work. Recover the same run after correcting the cause; do not create a replacement run to bypass uncertain state.

SQLite is under the runtime root. Large outputs and per-run snapshots go under `runs/<run-id>/`. Credentials live in `codex-home/` with owner-only access. Never copy these into a repository or issue tracker. Backups should include the SQLite database and relevant run directories while no writer is active; App Server session files under `codex-home/` are required to resume native history.

Recovery cannot determine whether an external process completed if it died between doing work and recording a result. Inspect output files, process state, and project artifacts, then explicitly resolve the attempt. Budgets reduce runaway work but are not exact billing limits.

# Agentic Supervisor

Agentic Supervisor is a standalone, general-purpose runtime for long-running Codex coding and research tasks. It keeps a Codex session available after you disconnect, records workflow state, and provides commands to monitor, steer, pause, resume, and recover it. Codex owns the conversation and agent behavior; the supervisor owns lifecycle and operational records.

The core has no robotics, Isaac Sim, ROS, GPU, or target-project dependencies. It accepts any local Git working tree and optional project commands. Agentic MAPF is an example application, not the purpose of the supervisor.

## Requirements

- Linux, Python 3.10+, Codex CLI, and tmux.
- A Codex account signed in through the supervisor's private Codex home.
- SSH access for reconnecting to a remote host.

The M0 runtime uses Codex App Server's Unix socket and native TUI. App Server is experimental and its protocol can change between Codex releases. This implementation currently accepts only the audited `codex-cli 0.144.4`; `doctor` and `start` reject other versions until the protocol is re-audited and tested. It does not install a system service or configure server boot-time startup.

## Install and authenticate

From this repository, create an isolated Python environment and install the package:

```sh
python3 -m venv .runtime/venv
.runtime/venv/bin/pip install --no-build-isolation --no-deps -e .
```

Some shared Ubuntu hosts do not include `ensurepip` or the `python3-venv` package, and users cannot install that system package without an administrator. On those hosts, install the dependency-free CLI into your account instead:

```sh
python3 -m pip install --user --no-build-isolation --no-deps .
export PATH="$HOME/.local/bin:$PATH"
```

This does not require `sudo` or alter system packages. Add the `PATH` export to your shell startup file if you want it to persist across logins. Reinstall after updating the checkout so the user-level command picks up code changes.

Use a private runtime root for state, Codex credentials, logs, and run artifacts. On this server, a suitable location is `/data/yangjiao/agentic-supervisor`; set `AGENTIC_SUPERVISOR_HOME` to that path. The runtime root is created with owner-only permissions. It is separate from the target project checkout.

Sign in once using the private Codex home:

```sh
agentic-supervisor login
```

On a headless host the command prints a device-login link and one-time code. Complete that flow in a browser. Alternatively, set up the Codex SSH callback forwarding described in [OpenAI authentication documentation](https://learn.chatgpt.com/docs/auth). Credentials are stored under the runtime root and are never included in the SQLite event log or run configuration snapshots.

Check the host before creating a run:

```sh
agentic-supervisor doctor
```

## Quickstart

Copy `examples/generic-project.json` and set the repository, objective, and runtime root:

```json
{
  "version": 1,
  "project": "my-project",
  "repository": "/path/to/my-project",
  "objective": "Fix the parser bug and run the project's test suite.",
  "context_files": [],
  "token_budget": 100000,
  "time_budget_seconds": 7200,
  "checks": []
}
```

Start the autonomous run and attach to its native Codex TUI:

```sh
agentic-supervisor start ./my-project.json
agentic-supervisor list
agentic-supervisor attach RUN_ID
```

The TUI, App Server, and supervisor worker run in an Agentic Supervisor-owned tmux session. Detach with the tmux detach shortcut; this leaves the run active. Reconnect over SSH and run `agentic-supervisor attach RUN_ID` to return to the same TUI and session.

## Monitor and steer

```sh
agentic-supervisor status RUN_ID
agentic-supervisor events RUN_ID --follow
agentic-supervisor send RUN_ID "Keep the public API unchanged and run tests before editing docs."
agentic-supervisor send RUN_ID --queue "After this checkpoint, summarize changed files and test results."
agentic-supervisor pause RUN_ID
agentic-supervisor resume RUN_ID
agentic-supervisor interrupt RUN_ID
agentic-supervisor stop RUN_ID
```

`send` steers an active turn and starts a follow-up when the run is idle. `--queue` persists input for the next available turn. Input sent while paused waits until an explicit resume. `pause` pauses the native goal and interrupts the current turn. `interrupt` cancels the turn but does not promise that the goal remains paused. Use the native TUI for Codex approvals and its native goal controls. Approval routing for turns initiated by the supervisor worker still needs an authenticated native interaction test; M0 does not claim unattended approvals are verified.

Resolve uncertain outcomes with evidence after the supervisor has marked them `outcome_unknown`. For example, use `agentic-supervisor resolve RUN_ID ATTEMPT_ID --type attempt --outcome succeeded --evidence "verified result file and exit record"` for a command, or `agentic-supervisor resolve RUN_ID CONTROL_ID --type control --outcome not_applied --evidence "confirmed no request reached Codex"` for a control. Resolution updates the ledger only; it does not retry the operation.

## Configuration and checks

Configuration paths are resolved relative to the JSON file. See the example for optional model, reasoning effort, environment-variable allowlist, writable artifact paths, and named validation commands. Checks use argument arrays, run through Codex's sandboxed command interface, and record their exit status and declared result files. Core code treats result JSON as opaque and makes no domain-specific assumptions.

The target repository is writable only under Codex's configured sandbox and approval policy. Do not disable the sandbox or automatically accept approval requests. Avoid running concurrent supervisor instances against the same checkout.

## Runtime data and recovery

The runtime root contains a private SQLite database, a private Codex home, Unix sockets, process logs, and per-run directories. Each run records a snapshot of its configuration, Git revision and dirty state, native thread and turn IDs, normalized events, control requests, check attempts, token usage, and artifact hashes. Per-run environment files are mode `0600`; values for explicitly allowlisted environment variables are not embedded in tmux command lines or database snapshots. Keep the runtime root private because it also holds credentials and process logs. Generated files stay under the runtime root; never commit credentials, event logs, databases, or experiment output.

If the worker exits, use `agentic-supervisor recover RUN_ID` to reconnect it to the existing App Server and thread. If the App Server stopped, recovery restarts that owned process against the private Codex home and verifies the stored thread before continuing; the native TUI pane is restarted to attach to the new process. Recovery reconciles saved events and controls before continuing. A command interrupted during a crash can have an unknown outcome; inspect its artifacts and resolve the attempt explicitly instead of blindly repeating it. `resolve` records the operator's outcome and evidence.

M0 supports recovery from SSH disconnection and controlled process interruption. It does not configure reboot autostart, guarantee exactly-once execution of arbitrary external commands, recover a machine failure, or turn a thread resume into proof that an experiment completed. Native token budgets and supervisor elapsed-time limits are operational bounds, not exact billing caps. Authentication expiry, account limits, approval requests, and the experimental App Server protocol can pause progress.

## Development

Run unit tests with `python3 -m unittest discover -s tests/unit -v`. Runtime and live Codex tests are opt-in; set `AGENTIC_SUPERVISOR_TEST_ROOT` to a fresh directory beneath the runtime root before running them. See [architecture](docs/architecture.md), [operations](docs/operations.md), and [repository guidance](AGENTS.md).

# Agentic Supervisor

Agentic Supervisor is a standalone, general-purpose runtime for long-running Codex coding and research tasks. It keeps a Codex session available after you disconnect, records workflow state, and provides commands to monitor, steer, pause, resume, and recover it. Codex owns the conversation and agent behavior; the supervisor owns lifecycle and operational records.

The core has no robotics, Isaac Sim, ROS, GPU, or target-project dependencies. It accepts any local Git working tree and optional project commands. Agentic MAPF is an example application, not the purpose of the supervisor.

## Requirements

- Linux, Python 3.10+, Codex CLI, and tmux.
- A Codex account signed in through the supervisor's private Codex home.
- SSH access for reconnecting to a remote host.

The M0 runtime uses Codex App Server's Unix socket and native TUI. App Server is experimental and its protocol can change between Codex releases. Validated releases are `codex-cli 0.144.4` and `codex-cli 0.166.0`. Other releases are rejected until audited and tested. Pin `codex_executable` in project configuration, use the global `--codex PATH` option, or set `AGENTIC_SUPERVISOR_CODEX`; the resolved executable and version are saved with each run. It does not install a system service or configure server boot-time startup.

## Install and authenticate

From this repository, create an isolated Python environment and install the package:

```sh
python3 -m venv .runtime/agentic-supervisor
source .runtime/agentic-supervisor/bin/activate
python -m pip install --no-build-isolation --no-deps -e .
```

Some shared Ubuntu hosts do not include `ensurepip` or the `python3-venv` package, and users cannot install that system package without an administrator. On those hosts, install the `virtualenv` environment builder into your user account, then keep the supervisor itself inside `.runtime/agentic-supervisor`:

```sh
python3 -m pip install --user virtualenv
python3 -m virtualenv --python "$(command -v python3)" .runtime/agentic-supervisor
source .runtime/agentic-supervisor/bin/activate
python -m pip install --no-build-isolation --no-deps -e .
```

This does not require `sudo` or add the supervisor package to the system Python. The `virtualenv` bootstrap utility is installed only for your account; the supervisor package and its command are isolated in `.runtime/agentic-supervisor`. See the [`virtualenv` installation guide](https://virtualenv.pypa.io/en/stable/how-to/install.html) for other bootstrap options.

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
  "objective": "Maintain and improve this project.",
  "milestone": {"name": "audit", "objective": "Inspect the parser and propose a fix; await review before implementation."},
  "model": "gpt-6.1-sol",
  "reasoning_effort": "high",
  "approvals_reviewer": "auto_review",
  "sandbox_mode": "workspace-write",
  "context_files": [],
  "token_budget": 100000,
  "time_budget_seconds": 7200,
  "checks": []
}
```

Start the autonomous run and attach to its native Codex TUI:

```sh
agentic-supervisor doctor --config ./my-project.json
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

`send` steers an active turn and starts a follow-up when the run is idle. `--queue` persists input for the next available turn. Input sent while paused waits until an explicit resume. `pause` pauses the native goal and interrupts the current turn. `interrupt` cancels the turn but does not promise that the goal remains paused. The supervisor does not implement an approval responder. Set `approvals_reviewer: "auto_review"` in project JSON to route eligible `on-request` prompts through native Codex automatic review; the sandbox still applies. Otherwise, attach to the native TUI to review and answer prompts. The 0.166.0 upgrade test verified automatic review of a scoped file write. Reviewers can deny requests; this is not unconditional approval.

Resolve uncertain outcomes with evidence after the supervisor has marked them `outcome_unknown`. For example, use `agentic-supervisor resolve RUN_ID ATTEMPT_ID --type attempt --outcome succeeded --evidence "verified result file and exit record"` for a command, or `agentic-supervisor resolve RUN_ID CONTROL_ID --type control --outcome not_applied --evidence "confirmed no request reached Codex"` for a control. Resolution updates the ledger only; it does not retry the operation.

## Configuration and checks

Repository/runtime/executable/task/artifact paths are relative to the JSON file; context files, check working directories and relative check results are relative to the target repository. See the example for optional model, reasoning effort, environment-variable allowlist, writable artifact paths, and named validation commands. Checks use argument arrays, run through Codex's sandboxed command interface, and record their exit status and declared result files. Core code treats result JSON as opaque and makes no domain-specific assumptions.

The target repository is writable only under Codex's configured sandbox and approval policy. Keep the sandbox enabled and use either native human review or configured Codex auto-review. Avoid running concurrent supervisor instances against the same checkout.

## Persistent projects and budgets

A `milestone` object enables a persistent project. The overarching `objective` remains in thread context while the native goal tracks the current milestone. Completion runs checks and pauses for human review, retaining the same thread, worker and TUI. Configurations without a milestone retain one-task completion behavior.

```sh
agentic-supervisor budget RUN_ID --add-tokens 80000 --add-seconds 7200
agentic-supervisor milestone RUN_ID implementation --objective "Implement the reviewed design and run its acceptance checks."
agentic-supervisor resume RUN_ID
```

These changes require a confirmed paused run. Budget extensions are explicit, additive, and recorded; they never reset cumulative usage. Status reports cumulative and current-milestone usage. Time limits count active execution, including worker downtime while active; review pauses do not consume time. Resolve uncertain operations before resuming. Use `pause` for overnight breaks; `stop` ends the run.

`initial_task_file` supplies a complete initial task (UTF-8, at most 128 KiB) alongside the overarching objective. `artifact_paths` grants narrow external directories through native writable roots. Create those directories first and keep them separate from the private runtime/credentials. `writable_paths` remains restricted to paths inside the checkout. The default sandbox is `workspace-write`; `read-only` is optional. Select an available model and reasoning effort using `doctor --config` before launch; the detached runtime does not inherit the VS Code model selection.

## Runtime data and recovery

The runtime root contains a private SQLite database, a private Codex home, Unix sockets, process logs, and per-run directories. Each run records a snapshot of its configuration, Git revision and dirty state, native thread and turn IDs, normalized events, control requests, check attempts, token usage, and artifact hashes. Per-run environment files are mode `0600`; values for explicitly allowlisted environment variables are not embedded in tmux command lines or database snapshots. Keep the runtime root private because it also holds credentials and process logs. Generated files stay under the runtime root; never commit credentials, event logs, databases, or experiment output.

If the worker exits, use `agentic-supervisor recover RUN_ID` to reconnect it to the existing App Server and thread. If the App Server stopped, recovery restarts that owned process against the private Codex home and verifies the stored thread before continuing; the native TUI pane is restarted to attach to the new process. Recovery reconciles saved events and controls before continuing. A command interrupted during a crash can have an unknown outcome; inspect its artifacts and resolve the attempt explicitly instead of blindly repeating it. `resolve` records the operator's outcome and evidence.

M0 supports recovery from SSH disconnection and controlled process interruption. It does not configure reboot autostart, guarantee exactly-once execution of arbitrary external commands, recover a machine failure, or turn a thread resume into proof that an experiment completed. Native token budgets and supervisor elapsed-time limits are operational bounds, not exact billing caps. Authentication expiry, account limits, approval requests, and the experimental App Server protocol can pause progress.

## Development

Run unit tests with `python3 -m unittest discover -s tests/unit -v`. Runtime and live Codex tests are opt-in; set `AGENTIC_SUPERVISOR_TEST_ROOT` to a fresh directory beneath the runtime root before running them. See [architecture](docs/architecture.md), [operations](docs/operations.md), and [repository guidance](AGENTS.md).

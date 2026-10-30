# Operations

## Installation and executable selection

Use Python 3.10+, tmux and a validated standalone Codex CLI. No sudo, Node upgrade, containers or system services are required. Keep the old CLI release for rollback. Project `codex_executable` pins an absolute executable; global `--codex PATH` overrides it at launch. `AGENTIC_SUPERVISOR_CODEX` overrides PATH discovery when no explicit path is selected. Existing runs retain their resolved executable for recovery.

Choose a short private runtime root outside the target checkout and artifact directories. Authenticate once with `agentic-supervisor login`. Never put credentials inside writable project artifacts.

## Launch and reconnect

```sh
source .runtime/agentic-supervisor/bin/activate
export AGENTIC_SUPERVISOR_HOME=/path/to/private-runtime
agentic-supervisor doctor --config /path/to/project.json
agentic-supervisor start /path/to/project.json
agentic-supervisor list
TERM=screen-256color agentic-supervisor attach RUN_ID
```

`start` prints a run ID after the worker and native TUI are attached and the task is submitted. Detach using **Ctrl-b, then d**. Closing SSH disconnects the terminal client; the tmux-owned processes continue. After reconnecting, restore the environment above and use the same run ID. No reboot autostart is installed.

```sh
agentic-supervisor status RUN_ID
agentic-supervisor events RUN_ID --follow
agentic-supervisor send RUN_ID "Steer the current task without changing its scope."
agentic-supervisor send RUN_ID --queue "Apply this instruction at the next eligible turn."
agentic-supervisor pause RUN_ID
agentic-supervisor resume RUN_ID
```

`send` steers an active turn or starts a follow-up if idle. Paused instructions remain durable and are included, in FIFO order, in the explicitly resumed turn. `interrupt` cancels a turn without promising to pause its goal. `pause` requests cancellation and preserves context; confirm `status=paused`. This is not process suspension or rollback of already executed commands.

## Milestones and budgets

Set `milestone: {"name":"audit","objective":"Inspect and propose a design"}` to keep the project alive after its first goal. The top-level objective describes the project; the milestone objective bounds the currently authorized work. At milestone completion, configured checks run once per milestone generation, artifacts are hashed, and the project pauses for review. The same thread and native TUI remain available.

After reviewing findings, explicitly assign the next milestone and budget, then resume:

```sh
agentic-supervisor budget RUN_ID --add-tokens 80000 --add-seconds 7200
agentic-supervisor milestone RUN_ID implementation --objective "Implement the reviewed proposal and verify its acceptance criteria."
agentic-supervisor resume RUN_ID
```

Budget and milestone updates require confirmed pause and no uncertain operations. Budget changes add to cumulative limits and are recorded in the event ledger; they do not erase usage. A milestone transition records prior-stage usage and starts a new stage baseline. An exhausted budget pauses the project and requires an explicit extension. Active elapsed time includes worker outages while execution was active, but excludes confirmed review pauses. Native goal token accounting is reconciled from returned counters, whose reset behavior differs by release. Limits are operational bounds, not exact billing caps; enforcement can be delayed while the worker is unavailable.

Assign a new milestone generation even when revising the same milestone name. This explicitly authorizes new work and validation attempts. Configuration is snapshotted at run creation: editing the original JSON does not update a live run. Use durable controls for milestone and budget changes.

## Approvals and sandbox

Project JSON supports `approval_policy` (`on-request`, or the legacy `untrusted` where supported), `approvals_reviewer` (`user` or `auto_review`), and `sandbox_mode` (`workspace-write` by default, optionally `read-only`). Do not use `never` or full access. The project repository, run artifacts, and explicit external `artifact_paths` are writable under workspace-write. The private runtime and credentials must not overlap external artifact directories.

Codex automatic review decides eligible escalation requests; it can deny them. It does not widen writable roots or disable network restrictions. Routine allowed workspace operations need no review. Native human approval prompts remain in the TUI; the supervisor records operational events and never answers them. See [Codex auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review).

## Recovery and shutdown

```sh
agentic-supervisor recover RUN_ID
agentic-supervisor status RUN_ID
agentic-supervisor resolve RUN_ID ATTEMPT_ID --type attempt --outcome succeeded --evidence "Checked output and exit record"
agentic-supervisor resolve RUN_ID CONTROL_ID --type control --outcome not_applied --evidence "Confirmed no request was sent"
```

Recover only after the owned worker exits. Recovery reconnects to surviving App Server state or restarts its owned pane and resumes the recorded thread. It preserves pause intent, checks budgets, and never creates a replacement conversation. Dispatched controls, interrupted checks, and observed unfinished native effects after server loss remain uncertain until evidence resolves them. Resolution does not retry work. Recent paginated native history is used for reconciliation; complete conversation history remains in Codex.

Capacity, quota, authentication, denied approvals and protocol errors may halt progress. Inspect the TUI and status, correct the cause, then explicitly resume a paused run. No automatic retry of side-effectful operations is promised. Pending approvals do not terminate the project. Detached external experiments still require process/output inspection.

`agentic-supervisor stop RUN_ID` ends the owned runtime and retains its records. Use `pause` when you intend to continue the same project tomorrow. If the entire tmux server is lost, M0 recovery does not automatically reconstruct all processes; preserve the runtime and Codex home for manual recovery.

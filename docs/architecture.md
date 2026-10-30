# Architecture

Agentic Supervisor is a general-purpose process and workflow layer around native Codex. Codex owns reasoning, tools, conversation history, compaction and autonomous goal continuation. The supervisor owns durable operator controls, process ownership, provenance, execution budgets and conservative recovery. There is no separate conversation manager or required project adapter.

## Runtime and compatibility

One runtime root holds one live project run, its private Codex home, SQLite state, Unix sockets, logs and per-run artifacts. App Server, the Python worker and the native remote TUI occupy owned tmux windows. The worker and TUI connect to the same thread through Unix WebSockets; no TCP listener is exposed. tmux provides client-disconnect independence, not reboot autostart.

The explicit validated release list is 0.144.4 and 0.166.0. Unknown releases fail the version gate. Startup also probes authenticated model discovery, two clients and shared paused goal state without a model turn. Each run records the resolved executable and version; recovery uses that executable and checks that its version has not changed.

For 0.166.0, thread resume requests exclude full history and retrieve a bounded page of recent turn summaries through `thread/turns/list`. Full history remains native. Goal mutations distinguish explicit operator authorization from automatic lifecycle actions. The code does not infer token-counter resets from objective changes: it reads the acknowledged native baseline and calibrates the remaining native limit.

## Persistent project lifecycle

The project objective is stable across milestones. Initial instructions and an optional full task file enter the native thread. The current native goal describes the current milestone. When that goal completes and the turn ends, the worker runs configured checks once for that milestone generation, records result hashes and pauses for review. It retains the same thread, worker and TUI. A human assigns the next milestone and resumes; no new agent identity is created.

Without a milestone, original one-task terminal completion behavior remains supported. `stop` is terminal; confirmed pause is the normal overnight/review state. Reports and concise decision records complement native history when context compacts.

SQLite distinguishes desired state from observed state. Controls are recorded before dispatch and become accepted/confirmed or uncertain. Queued input is delivered in order at the next eligible turn, including an explicitly resumed turn. A lost acknowledgement never causes blind replay.

Budgets are cumulative token and active-time ceilings with explicit additive extensions. Stage baselines provide current-stage usage without clearing total usage. Confirmed pauses stop active-time accounting; worker downtime while active is counted. Budget changes and milestone assignments are atomic ledger events and require a paused run with no unresolved operations.

## Permissions and evidence

Sandbox, approval policy, reviewer, model and reasoning effort are configurable. Workspace-write includes the target checkout, run artifacts and narrow explicit external artifact directories. External artifact roots cannot contain private runtime state. Native auto-review is optional and does not remove sandbox restrictions. The supervisor never grants approvals itself.

Checks use argument arrays, native sandboxed `command/exec`, finite timeouts and attempt records written before dispatch. Git checkpoints record HEAD, changed paths and a streamed tracked-diff hash; they do not automatically commit application code. Project workflows remain responsible for durable source commits and experiment records.

Worker recovery reconciles the same thread. Server recovery also marks observed unfinished command/file/tool effects uncertain. Resolving a ledger record requires evidence and does not retry it. Native event IDs deduplicate canonical events; transient output gaps are reported. Arbitrary external process completion and exactly-once effects cannot be reconstructed from a resumed conversation alone.

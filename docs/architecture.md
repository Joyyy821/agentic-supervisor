# Architecture

Agentic Supervisor is a general-purpose process and workflow layer around native Codex. Codex owns reasoning, tools, conversation history, compaction and autonomous goal continuation. The supervisor owns durable operator controls, process ownership, provenance, token accounting, elapsed-time limits and conservative recovery. There is no separate conversation manager or required project adapter.

## Runtime and compatibility

One runtime root holds one live project run, its private Codex home, SQLite state, Unix sockets, logs and per-run artifacts. App Server, the Python worker and the native remote TUI occupy owned tmux windows. The worker and TUI connect to the same thread through Unix WebSockets; no TCP listener is exposed. tmux provides client-disconnect independence, not reboot autostart.

The explicit validated release list is 0.144.4 and 0.166.0. Unknown releases fail the version gate. Startup also probes authenticated model discovery, two clients and shared paused goal state without a model turn. Each run records the resolved executable and version; recovery uses that executable and checks that its version has not changed.

For 0.166.0, thread resume requests exclude full history and retrieve a bounded page of recent turn summaries through `thread/turns/list`. Full history remains native. Goal mutations distinguish explicit operator authorization from automatic lifecycle actions. The code does not infer token-counter resets from objective changes: it reads the acknowledged native baseline. New goals set `tokenBudget: null`; provider and context-window limits still apply.

## Persistent project lifecycle

The project objective is stable across milestones. Initial instructions and an optional full task file enter the native thread. The current native goal describes the current milestone. When that goal completes and the turn ends, the worker runs configured checks once for that milestone generation, records result hashes and pauses for review. It retains the same thread, worker and TUI. A human assigns the next milestone and resumes; no new agent identity is created.

Without a milestone, original one-task terminal completion behavior remains supported. `stop` is terminal; confirmed pause is the normal overnight/review state. Reports and concise decision records complement native history when context compacts.

SQLite distinguishes desired state from observed state. Controls are recorded before dispatch and become accepted/confirmed or uncertain. Queued input is delivered in order at the next eligible turn, including an explicitly resumed turn. A lost acknowledgement never causes blind replay.

Token use is cumulative and stage baselines report current-stage use. A configured token threshold is advisory for new runs; warnings are emitted when a multiple is crossed. Existing `token_budget` JSON is accepted as a warning threshold for new runs. Pre-upgrade snapshots keep their hard limit and are labeled legacy until explicitly migrated while paused. The elapsed-time ceiling remains enforced and can be extended explicitly. Confirmed pauses and provider usage waits stop active-time accounting; worker downtime while active is counted. Milestone assignments and extensions are atomic ledger events and require a paused run with no unresolved operations.

Optional provider usage recovery uses Codex 0.166.0's structured turn error, `usageLimited` goal status and `account/rateLimits/read`. Only backend-classified temporary rate/usage windows qualify. The worker stores the failed turn, reset time, next check and attempt count, then polls without model requests. `ordinaryUsageAllowed: true` is required to continue. Before a follow-up, it checks the same thread's recent turns, native status and approval flags, goal, manual pause/review intent and unresolved side effects. The worker records dispatch before the native call; a lost acknowledgement or worker crash during dispatch becomes an uncertain control requiring evidence, never an automatic replay. A lifecycle lock serializes supervisor pause/stop with dispatch. Native TUI actions are independent clients; a simultaneous TUI mutation cannot be made atomic with the supervisor's check. No earned reset credit is consumed and Luna reserve is disabled.

## Permissions and evidence

Sandbox, approval policy, reviewer, model and reasoning effort are configurable. Workspace-write includes the target checkout, run artifacts and narrow explicit external artifact directories. External artifact roots cannot contain private runtime state. Native auto-review is optional and does not remove sandbox restrictions. The supervisor never grants approvals itself.

Checks use argument arrays, native sandboxed `command/exec`, finite timeouts and attempt records written before dispatch. Git checkpoints record HEAD, changed paths and a streamed tracked-diff hash; they do not automatically commit application code. Project workflows remain responsible for durable source commits and experiment records.

Worker recovery reconciles the same thread. Server recovery also marks observed unfinished command/file/tool effects uncertain. Resolving a ledger record requires evidence and does not retry it. Native event IDs deduplicate canonical events; transient output gaps are reported. Arbitrary external process completion and exactly-once effects cannot be reconstructed from a resumed conversation alone.

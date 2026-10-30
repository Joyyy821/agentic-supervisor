# Architecture

M0 uses Codex as the agent runtime and keeps the supervisor small. Codex owns the conversation, tools, thread history, and goal continuation. The supervisor owns a run's configuration snapshot, bounded controls, lifecycle, operational events, check attempts, and recovery decisions.

## Components

- **CLI:** validates configuration and submits durable operator requests.
- **SQLite store:** records runs, control requests, normalized Codex events, and command attempts.
- **Codex App Server:** owns native Codex threads and serves clients through a private Unix socket.
- **Supervisor worker:** maintains a protocol subscription, processes queued controls, records events, and enforces elapsed-time budgets.
- **Native Codex TUI:** runs in the same owned tmux session. It is the user interface for conversation, approvals, and interactive goal changes.
- **tmux:** detaches the run from the SSH/VS Code client and provides the reconnectable TUI.

Each supervisor instance has one live run at a time. A runtime directory contains `state.sqlite3`, `codex-home/`, `sockets/`, `logs/`, and `runs/<run-id>/`. Runtime paths are configured independently from the target checkout. Directory and credential permissions are owner-only.

The worker and TUI use separate App Server WebSocket connections over a private Unix socket. Both use the same thread and goal store. The worker uses a small standard-library RFC 6455 client because the installed `codex app-server proxy --sock` sends raw JSONL to a listener that requires a WebSocket upgrade. The proxy therefore cannot bridge this installed transport. Multi-client goal state is verified by a bounded contract test. Approval routing for turns started by the worker and interactive TUI attachment still require an authenticated native interaction test; keep the TUI attached and do not assume unattended approval handling is validated.

## State and recovery

Run state distinguishes requested `desired_status` from observed `status`; a CLI request is not considered applied until its effect is confirmed. Each control request receives a UUID and moves through `queued`, `dispatched`, and a confirmed or `outcome_unknown` result. Controls are not automatically resent after an ambiguous disconnect.

Codex history remains canonical. SQLite stores thread/turn/item IDs, selected event fields, timestamps, and summaries sufficient for status and recovery. Event IDs deduplicate replayed notifications. Gaps are reported; the supervisor does not claim to reconstruct every streamed token.

Checks and configured operations receive an attempt record before dispatch. A restart with an unresolved attempt requires evidence and an explicit operator resolution. Worker recovery reattaches to the same App Server and thread; App Server recovery restarts only the owned server process, then verifies the stored thread before continuing. Both paths mark dispatched controls and in-flight check attempts as uncertain and never replay them. At turn boundaries, the worker records the Git revision, changed paths, and a streamed hash of tracked diffs; native history remains the source for conversation details.

## Protocol and permissions

The App Server protocol is versioned and experimental. The runtime currently accepts only the observed `codex-cli 0.144.4` and makes a no-turn probe of two clients, thread creation, goal control, and shared goal reads before starting a run. The separate contract test exercises those calls. This is a narrow compatibility gate, not complete protocol-schema validation; re-audit and rerun native tests before accepting another Codex version.

Threads start with workspace-write sandboxing and an on-request approval policy. The target repository and configured artifacts are explicit. No TCP listener, unrestricted execution mode, or automatic approval is enabled. App Server's `command/exec` interface runs named configured checks with argument arrays and timeouts.

## Data flow

1. Validate the repository, configuration, runtime root, and installed capabilities.
2. Persist the run and its configuration/provenance snapshot.
3. Start the owned App Server, create and record a thread, and attach the worker and native TUI.
4. Set the native goal and submit the initial objective only after startup reconciliation succeeds.
5. Persist controls before sending them. Record native events as they arrive and reconcile them after disconnects.
6. On goal completion, run configured checks once and store results and artifact hashes.

The optional Agentic MAPF application can configure existing project commands without introducing Isaac Sim or ROS dependencies in this repository.

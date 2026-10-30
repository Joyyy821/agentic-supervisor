# Repository guidance

## Purpose and boundaries

Agentic Supervisor is a standalone, general-purpose Codex lifecycle and workflow tool. Core code may depend on Python's standard library, Codex CLI/App Server, and tmux. It must not require or import any target project's libraries. Keep project objectives, checks, paths, and environment settings in user configuration.

M0 tracked changes belong only in this repository. Put runtime state, logs, credentials, temporary repositories, protocol schemas, and test outputs beneath a dedicated runtime root such as `/data/yangjiao/agentic-supervisor/`, or beneath ignored `.runtime/`. Never write to another target repository during M0 tests. Never commit generated runtime data, credentials, experiment output, or large artifacts.

Agentic MAPF is one optional application. Keep its assumptions and `AMAPF-M*` milestones in `docs/applications/`; do not add robotics concepts to core modules or required dependencies.

## Implementation conventions

- Prefer small Python 3.10+ standard-library modules and typed, explicit interfaces.
- Treat the installed Codex CLI and its generated App Server schemas as the protocol contract. Check protocol compatibility before launching a run.
- Keep Codex authoritative for conversation content. Store only operational state and references in the supervisor database.
- Separate desired lifecycle state from confirmed runtime state. Make control delivery and command attempts durable before dispatch.
- On uncertain delivery or command completion, preserve `outcome_unknown`; inspect evidence before retrying. Never claim arbitrary external operations are exactly-once.
- Use argument arrays for configured commands, validate working directories, enforce timeouts, and retain output and provenance.
- Reuse maintained third-party tools when needed. Avoid premature plugin frameworks and custom conversation or TUI layers.

## Safety and reproducibility

- Do not use unrestricted Codex execution or automatically approve commands. Keep approval decisions in the native TUI.
- Keep credentials in a private runtime Codex home, outside model-writable output folders. Do not log or snapshot secret values.
- Keep runtime environment files and logs private. Avoid command-line arguments for secret values; use only explicitly allowlisted environment variables.
- Bind App Server only to a private Unix socket. Do not expose unauthenticated TCP or publish credentials through a service.
- Use dedicated tmux sockets and terminate only processes whose ownership is recorded by this runtime.
- Record the Codex version, repository revision and dirty state, objective, effective settings, commands, exit codes, and artifact hashes.
- Never blindly repeat an interrupted command or experiment. A resumed conversation is not evidence that a process completed.
- Do not use sudo, destructive cleanup, Docker mutations, or broad process-name kills.

## Validation

Run unit tests with `python3 -m unittest discover -s tests/unit -v`. Put live-runtime tests behind an explicit opt-in and use a newly initialized nonrobotics repository. Store all generated test data under a unique directory inside the configured runtime test root. Verify that `mas-vln` remains unchanged.

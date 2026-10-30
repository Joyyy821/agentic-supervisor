"""Owned detached Codex runtime and supervisor worker."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .checks import run_check
from .codex import VALIDATED_VERSIONS, CodexError, RpcClient, codex_executable, version
from .config import RunConfig
from .store import ACTIVE_STATUSES, Store


class RuntimeErrorMessage(RuntimeError):
    pass


class WorkerAlreadyRunning(RuntimeErrorMessage):
    pass


def acquire_worker_lock(runtime_root: Path) -> int:
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root.chmod(0o700)
    path = runtime_root / "worker.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise WorkerAlreadyRunning("a supervisor worker already owns this runtime root") from exc
    return fd


def git_metadata(repository: Path) -> tuple[str, bool]:
    head = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(repository), "status", "--porcelain"],
                           check=True, capture_output=True, text=True, timeout=5).stdout
    return head, bool(dirty.strip())


def git_checkpoint(repository: Path, timeout: float = 60) -> dict[str, Any]:
    head = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True, timeout=5).stdout.strip()
    status_text = subprocess.run(["git", "-C", str(repository), "status", "--porcelain=v1",
                                  "--untracked-files=all"], check=True, capture_output=True,
                                text=True, timeout=10).stdout
    proc = subprocess.Popen(["git", "-C", str(repository), "diff", "--binary", "HEAD", "--"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    digest = hashlib.sha256()
    size = 0
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise TimeoutError("git checkpoint exceeded its time limit")
            readable, _, _ = select.select([proc.stdout], [], [], min(0.5, remaining))
            if readable:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            elif proc.poll() is not None:
                break
        if proc.wait(timeout=2) != 0:
            raise RuntimeError("git diff failed while creating a checkpoint")
    finally:
        proc.stdout.close()
    return {"head": head, "clean": not bool(status_text.strip()),
            "changed_paths": [line[3:] for line in status_text.splitlines()],
            "tracked_diff_sha256": digest.hexdigest(), "tracked_diff_bytes": size}


def _environment(config: RunConfig, codex_home: Path) -> dict[str, str]:
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM"}
    allowed.update(config.environment_allowlist)
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["CODEX_HOME"] = str(codex_home)
    source_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = source_root
    return env


class Runtime:
    def __init__(self, root: Path, codex: str | None = None):
        self.root = root.expanduser().resolve()
        self.store = Store(self.root)
        self.codex = codex_executable(codex)
        self.codex_home = self.root / "codex-home"
        self.socket_root = self.root / "sockets"
        self.tmux_socket = self.root / "supervisor.tmux"

    def app_socket(self, run_id: str) -> Path:
        return self.socket_root / f"{run_id.replace('-', '')[:16]}.sock"

    def _prepare(self, run_id: str) -> tuple[Path, Path, Path]:
        for path in (self.root, self.codex_home, self.socket_root, self.root / "logs"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        (self.root / "runs" / run_id).mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path = self.codex_home / "config.toml"
        if not config_path.exists():
            config_path.write_text(
                'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n\n[features]\ngoals = true\n',
                encoding="utf-8",
            )
            config_path.chmod(0o600)
        socket = self.app_socket(run_id)
        if len(os.fsencode(socket)) >= 100:
            raise RuntimeErrorMessage("runtime root is too deep for the host Unix socket path limit; choose a shorter root")
        return (socket, self.root / "runs" / run_id, self.root / "logs")

    def _sandbox_policy(self, run_id: str, config: RunConfig) -> dict[str, Any]:
        if config.sandbox_mode == "read-only":
            return {"type": "readOnly", "networkAccess": False}
        roots = [config.repository, *config.writable_paths, *config.artifact_paths,
                 self.root / "runs" / run_id / "artifacts"]
        return {"type": "workspaceWrite", "writableRoots": list(dict.fromkeys(str(p) for p in roots))}

    async def protocol_probe(self, config: RunConfig | None = None) -> dict[str, Any]:
        """Probe two-client Unix WebSocket transport without sending a model turn."""
        active = next((row for row in self.store.list_runs()
                       if row["status"] in ACTIVE_STATUSES and row["thread_id"]), None)
        if active:
            socket = self.app_socket(active["id"])
            clients = [RpcClient(self.codex, socket, self.codex_home),
                       RpcClient(self.codex, socket, self.codex_home)]
            try:
                for i, client in enumerate(clients):
                    await client.connect(f"agentic_supervisor_doctor_{i}")
                models = await clients[0].call("model/list", {})
                goal = await clients[1].call("thread/goal/get", {"threadId": active["thread_id"]})
                return {"transport": "unix-websocket", "clients": 2,
                        "model_count": len(models.get("data", [])),
                        "models": [{"id": m["id"], "efforts": m.get("supportedReasoningEfforts", [])} for m in models.get("data", [])],
                        "shared_goal_status": (goal.get("goal") or {}).get("status"),
                        "reused_active_server": True}
            finally:
                for client in clients:
                    await client.close()

        probe_id = uuid.uuid4().hex[:8]
        probe_root = self.root / "checks" / probe_id
        probe_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        probe_root.chmod(0o700)
        probe_socket = probe_root / "s.sock"
        if len(os.fsencode(probe_socket)) >= 100:
            raise RuntimeErrorMessage("runtime root is too deep for the host Unix socket path limit")
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL"}}
        env["CODEX_HOME"] = str(self.codex_home)
        self.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.codex_home.chmod(0o700)
        log_path = probe_root / "app-server.log"
        with log_path.open("wb") as log:
            argv = [self.codex, "app-server", "--listen", f"unix://{probe_socket}", "--enable", "goals"]
            if config:
                argv += ["-c", f"approval_policy={json.dumps(config.approval_policy)}",
                         "-c", f"sandbox_mode={json.dumps(config.sandbox_mode)}"]
                if config.approvals_reviewer:
                    argv += ["-c", f"approvals_reviewer={json.dumps(config.approvals_reviewer)}"]
            server = await asyncio.create_subprocess_exec(
                *argv,
                stdout=log, stderr=log, env=env,
            )
            clients: list[RpcClient] = []
            thread_id: str | None = None
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and not probe_socket.exists():
                    if server.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
                if not probe_socket.exists():
                    raise RuntimeErrorMessage(f"App Server probe did not bind its socket; see {log_path}")
                for i in range(2):
                    client = RpcClient(self.codex, probe_socket, self.codex_home)
                    await client.connect(f"agentic_supervisor_doctor_{i}")
                    clients.append(client)
                models = await clients[0].call("model/list", {}, timeout=20)
                effective = (await clients[0].call("config/read", {"includeLayers": False}))["config"]
                if config and config.model:
                    model = next((m for m in models.get("data", []) if m["id"] == config.model), None)
                    if not model or (config.reasoning_effort and config.reasoning_effort not in
                                     {e["reasoningEffort"] for e in model.get("supportedReasoningEfforts", [])}):
                        raise RuntimeErrorMessage("requested model/reasoning effort is not in the authenticated catalog")
                workspace = probe_root / "workspace"
                workspace.mkdir(mode=0o700)
                created = await clients[0].call("thread/start", {
                    "cwd": str(workspace), "sandbox": config.sandbox_mode if config else "read-only",
                    "approvalPolicy": config.approval_policy if config else "on-request",
                    "model": config.model if config else None,
                    "config": {"model_reasoning_effort": config.reasoning_effort} if config and config.reasoning_effort else {},
                }, timeout=20)
                thread_id = created["thread"]["id"]
                await clients[0].call("thread/goal/set", {
                    "threadId": thread_id, "objective": "Transport health check; remain paused.",
                    "status": "paused", "tokenBudget": 1, "origin": "user",
                }, timeout=20)
                shared_goal = await clients[1].call("thread/goal/get", {"threadId": thread_id}, timeout=20)
                goal_status = (shared_goal.get("goal") or {}).get("status")
                if goal_status != "paused":
                    raise RuntimeErrorMessage(f"second App Server client did not observe paused goal: {goal_status}")
                await clients[0].call("thread/goal/clear", {"threadId": thread_id}, timeout=20)
                await clients[0].call("thread/delete", {"threadId": thread_id}, timeout=20)
                thread_id = None
                return {"transport": "unix-websocket", "clients": 2,
                        "model_count": len(models.get("data", [])),
                        "models": [{"id": m["id"], "efforts": m.get("supportedReasoningEfforts", [])} for m in models.get("data", [])], "shared_goal_status": goal_status,
                        "reused_active_server": False, "model_turns_sent": 0,
                        "effective_config": {k: effective.get(k) for k in ("approval_policy", "approvals_reviewer", "sandbox_mode", "web_search")},
                        "thread_settings": {k: created.get(k) for k in ("model", "reasoningEffort", "approvalPolicy", "approvalsReviewer", "sandbox")}}
            finally:
                if thread_id and clients:
                    try:
                        await clients[0].call("thread/goal/clear", {"threadId": thread_id}, timeout=5)
                        await clients[0].call("thread/delete", {"threadId": thread_id}, timeout=5)
                    except Exception:
                        pass
                for client in clients:
                    await client.close()
                if server.returncode is None:
                    server.terminate()
                    try:
                        await asyncio.wait_for(server.wait(), 5)
                    except asyncio.TimeoutError:
                        server.kill()
                        await server.wait()

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        binary = shutil.which("tmux")
        if not binary:
            raise RuntimeErrorMessage("tmux is required but was not found")
        result = subprocess.run([binary, "-S", str(self.tmux_socket), *args], capture_output=True,
                                text=True, timeout=20)
        if check and result.returncode != 0:
            raise RuntimeErrorMessage(result.stderr.strip() or "tmux command failed")
        return result

    def _server_command(self, run_id: str, socket: Path, config: RunConfig, log_dir: Path) -> str:
        env = _environment(config, self.codex_home)
        argv = [self.codex, "app-server", "--listen", f"unix://{socket}", "--enable", "goals",
                "-c", f"approval_policy={json.dumps(config.approval_policy)}",
                "-c", f"sandbox_mode={json.dumps(config.sandbox_mode)}"]
        if config.approvals_reviewer:
            argv += ["-c", f"approvals_reviewer={json.dumps(config.approvals_reviewer)}"]
        return self._owned_command(run_id, env, argv, log_dir / f"{run_id}-app-server.log")

    def _worker_command(self, run_id: str, config: RunConfig, log_dir: Path) -> str:
        env = _environment(config, self.codex_home)
        argv = [sys.executable, "-m", "agentic_supervisor.worker", "--runtime-root", str(self.root), run_id]
        return self._owned_command(run_id, env, argv, log_dir / f"{run_id}-worker.log")

    def _tui_command(self, run_id: str, thread_id: str, config: RunConfig,
                     socket: Path) -> str:
        env = _environment(config, self.codex_home)
        env["TERM"] = "screen-256color"
        argv = [self.codex, "--cd", str(config.repository), "--remote", f"unix://{socket}", "--no-alt-screen",
                "resume", thread_id]
        return self._owned_command(run_id, env, argv, None, "tui.env")

    def _owned_command(self, run_id: str, env: dict[str, str], argv: list[str],
                       log_path: Path | None, env_name: str = "runtime.env") -> str:
        env_path = self.root / "runs" / run_id / env_name
        env_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded_env = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in sorted(env.items()))
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded_env)
        env_path.chmod(0o600)
        command = ["/usr/bin/env", "-i", "/bin/sh", "-c",
                   'set -a; . "$1"; set +a; shift; exec "$@"',
                   "agentic-supervisor", str(env_path), *argv]
        redirected = f" >>{shlex.quote(str(log_path))} 2>&1" if log_path else ""
        return "exec " + shlex.join(command) + redirected

    async def connect_when_ready(self, socket: Path, timeout: float = 20) -> RpcClient:
        end = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < end:
            if socket.exists():
                client = RpcClient(self.codex, socket, self.codex_home)
                try:
                    await client.connect("agentic_supervisor")
                    return client
                except Exception as exc:
                    last = exc
                    await client.close()
            await asyncio.sleep(0.2)
        raise RuntimeErrorMessage(f"App Server did not become ready: {last or 'Unix socket not created'}")

    async def wait_for_worker(self, run_id: str, session: str, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.store.latest_event(run_id, "supervisor/worker_attached"):
                return
            worker = self.tmux("list-panes", "-t", f"{session}:supervisor", "-F", "#{pane_dead}", check=False)
            if worker.returncode or worker.stdout.strip() == "1":
                raise RuntimeErrorMessage("supervisor worker exited before attaching to the native thread")
            await asyncio.sleep(0.2)
        raise RuntimeErrorMessage("supervisor worker did not attach before the startup deadline")

    async def wait_for_tui(self, session: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pane = self.tmux("list-panes", "-t", f"{session}:codex", "-F",
                             "#{pane_dead}:#{pane_current_command}", check=False)
            if pane.returncode == 0 and pane.stdout.strip():
                state, _, command = pane.stdout.strip().partition(":")
                if state == "1":
                    raise RuntimeErrorMessage("native Codex TUI exited during startup")
                if state == "0" and command == "codex":
                    await asyncio.sleep(0.75)
                    stable = self.tmux("list-panes", "-t", f"{session}:codex", "-F",
                                       "#{pane_dead}:#{pane_current_command}", check=False)
                    if stable.returncode == 0 and stable.stdout.strip() == "0:codex":
                        return
            await asyncio.sleep(0.2)
        raise RuntimeErrorMessage("native Codex TUI process did not remain attached before the startup deadline")

    @staticmethod
    def instructions(run_id: str, config: RunConfig, run_dir: Path) -> str:
        contexts = "\n".join(str(p) for p in config.context_files) or "(none)"
        artifacts = "\n".join([str(run_dir / "artifacts"), *(str(p) for p in (*config.writable_paths, *config.artifact_paths))])
        return (
            "You are a general-purpose development and research agent. Follow the target repository's "
            "AGENTS.md and the user's objective. Work in small checkpoints, preserve evidence, and run "
            "configured checks before claiming completion. Do not assume access to unrelated repositories. "
            f"\nPersistent project objective: {config.objective}\nSupervisor run ID: {run_id}\nContext files:\n{contexts}\n"
            f"Configured writable artifact paths:\n{artifacts}\n"
            "Do not claim an interrupted command completed without checking its outputs."
        )

    @classmethod
    def initial_prompt(cls, run_id: str, config: RunConfig, run_dir: Path) -> str:
        checks = "\n".join(f"- {check.name}: {shlex.join(check.command)} (timeout {check.timeout_seconds}s)"
                           for check in config.checks) or "(no named checks configured)"
        return (
            f"Pursue this objective until its stated verification conditions are met:\n\n{config.objective}\n\n"
            f"Supervisor run: {run_id}. Record short progress checkpoints in `{run_dir / 'artifacts' / 'progress.md'}`.\n"
            f"Configured checks available to the supervisor:\n{checks}\n" +
            ("Full initial task:\n" + config.initial_task_file.read_text(encoding="utf-8") + "\n" if config.initial_task_file else "") +
            "Follow project instructions. Ask for approval when required by the configured policy. "
            "Do not report completion until requested verification has been attempted."
        )

    async def start(self, config: RunConfig) -> str:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(row["status"] in ACTIVE_STATUSES for row in self.store.list_runs()):
            raise RuntimeErrorMessage("this runtime root already has a live or blocked run; inspect or recover it first")
        self.codex = codex_executable(config.codex_executable or self.codex)
        version_text = await version(self.codex)
        if version_text not in VALIDATED_VERSIONS:
            raise RuntimeErrorMessage(
                f"Codex version {version_text!r} is not validated; validated versions: {sorted(VALIDATED_VERSIONS)}"
            )
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        identity = subprocess.run([self.codex, "login", "status"], capture_output=True,
                                  text=True, timeout=10, env=env)
        if identity.returncode != 0:
            raise RuntimeErrorMessage("private Codex home is not authenticated; run agentic-supervisor login")

        try:
            capability = await self.protocol_probe(config)
        except Exception as exc:
            raise RuntimeErrorMessage(f"installed App Server capability probe failed: {exc}") from exc
        if capability.get("clients") != 2 or capability.get("shared_goal_status") != "paused":
            raise RuntimeErrorMessage("installed App Server did not pass the validated two-client goal probe")

        git_head, git_dirty = git_metadata(config.repository)
        planned_socket = self.socket_root / "0123456789abcdef.sock"
        if len(os.fsencode(planned_socket)) >= 100:
            raise RuntimeErrorMessage("runtime root is too deep for the host Unix socket path limit; choose a shorter root")
        run_id = self.store.create_run(config, version_text, git_head, git_dirty)
        self.store.update_run(run_id, codex_executable=self.codex)
        if config.initial_task_file:
            self.store.add_event(run_id, "supervisor/task_input", {
                "path": str(config.initial_task_file),
                "sha256": hashlib.sha256(config.initial_task_file.read_bytes()).hexdigest(),
            })
        session = self.store.get_run(run_id)["tmux_session"]
        try:
            socket, run_dir, log_dir = self._prepare(run_id)
            self.tmux("new-session", "-d", "-s", session, "-n", "app-server",
                      self._server_command(run_id, socket, config, log_dir))
            self.tmux("set-window-option", "-t", f"{session}:app-server", "remain-on-exit", "on")
            client = await self.connect_when_ready(socket)
            try:
                result = await client.call("thread/start", {
                    "cwd": str(config.repository),
                    "sandbox": config.sandbox_mode,
                    "approvalPolicy": config.approval_policy,
                    "baseInstructions": self.instructions(run_id, config, run_dir),
                    "model": config.model,
                    "config": {"model_reasoning_effort": config.reasoning_effort} if config.reasoning_effort else {},
                }, timeout=45)
                thread_id = result.get("thread", {}).get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise RuntimeErrorMessage("Codex did not return a thread ID")
                self.store.update_run(run_id, thread_id=thread_id)
                self.store.add_event(run_id, "supervisor/effective_settings", {
                    k: result.get(k) for k in ("model", "reasoningEffort", "approvalPolicy", "approvalsReviewer", "sandbox")
                })
                if config.approvals_reviewer and version_text == "codex-cli 0.166.0" and result.get("approvalsReviewer") != config.approvals_reviewer:
                    raise RuntimeErrorMessage("App Server did not select the requested approval reviewer")
                if config.model and result.get("model") != config.model:
                    raise RuntimeErrorMessage("App Server did not select the requested model")
                if config.reasoning_effort and result.get("reasoningEffort") != config.reasoning_effort:
                    raise RuntimeErrorMessage("App Server did not select the requested reasoning effort")

                # A newly started App Server thread has no persisted rollout yet, so
                # thread/resume fails with "no rollout found" until its first turn.
                # Establish a bounded, read-only bootstrap turn before starting the
                # worker and native TUI; both then attach to the same persisted thread
                # before the actual objective begins.
                bootstrap_budget = config.token_budget
                await client.call("thread/goal/set", {
                    "threadId": thread_id,
                    "objective": "Session setup only: respond READY without using tools.",
                    "status": "paused",
                    "tokenBudget": bootstrap_budget, "origin": "user",
                })
                bootstrap = await client.call("turn/start", {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text":
                               "Reply with exactly READY. This is a session setup turn. "
                               "Do not use tools, access files, or change anything."}],
                    "cwd": str(config.repository),
                    "model": config.model,
                    "effort": config.reasoning_effort,
                    "sandboxPolicy": {"type": "readOnly"},
                }, timeout=45)
                bootstrap_turn_id = bootstrap.get("turn", {}).get("id")
                if not isinstance(bootstrap_turn_id, str) or not bootstrap_turn_id:
                    raise RuntimeErrorMessage("Codex did not return a session bootstrap turn ID")
                deadline = time.monotonic() + 180
                bootstrap_status = None
                while time.monotonic() < deadline:
                    resumed = await client.call("thread/resume", {
                        "threadId": thread_id, "cwd": str(config.repository),
                    }, timeout=15)
                    turns = resumed.get("thread", {}).get("turns", [])
                    turn = next((item for item in reversed(turns)
                                 if isinstance(item, dict) and item.get("id") == bootstrap_turn_id), None)
                    bootstrap_status = turn.get("status") if turn else None
                    if bootstrap_status == "completed":
                        break
                    if bootstrap_status in {"failed", "interrupted"}:
                        raise RuntimeErrorMessage(
                            f"Codex session bootstrap ended as {bootstrap_status}"
                        )
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeErrorMessage("Codex session bootstrap did not finish within 180 seconds")

                goal_state = await client.call("thread/goal/get", {"threadId": thread_id})
                bootstrap_tokens = (goal_state.get("goal") or {}).get("tokensUsed", 0)
                if isinstance(bootstrap_tokens, int) and bootstrap_tokens > 0:
                    self.store.observe_token_usage(run_id, bootstrap_tokens)
                await client.call("thread/goal/set", {"threadId": thread_id, "status": "paused"})
                self.store.add_event(run_id, "supervisor/session_initialized", {
                    "thread_id": thread_id,
                    "bootstrap_turn_id": bootstrap_turn_id,
                    "bootstrap_tokens": bootstrap_tokens if isinstance(bootstrap_tokens, int) else None,
                })
            finally:
                await client.close()

            self.tmux("new-window", "-t", session, "-n", "supervisor",
                      self._worker_command(run_id, config, log_dir))
            self.tmux("set-window-option", "-t", f"{session}:supervisor", "remain-on-exit", "on")
            await self.wait_for_worker(run_id, session)
            self.tmux("new-window", "-t", session, "-n", "codex",
                      self._tui_command(run_id, thread_id, config, socket))
            self.tmux("set-window-option", "-t", f"{session}:codex", "remain-on-exit", "on")
            await self.wait_for_tui(session)
            client = await self.connect_when_ready(socket, 10)
            try:
                started_at = time.time()
                self.store.update_run(run_id, status="active", started_at=started_at, last_error=None)
                self.store.add_event(run_id, "supervisor/run_started",
                                     {"thread_id": thread_id, "codex_version": version_text})
                bootstrap_tokens = self.store.get_run(run_id)["tokens_used"]
                remaining_token_budget = config.token_budget - bootstrap_tokens
                if remaining_token_budget < 1:
                    raise RuntimeErrorMessage(
                        "session bootstrap consumed the configured token budget; increase token_budget"
                    )
                await self.activate(client, run_id, config, self.initial_prompt(run_id, config, run_dir))

            finally:
                await client.close()

            return run_id
        except Exception as exc:
            self.store.update_run(run_id, status="blocked", desired_status="blocked", last_error=str(exc))
            self.store.add_event(run_id, "supervisor/start_failed", {"error": str(exc)})
            raise

    @staticmethod
    def goal_objective(row: Any) -> str:
        if row["milestone_name"]:
            return (f"Milestone {row['milestone_name']}: {row['milestone_objective']}\n"
                    "Complete only this milestone, then report findings for human review. "
                    "The overarching project continues after review; do not start a later milestone.")
        return row["objective"]

    async def activate(self, client: RpcClient, run_id: str, config: RunConfig, prompt: str) -> str:
        row = self.store.get_run(run_id)
        if self.store.has_uncertain_work(run_id):
            raise ValueError("resolve uncertain operations before resuming")
        goal = (await client.call("thread/goal/get", {"threadId": row["thread_id"]})).get("goal") or {}
        if isinstance(goal.get("tokensUsed"), int):
            self.store.observe_token_usage(run_id, goal["tokensUsed"])
        row = self.store.get_run(run_id)
        remaining = row["token_budget"] - row["tokens_used"]
        if remaining <= 0 or self.store.elapsed(row) >= row["time_budget_seconds"]:
            raise ValueError("execution budget exhausted; extend it while paused before resuming")
        objective = self.goal_objective(row)
        native_used = goal.get("tokensUsed", 0)
        updated = await client.call("thread/goal/set", {
            "threadId": row["thread_id"], "objective": objective, "status": "paused",
            "tokenBudget": native_used + remaining, "origin": "user",
        })
        # Counter reset semantics differ by release. Use the acknowledged native
        # baseline, never infer a reset just from an objective change.
        actual_used = (updated.get("goal") or {}).get("tokensUsed")
        if not isinstance(actual_used, int):
            actual_used = ((await client.call("thread/goal/get", {"threadId": row["thread_id"]})).get("goal") or {}).get("tokensUsed", native_used)
        self.store.update_run(run_id, native_tokens_seen=actual_used)
        if actual_used != native_used:
            await client.call("thread/goal/set", {"threadId": row["thread_id"],
                "tokenBudget": actual_used + remaining, "status": "paused", "origin": "user"})
        turn = await client.call("turn/start", {
            "threadId": row["thread_id"], "cwd": str(config.repository),
            "input": [{"type": "text", "text": prompt + "\nCurrent authorized goal:\n" + objective}],
            "model": config.model, "effort": config.reasoning_effort,
            "approvalPolicy": config.approval_policy,
            "sandboxPolicy": self._sandbox_policy(run_id, config),
        }, timeout=45)
        await client.call("thread/goal/set", {"threadId": row["thread_id"], "status": "active", "origin": "user"})
        self.store.update_run(run_id, status="active", desired_status="active", last_error=None,
                              milestone_status="active" if row["milestone_name"] else None)
        return turn.get("turn", {}).get("id") or "pending"

    def submit(self, run_id: str, kind: str, payload: str = "") -> str:
        row = self.store.get_run(run_id)
        terminal = {"complete", "validation_failed", "stopped", "failed"}
        if row["status"] in terminal and kind == "stop":
            control_id = self.store.queue_control(run_id, kind, payload)
            self.tmux("kill-session", "-t", row["tmux_session"], check=False)
            self.store.update_run(run_id, status="stopped", desired_status="stopped")
            self.store.update_control(control_id, "confirmed", "closed owned terminal runtime")
            return control_id
        if row["status"] in terminal and kind != "resolve":
            raise RuntimeErrorMessage(f"run is terminal ({row['status']})")
        if row["desired_status"] == "stopped" and kind != "resolve":
            raise RuntimeErrorMessage("run has a stop request in progress")
        if kind == "resume":
            if row["status"] != "paused" or row["desired_status"] != "paused":
                raise RuntimeErrorMessage("resume requires a confirmed paused run")
            if row["milestone_status"] == "review":
                raise RuntimeErrorMessage("assign the next milestone (or a revision) before resuming")
            if self.store.has_uncertain_work(run_id):
                raise RuntimeErrorMessage("resolve uncertain operations before resuming")
            if row["tokens_used"] >= row["token_budget"] or self.store.elapsed(row) >= row["time_budget_seconds"]:
                raise RuntimeErrorMessage("extend the exhausted budget before resuming")
        control_id = self.store.queue_control(run_id, kind, payload)
        if kind in {"pause", "stop"}:
            self.store.update_run(run_id, desired_status="paused" if kind == "pause" else "stopped")
        elif kind == "resume":
            self.store.update_run(run_id, desired_status="active")
        elif kind in {"send", "queue"} and row["desired_status"] in {"paused", "blocked"}:
            self.store.update_control(control_id, "waiting_resume", "delivery waits for an explicit resume")
        return control_id

    def resolve_attempt(self, run_id: str, attempt_id: str, outcome: str, evidence: str) -> None:
        attempt = self.store.attempt(run_id, attempt_id)
        if attempt["status"] != "outcome_unknown":
            raise RuntimeErrorMessage("only an outcome_unknown attempt can be resolved")
        self.store.resolve_attempt(run_id, attempt_id, outcome, evidence)
        row = self.store.get_run(run_id)
        if row["status"] == "blocked" and row["last_error"] == "validation command outcome requires resolution":
            config = config_from_snapshot(json.loads(row["config_json"]))
            attempts = {attempt["name"]: attempt["status"] for attempt in self.store.attempts(run_id)}
            if row["milestone_name"] and not self.store.has_uncertain_work(run_id):
                self.store.update_run(run_id, status="paused", desired_status="paused", milestone_status="review",
                                      last_error="validation outcome resolved; review evidence before the next milestone")
            if not row["milestone_name"] and config.checks and attempt["name"] in {check.name for check in config.checks} and all(
                   check.name in attempts and attempts[check.name] not in {"running", "outcome_unknown"}
                   for check in config.checks):
                final_status = "complete" if all(attempts[check.name] == "succeeded"
                                                  for check in config.checks) else "validation_failed"
                self.store.update_run(run_id, status=final_status, desired_status=final_status,
                                      finished_at=time.time(), last_error=None)
        self.store.add_event(run_id, "supervisor/attempt_resolved", {
            "attempt_id": attempt_id, "outcome": outcome, "evidence": evidence,
        })

    def attach(self, run_id: str) -> None:
        row = self.store.get_run(run_id)
        self.tmux("has-session", "-t", row["tmux_session"])
        self.tmux("select-window", "-t", f"{row['tmux_session']}:codex")
        binary = shutil.which("tmux")
        assert binary
        os.execv(binary, [binary, "-S", str(self.tmux_socket), "attach-session", "-t", row["tmux_session"]])

    def recover(self, run_id: str) -> None:
        row = self.store.get_run(run_id)
        self.codex = codex_executable(row["codex_executable"] or self.codex)
        if row["codex_executable"] and asyncio.run(version(self.codex)) != row["codex_version"]:
            raise RuntimeErrorMessage("recorded Codex executable changed version; restore the pinned binary before recovery")
        if row["status"] in {"complete", "validation_failed", "stopped", "failed"}:
            raise RuntimeErrorMessage(f"terminal runs retain history and cannot be restarted ({row['status']})")
        if row["desired_status"] == "stopped":
            raise RuntimeErrorMessage("run has a persisted stop request; inspect its outcome before recovery")
        session = row["tmux_session"]
        if self.tmux("has-session", "-t", session, check=False).returncode:
            raise RuntimeErrorMessage("owned tmux session is gone; automatic replacement was not created")
        worker = self.tmux("list-panes", "-t", f"{session}:supervisor", "-F", "#{pane_dead}", check=False)
        if worker.returncode == 0 and worker.stdout.strip() == "0":
            raise RuntimeErrorMessage("worker pane is still running; inspect status before recovering")
        config = config_from_snapshot(json.loads(row["config_json"]))
        interrupted = self.store.mark_interrupted_work_unknown(run_id)
        socket = self.app_socket(run_id)
        server = self.tmux("list-panes", "-t", f"{session}:app-server", "-F", "#{pane_dead}", check=False)
        command = self._server_command(run_id, socket, config, self.root / "logs")
        server_restarted = False
        if server.returncode:
            self.tmux("new-window", "-t", session, "-n", "app-server", command)
            self.tmux("set-window-option", "-t", f"{session}:app-server", "remain-on-exit", "on")
            server_restarted = True
        elif server.stdout.strip() == "1":
            self.tmux("respawn-window", "-k", "-t", f"{session}:app-server", command)
            server_restarted = True

        if server_restarted:
            unknown_native = self.store.mark_native_operations_unknown(run_id)
            if unknown_native:
                self.store.update_run(run_id, desired_status="paused")
                row = self.store.get_run(run_id)
        self.store.update_run(run_id, status="recovering")
        try:
            turns, native_status, observed = asyncio.run(
                self._reconcile_native(socket, row, config, self.root / "runs" / run_id)
            )
        except Exception as exc:
            self.store.update_run(run_id, status="recovering", last_error=f"thread reconciliation failed: {exc}")
            self.store.add_event(run_id, "supervisor/recovery_failed", {"error": str(exc)})
            raise RuntimeErrorMessage(f"recovery could not confirm the existing App Server thread: {exc}") from exc

        self.store.add_event(run_id, "supervisor/thread_reconciled", {
            "thread_id": row["thread_id"], "native_goal_status": native_status,
            "turn_ids": [turn.get("id") for turn in turns if isinstance(turn, dict)],
            "interrupted_attempts": interrupted["attempts"],
            "uncertain_controls": interrupted["controls"],
        })
        tui = self.tmux("list-panes", "-t", f"{session}:codex", "-F", "#{pane_dead}", check=False)
        tui_command = self._tui_command(run_id, row["thread_id"], config, socket)
        if tui.returncode:
            self.tmux("new-window", "-t", session, "-n", "codex", tui_command)
            self.tmux("set-window-option", "-t", f"{session}:codex", "remain-on-exit", "on")
        elif tui.stdout.strip() == "1" or server_restarted:
            self.tmux("respawn-window", "-k", "-t", f"{session}:codex", tui_command)

        self.store.update_run(run_id, status=observed, last_error=None)
        worker_command = self._worker_command(run_id, config, self.root / "logs")
        if worker.returncode:
            self.tmux("new-window", "-t", session, "-n", "supervisor", worker_command)
        else:
            self.tmux("respawn-window", "-k", "-t", f"{session}:supervisor", worker_command)
        self.tmux("set-window-option", "-t", f"{session}:supervisor", "remain-on-exit", "on")
        self.store.add_event(run_id, "supervisor/recovery_started", {"thread_id": row["thread_id"]})

    async def _reconcile_native(self, socket: Path, row: Any, config: RunConfig,
                                run_dir: Path) -> tuple[list[dict[str, Any]], str | None, str]:
        client = await self.connect_when_ready(socket, 20)
        try:
            response = await client.call("thread/resume", {
                "threadId": row["thread_id"], "cwd": str(row["repository"]),
            }, timeout=45)
            turns = response.get("thread", {}).get("turns", [])
            active_turn = next((turn for turn in reversed(turns)
                                if isinstance(turn, dict) and turn.get("status") == "inProgress"), None)
            goal = await client.call("thread/goal/get", {"threadId": row["thread_id"]}, timeout=20)
            native_status = (goal.get("goal") or {}).get("status")
            native_tokens = (goal.get("goal") or {}).get("tokensUsed")
            if isinstance(native_tokens, int):
                total_tokens, _ = self.store.observe_token_usage(row["id"], native_tokens)
            else:
                total_tokens = row["tokens_used"]
            if row["desired_status"] == "paused":
                await client.call("thread/goal/set", {"threadId": row["thread_id"], "status": "paused"})
                if active_turn and active_turn.get("id"):
                    await client.call("turn/interrupt", {
                        "threadId": row["thread_id"], "turnId": active_turn["id"],
                    })
                observed = "paused"
            elif (total_tokens >= row["token_budget"] or self.store.elapsed(row) >= row["time_budget_seconds"] or
                  self.store.has_uncertain_work(row["id"])):
                await client.call("thread/goal/set", {"threadId": row["thread_id"], "status": "paused"})
                if active_turn and active_turn.get("id"):
                    await client.call("turn/interrupt", {
                        "threadId": row["thread_id"], "turnId": active_turn["id"],
                    })
                self.store.update_run(row["id"], desired_status="paused",
                                      last_error="budget expired or operation requires resolution")
                observed = "paused"
            elif row["desired_status"] == "active":
                if native_status == "paused":
                    self.store.update_run(row["id"], desired_status="paused")
                    return turns, native_status, "paused"
                observed = "active" if native_status == "active" else "recovering"
            else:
                observed = "blocked"
            return turns, native_status, observed
        finally:
            await client.close()

    async def _interrupt_active_turn(self, client: RpcClient, thread_id: str,
                                     turn_id: str | None, timeout: float = 10) -> bool:
        if not turn_id or turn_id == "pending":
            resumed = await client.call("thread/resume", {"threadId": thread_id}, timeout=15)
            turns = resumed.get("thread", {}).get("turns", [])
            turn = next((item for item in reversed(turns)
                         if isinstance(item, dict) and item.get("status") == "inProgress"), None)
            if not turn:
                return True
            turn_id = turn.get("id")
        if not turn_id:
            return False
        await client.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resumed = await client.call("thread/resume", {"threadId": thread_id}, timeout=15)
            turns = resumed.get("thread", {}).get("turns", [])
            turn = next((item for item in reversed(turns)
                         if isinstance(item, dict) and item.get("id") == turn_id), None)
            if not turn or turn.get("status") != "inProgress":
                return True
            await asyncio.sleep(0.25)
        return False


def config_from_snapshot(snapshot: dict[str, Any]) -> RunConfig:
    from .config import Check, RunConfig
    return RunConfig(
        source=Path("<snapshot>"), version=1, project=snapshot["project"],
        repository=Path(snapshot["repository"]), objective=snapshot["objective"],
        runtime_root=Path(snapshot["runtime_root"]),
        context_files=tuple(Path(x) for x in snapshot.get("context_files", [])),
        model=snapshot.get("model"), reasoning_effort=snapshot.get("reasoning_effort"),
        approval_policy=snapshot.get("approval_policy", "on-request"),
        token_budget=snapshot["token_budget"], time_budget_seconds=snapshot["time_budget_seconds"],
        environment_allowlist=tuple(snapshot.get("environment_allowlist", [])),
        writable_paths=tuple(Path(x) for x in snapshot.get("writable_paths", [])),
        codex_executable=snapshot.get("codex_executable"), approvals_reviewer=snapshot.get("approvals_reviewer"),
        sandbox_mode=snapshot.get("sandbox_mode", "workspace-write"),
        artifact_paths=tuple(Path(p) for p in snapshot.get("artifact_paths", [])),
        milestone=snapshot.get("milestone"),
        initial_task_file=Path(snapshot["initial_task_file"]) if snapshot.get("initial_task_file") else None,
        checks=tuple(Check(c["name"], tuple(c["command"]), Path(c["cwd"]), c["timeout_seconds"],
                           tuple(Path(x) for x in c.get("result_files", [])))
                     for c in snapshot.get("checks", [])),
    )


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<nested>"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            non_secret_tokens = {"tokenbudget", "tokensused", "totaltokens", "tokenusage"}
            if normalized not in non_secret_tokens and any(
                word in normalized for word in ("authorization", "secret", "password", "credential",
                                                "api_key", "access_token", "refresh_token")
            ):
                result[key] = "<redacted>"
            else:
                result[key] = _sanitize(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:20000]
    return value


def _event_summary(method: str, params: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    summary: dict[str, Any] = {"method": method}
    objects = {"thread_id": params.get("thread"), "turn_id": params.get("turn"),
               "item_id": params.get("item"), "request_id": params.get("request")}
    for key, obj in objects.items():
        if isinstance(obj, dict) and obj.get("id"):
            summary[key] = str(obj["id"])
    for source_name, fields in (("turn", ("status", "tokensUsed", "totalTokens")),
                                ("goal", ("status", "tokensUsed", "tokenBudget", "timeUsedSeconds")),
                                ("item", ("type", "status"))):
        obj = params.get(source_name)
        if isinstance(obj, dict):
            for key in fields:
                value = obj.get(key)
                if isinstance(value, (str, int, float, bool)):
                    summary[key] = value
    if "delta" in params:
        delta = params["delta"]
        summary["has_delta"] = bool(delta)
        summary["delta_length"] = (len(delta) if isinstance(delta, str) else
                                    delta.get("length") if isinstance(delta, dict) else
                                    delta if isinstance(delta, int) else None)
    error = params.get("error")
    if isinstance(error, dict):
        if "code" in error:
            summary["error_code"] = str(error["code"])[:100]
        if isinstance(error.get("message"), str):
            summary["error_message_length"] = len(error["message"])
        elif isinstance(error.get("message"), dict):
            summary["error_message_length"] = error["message"].get("length")
    native_id = None
    if method.startswith("item/") and not method.endswith("/delta"):
        native_id = summary.get("item_id")
    elif method in {"turn/started", "turn/completed"}:
        native_id = summary.get("turn_id")
    elif method == "thread/started":
        native_id = summary.get("thread_id")
    return summary, native_id


def _record_goal_limited(store: Store, run_id: str, row: Any, status: str) -> None:
    """Persist a goal limit once, while keeping repeated reconciliation idempotent."""
    target = "paused" if status == "budgetLimited" else "blocked"
    error = f"Codex goal status: {status}"
    if row["status"] == target and row["desired_status"] == target and row["last_error"] == error:
        return
    store.update_run(run_id, status=target, desired_status=target, last_error=error)
    store.add_event(run_id, "supervisor/goal_limited", {"goal_status": status})


async def run_worker(root: Path, run_id: str) -> None:
    runtime = Runtime(root)
    deadline = time.monotonic() + 10
    while True:
        try:
            fd = acquire_worker_lock(runtime.root)
            break
        except WorkerAlreadyRunning:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.2)
    store = runtime.store
    try:
        row = store.get_run(run_id)
        if not row["thread_id"]:
            raise RuntimeErrorMessage("run has no confirmed thread ID; inspect startup outcome before retrying")
        config = config_from_snapshot(json.loads(row["config_json"]))
        runtime.codex = codex_executable(row["codex_executable"] or config.codex_executable or runtime.codex)
        socket = runtime.app_socket(run_id)
        client = RpcClient(runtime.codex, socket, runtime.codex_home, _environment(config, runtime.codex_home))
        try:
            await client.connect("agentic_supervisor_worker")
            response = await client.call("thread/resume", {"threadId": row["thread_id"], "cwd": row["repository"]}, timeout=45)
            store.add_event(run_id, "supervisor/worker_attached", {"thread_id": row["thread_id"]})
            turns = response.get("thread", {}).get("turns", [])
            active_turn = next((turn.get("id") for turn in reversed(turns)
                                if turn.get("status") == "inProgress"), None)
            store.add_event(run_id, "supervisor/thread_reconciled", {
                "thread_id": row["thread_id"],
                "turns": [{"id": turn.get("id"), "status": turn.get("status"),
                           "item_ids": [item.get("id") for item in turn.get("items", [])
                                        if isinstance(item, dict)]}
                          for turn in turns if isinstance(turn, dict)],
            })
            last_budget_check = 0.0
            checked_generation = None
            while True:
                row = store.get_run(run_id)
                for control in store.pending_controls(run_id):
                    kind, payload = control["kind"], control["payload"]
                    if kind in {"send", "queue"} and row["desired_status"] in {"paused", "blocked"}:
                        store.update_control(control["id"], "waiting_resume", "delivery waits for an explicit resume")
                        continue
                    store.update_control(control["id"], "dispatched")
                    resumed_inputs = []
                    try:
                        if kind in {"send", "queue"}:
                            if kind == "send" and active_turn and active_turn != "pending":
                                await client.call("turn/steer", {
                                    "threadId": row["thread_id"], "expectedTurnId": active_turn,
                                    "input": [{"type": "text", "text": payload}],
                                    "clientUserMessageId": control["id"],
                                })
                                store.update_control(control["id"], "accepted", "steering accepted")
                            elif active_turn:
                                store.update_control(control["id"], "waiting_turn", "waiting for the current turn to finish")
                            else:
                                turn_result = await client.call("turn/start", {
                                    "threadId": row["thread_id"], "cwd": row["repository"],
                                    "input": [{"type": "text", "text": payload}],
                                    "clientUserMessageId": control["id"],
                                    "model": config.model, "effort": config.reasoning_effort,
                                    "sandboxPolicy": runtime._sandbox_policy(run_id, config),
                                })
                                active_turn = turn_result.get("turn", {}).get("id") or "pending"
                                store.update_control(control["id"], "accepted", "follow-up turn accepted")
                        elif kind == "pause":
                            await client.call("thread/goal/set", {"threadId": row["thread_id"], "status": "paused"})
                            interrupted = await runtime._interrupt_active_turn(client, row["thread_id"], active_turn)
                            if interrupted:
                                active_turn = None
                                store.update_run(run_id, status="paused", desired_status="paused")
                                store.update_control(control["id"], "confirmed", "goal paused; active turn ended")
                            else:
                                store.update_run(run_id, status="pausing", desired_status="paused")
                                store.update_control(control["id"], "awaiting_ack", "goal paused; waiting for active turn to stop")
                        elif kind == "interrupt":
                            if active_turn and active_turn != "pending":
                                await client.call("turn/interrupt", {"threadId": row["thread_id"], "turnId": active_turn})
                            active_turn = None
                            store.update_control(control["id"], "confirmed", "active turn interrupted")
                        elif kind == "stop":
                            await client.call("thread/goal/set", {"threadId": row["thread_id"], "status": "paused"})
                            await runtime._interrupt_active_turn(client, row["thread_id"], active_turn)
                            active_turn = None
                            store.update_control(control["id"], "confirmed", "goal paused before owned session shutdown")
                        elif kind == "resume":
                            resumed_inputs = sorted([c for c in store.controls(run_id)
                                if c["kind"] in {"send", "queue"} and c["status"] in {"waiting_resume", "waiting_turn", "queued"}],
                                key=lambda c: (c["created_at"], c["id"]))
                            for queued in resumed_inputs:
                                store.update_control(queued["id"], "dispatched")
                            prompt = "Continue the existing project in this same thread. Read prior findings and decisions before proceeding."
                            if resumed_inputs:
                                prompt += "\nQueued human instructions, in order:\n" + "\n".join(c["payload"] for c in resumed_inputs)
                            active_turn = await runtime.activate(client, run_id, config, prompt)
                            for queued in resumed_inputs:
                                store.update_control(queued["id"], "accepted", "included in resumed turn")
                            store.release_waiting_controls(run_id)
                            store.update_control(control["id"], "confirmed", "goal resumed")
                        elif kind == "resolve":
                            item = json.loads(payload)
                            store.resolve_attempt(run_id, item["attempt_id"], item["outcome"], item["evidence"])
                            if row["status"] == "blocked" and row["last_error"] == "validation command outcome requires resolution":
                                attempts = {attempt["name"]: attempt["status"] for attempt in store.attempts(run_id)}
                                if config.checks and item["attempt_id"] in {
                                       attempt["id"] for attempt in store.attempts(run_id)
                                       if attempt["name"] in {check.name for check in config.checks}
                                   } and all(check.name in attempts and attempts[check.name] not in {"running", "outcome_unknown"}
                                       for check in config.checks):
                                    final_status = "complete" if all(attempts[check.name] == "succeeded"
                                                                      for check in config.checks) else "validation_failed"
                                    store.update_run(run_id, status=final_status, desired_status=final_status,
                                                     finished_at=time.time(), last_error=None)
                            store.update_control(control["id"], "confirmed", "attempt resolution recorded")
                        else:
                            store.update_control(control["id"], "failed", f"unsupported control: {kind}")
                    except (CodexError, asyncio.TimeoutError) as exc:
                        for queued in resumed_inputs:
                            store.update_control(queued["id"], "outcome_unknown", str(exc))
                        store.update_control(control["id"], "outcome_unknown", str(exc))
                        store.update_run(run_id, status="recovering", desired_status="paused", last_error=str(exc))
                        store.add_event(run_id, "supervisor/control_outcome_unknown",
                                        {"control_id": control["id"], "error": str(exc)})
                    except (ValueError, KeyError, json.JSONDecodeError) as exc:
                        for queued in resumed_inputs:
                            store.update_control(queued["id"], "waiting_resume", "resume precondition failed before dispatch")
                        store.update_run(run_id, status="paused", desired_status="paused", last_error=str(exc))
                        store.update_control(control["id"], "failed", str(exc))
                        store.add_event(run_id, "supervisor/control_failed", {
                            "control_id": control["id"], "error": str(exc),
                        })

                row = store.get_run(run_id)
                if row["status"] in {"complete", "validation_failed", "failed"}:
                    return
                if row["desired_status"] == "stopped":
                    store.update_run(run_id, status="stopped", finished_at=time.time())
                    store.add_event(run_id, "supervisor/run_stopped", {})
                    runtime.tmux("kill-session", "-t", row["tmux_session"], check=False)
                    return

                now = time.time()
                if store.elapsed(row) >= row["time_budget_seconds"] and row["status"] == "active":
                    await client.call("thread/goal/set", {"threadId": row["thread_id"], "status": "paused"})
                    if active_turn and active_turn != "pending":
                        await client.call("turn/interrupt", {"threadId": row["thread_id"], "turnId": active_turn})
                    store.update_run(run_id, status="paused", desired_status="paused",
                                     last_error="elapsed-time budget reached")
                    store.add_event(run_id, "supervisor/time_budget_reached",
                                    {"budget_seconds": row["time_budget_seconds"]})

                if now - last_budget_check >= 5:
                    last_budget_check = now
                    try:
                        result = await client.call("thread/goal/get", {"threadId": row["thread_id"]}, timeout=10)
                        goal = result.get("goal") or {}
                        status = goal.get("status")
                        if row["status"] == "pausing" and active_turn:
                            resumed = await client.call("thread/resume", {
                                "threadId": row["thread_id"], "cwd": row["repository"],
                            }, timeout=15)
                            turns = resumed.get("thread", {}).get("turns", [])
                            current_turn = next((turn for turn in reversed(turns)
                                                 if isinstance(turn, dict) and turn.get("id") == active_turn), None)
                            if not current_turn or current_turn.get("status") != "inProgress":
                                active_turn = None
                                store.update_run(run_id, status="paused")
                                for control in store.controls(run_id):
                                    if control["kind"] == "pause" and control["status"] == "awaiting_ack":
                                        store.update_control(control["id"], "confirmed", "active turn ended")
                        if isinstance(goal.get("tokensUsed"), int):
                            total_tokens, token_delta = store.observe_token_usage(run_id, goal["tokensUsed"])
                            if token_delta:
                                store.add_event(run_id, "supervisor/token_usage", {
                                    "native_tokens_used": goal["tokensUsed"],
                                    "supervisor_tokens_used": total_tokens,
                                    "token_budget": row["token_budget"],
                                })
                            if total_tokens >= row["token_budget"] and row["status"] == "active":
                                await client.call("thread/goal/set", {
                                    "threadId": row["thread_id"], "status": "paused",
                                })
                                if active_turn and active_turn != "pending":
                                    await client.call("turn/interrupt", {
                                        "threadId": row["thread_id"], "turnId": active_turn,
                                    })
                                store.update_run(run_id, status="paused", desired_status="paused",
                                                 last_error="supervisor token budget reached")
                                store.add_event(run_id, "supervisor/token_budget_reached", {
                                    "tokens_used": total_tokens, "token_budget": row["token_budget"],
                                })
                        if status == "paused" and row["status"] == "active" and not active_turn:
                            store.update_run(run_id, status="paused", desired_status="paused",
                                             last_error="native goal paused; inspect progress before resuming")
                        if status == "complete" and not active_turn and row["status"] not in {"complete", "stopped", "validation_failed"}:
                            if checked_generation != row["milestone_generation"] and row["milestone_status"] != "review":
                                checked_generation = row["milestone_generation"]
                                outcomes = []
                                for check in config.checks:
                                    latest = store.get_run(run_id)
                                    remaining = int(latest["time_budget_seconds"] - store.elapsed(latest))
                                    if remaining <= 0:
                                        attempt_id = store.create_attempt(run_id, f"{row['milestone_generation']}/{check.name}" if row["milestone_name"] else check.name, list(check.command))
                                        store.resolve_attempt(run_id, attempt_id, "not_run",
                                                              "elapsed-time budget reached before dispatch")
                                        outcomes.append({"attempt_id": attempt_id, "name": check.name,
                                                         "status": "not_run",
                                                         "reason": "elapsed-time budget reached"})
                                        store.add_event(run_id, "check/not_run", {
                                            "name": check.name, "reason": "elapsed-time budget reached",
                                        })
                                        continue
                                    outcomes.append(await run_check(client, store, run_id, check,
                                                                   runtime.root / "runs" / run_id,
                                                                   timeout_limit=remaining,
                                                                   scope=str(row["milestone_generation"]) if row["milestone_name"] else None,
                                                                   sandbox_policy=runtime._sandbox_policy(run_id, config)))
                                unknown = any(x["status"] in {"outcome_unknown", "not_run"} for x in outcomes)
                                if unknown:
                                    store.update_run(run_id, status="blocked", desired_status="blocked",
                                                     last_error="validation command outcome requires resolution")
                                    store.add_event(run_id, "supervisor/validation_blocked", {"checks": outcomes})
                                else:
                                    if row["milestone_name"]:
                                        await runtime._interrupt_active_turn(client, row["thread_id"], active_turn)
                                        active_turn = None
                                        store.update_run(run_id, status="paused", desired_status="paused",
                                                         milestone_status="review", last_error=None if all(x["status"] == "succeeded" for x in outcomes) else "milestone validation failed")
                                        store.add_event(run_id, "supervisor/milestone_review", {
                                            "milestone": row["milestone_name"], "checks": outcomes,
                                            "thread_id": row["thread_id"], "tokens_used": total_tokens,
                                        })
                                        continue
                                    final_status = "complete" if all(x["status"] == "succeeded" for x in outcomes) else "validation_failed"
                                    store.update_run(run_id, status=final_status, desired_status=final_status,
                                                     finished_at=time.time())
                                    store.add_event(run_id, "supervisor/goal_completed", {"checks": outcomes})
                                    if final_status in {"complete", "validation_failed"}:
                                        return
                        elif status in {"usageLimited", "budgetLimited", "blocked"} and row["status"] not in {"complete", "stopped"}:
                            _record_goal_limited(store, run_id, row, status)
                    except CodexError as exc:
                        store.add_event(run_id, "supervisor/goal_status_error", {"error": str(exc)})

                try:
                    message = await client.next_event(timeout=0.25)
                except asyncio.TimeoutError:
                    if client.dropped_notifications:
                        store.add_event(run_id, "supervisor/event_gap", {
                            "dropped_notifications": client.dropped_notifications,
                            "reason": "bounded in-memory App Server event queue was full",
                        })
                        client.dropped_notifications = 0
                    continue
                method = message.get("method", "unknown")
                if method == "supervisor/disconnected":
                    raise CodexError("App Server disconnected; recover the existing run")
                raw_params = message.get("params", {})
                if isinstance(raw_params, dict) and raw_params.get("threadId") not in {None, row["thread_id"]}:
                    continue
                params, native_id = _event_summary(method, raw_params if isinstance(raw_params, dict) else {})
                store.add_event(run_id, method, params, native_id)
                if method == "turn/started":
                    if active_turn in {None, "pending", params.get("turn_id")}:
                        active_turn = params.get("turn_id")
                elif method == "turn/completed":
                    was_active = active_turn in {"pending", params.get("turn_id")}
                    if was_active:
                        active_turn = None
                    if was_active and params.get("status") == "failed":
                        await client.call("thread/goal/set", {"threadId": row["thread_id"], "status": "paused"})
                        store.update_run(run_id, status="paused", desired_status="paused",
                                         last_error="Codex turn failed; inspect the native TUI before resuming")
                        store.add_event(run_id, "supervisor/turn_failed", params)
                    try:
                        checkpoint = await asyncio.to_thread(git_checkpoint, config.repository)
                        store.add_event(run_id, "supervisor/git_checkpoint", checkpoint,
                                        native_id=params.get("turn_id"))
                    except Exception as exc:
                        store.add_event(run_id, "supervisor/git_checkpoint_failed", {"error": str(exc)})
                    current = store.get_run(run_id)
                    if current["desired_status"] == "paused":
                        store.update_run(run_id, status="paused")
                        for control in store.controls(run_id):
                            if control["kind"] == "pause" and control["status"] == "awaiting_ack":
                                store.update_control(control["id"], "confirmed", "active turn ended")
                    store.release_waiting_controls(run_id)
                elif method.endswith("requestApproval") or method == "item/tool/requestUserInput":
                    store.add_event(run_id, "supervisor/approval_pending", {
                        "method": method, "request_id": params.get("request_id"),
                    })
                if client.dropped_notifications:
                    store.add_event(run_id, "supervisor/event_gap", {
                        "dropped_notifications": client.dropped_notifications,
                        "reason": "bounded in-memory App Server event queue was full",
                    })
                    client.dropped_notifications = 0
        finally:
            await client.close()
    finally:
        os.close(fd)

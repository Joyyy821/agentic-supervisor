"""Command line interface and detached worker entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .codex import codex_executable, version
from .config import ConfigError, load_config
from .runtime import Runtime, RuntimeErrorMessage


def default_root() -> Path:
    configured = os.environ.get("AGENTIC_SUPERVISOR_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    state_home = os.environ.get("XDG_STATE_HOME")
    return (Path(state_home) if state_home else Path.home() / ".local" / "state") / "agentic-supervisor"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agentic-supervisor", description="Manage persistent Codex runs")
    result.add_argument("--runtime-root", help="private runtime root (or set AGENTIC_SUPERVISOR_HOME)")
    commands = result.add_subparsers(dest="command", required=True)

    commands.add_parser("login", help="sign in under the private supervisor Codex home")
    commands.add_parser("doctor", help="check Codex, authentication, runtime paths, and tmux")
    start = commands.add_parser("start", help="start a detached autonomous run")
    start.add_argument("config")
    commands.add_parser("list", help="list saved runs")
    for name in ("status",):
        item = commands.add_parser(name)
        item.add_argument("run_id")
    events = commands.add_parser("events")
    events.add_argument("run_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--follow", action="store_true")
    attach = commands.add_parser("attach")
    attach.add_argument("run_id")
    send = commands.add_parser("send")
    send.add_argument("run_id")
    send.add_argument("text")
    send.add_argument("--queue", action="store_true")
    for name in ("pause", "resume", "interrupt", "stop", "recover"):
        item = commands.add_parser(name)
        item.add_argument("run_id")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("run_id")
    resolve.add_argument("target_id")
    resolve.add_argument("--type", dest="target_type", choices=["attempt", "control"], default="attempt")
    resolve.add_argument("--outcome", required=True,
                         choices=["succeeded", "failed", "not_run", "accepted", "rejected", "not_applied"])
    resolve.add_argument("--evidence", required=True)
    return result


def _private_config(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    config = home / "config.toml"
    if not config.exists():
        config.write_text('approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n\n[features]\ngoals = true\n',
                          encoding="utf-8")
        config.chmod(0o600)


def _doctor(root: Path, runtime: Runtime | None) -> int:
    results: dict[str, Any] = {"runtime_root": str(root), "checks": {}}
    checks = results["checks"]
    executable = runtime.codex if runtime else codex_executable()
    try:
        checks["codex"] = awaitable_version(executable)
        checks["codex_supported"] = checks["codex"] == "codex-cli 0.144.4"
    except Exception as exc:
        checks["codex_error"] = str(exc)
        checks["codex_supported"] = False
    try:
        checks["tmux"] = subprocess.run(["tmux", "-V"], capture_output=True, text=True,
                                        timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError) as exc:
        checks["tmux_error"] = str(exc)
        checks["tmux"] = None
    home = root / "codex-home"
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    try:
        login = subprocess.run([executable, "login", "status"], capture_output=True, text=True,
                               env=env, timeout=10)
        checks["authenticated"] = login.returncode == 0
        checks["auth_status"] = "signed in" if login.returncode == 0 else "not signed in to private Codex home"
    except (OSError, subprocess.SubprocessError) as exc:
        checks["authenticated"] = False
        checks["auth_status"] = f"could not verify private Codex home: {exc}"
    try:
        checks["app_server"] = asyncio.run(runtime.protocol_probe()) if runtime else None
        checks["protocol_ready"] = checks["app_server"] is not None
    except Exception as exc:
        checks["protocol_error"] = str(exc)
        checks["protocol_ready"] = False
    checks["private_home_exists"] = home.exists()
    results["ready"] = bool(checks.get("codex_supported") and checks.get("tmux")
                             and checks.get("authenticated") and checks.get("protocol_ready"))
    print(json.dumps(results, indent=2))
    return 0 if results["ready"] else 1


def awaitable_version(executable: str) -> str:
    return asyncio.run(version(executable))


def _print_status(runtime: Runtime, run_id: str) -> None:
    row = runtime.store.get_run(run_id)
    attempts = runtime.store.attempts(run_id)
    checkpoint = runtime.store.latest_event(run_id, "supervisor/git_checkpoint")
    print(json.dumps({
        "id": row["id"], "project": row["project"], "repository": row["repository"],
        "thread_id": row["thread_id"], "status": row["status"],
        "desired_status": row["desired_status"], "objective": row["objective"],
        "codex_version": row["codex_version"], "git_head": row["git_head"],
        "git_dirty_at_start": bool(row["git_dirty"]), "created_at": row["created_at"],
        "started_at": row["started_at"], "last_error": row["last_error"],
        "tokens_used": row["tokens_used"], "token_budget": row["token_budget"],
        "latest_checkpoint": json.loads(checkpoint["payload_json"]) if checkpoint else None,
        "attempts": [{"id": a["id"], "name": a["name"], "status": a["status"],
                       "exit_code": a["exit_code"]} for a in attempts],
        "controls": [{"id": c["id"], "kind": c["kind"], "status": c["status"],
                      "created_at": c["created_at"], "detail": c["detail"]}
                     for c in runtime.store.controls(run_id)],
    }, indent=2))


def _print_events(runtime: Runtime, run_id: str, after: int, follow: bool) -> None:
    cursor = after
    try:
        while True:
            rows = runtime.store.events(run_id, cursor)
            for row in rows:
                print(json.dumps({"id": row["id"], "at": row["created_at"], "kind": row["kind"],
                                  "payload": json.loads(row["payload_json"])}, ensure_ascii=False), flush=True)
                cursor = row["id"]
            if not follow:
                return
            time.sleep(0.5)
    except KeyboardInterrupt:
        return


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.runtime_root).expanduser().resolve() if args.runtime_root else default_root()
    if args.command == "login":
        executable = codex_executable()
        home = root / "codex-home"
        _private_config(home)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        command = [executable, "login", "--device-auth"]
        return subprocess.run(command, env=env).returncode
    if args.command == "doctor":
        return _doctor(root, Runtime(root))

    if args.command == "start":
        config = load_config(args.config, args.runtime_root)
        runtime = Runtime(config.runtime_root)
        run_id = asyncio.run(runtime.start(config))
        print(run_id)
        return 0
    runtime = Runtime(root)
    if args.command == "list":
        for row in runtime.store.list_runs():
            print(json.dumps({"id": row["id"], "project": row["project"], "status": row["status"],
                              "desired_status": row["desired_status"], "thread_id": row["thread_id"],
                              "tokens_used": row["tokens_used"], "token_budget": row["token_budget"],
                              "created_at": row["created_at"]}))
        return 0
    if args.command == "status":
        _print_status(runtime, args.run_id)
        return 0
    if args.command == "events":
        _print_events(runtime, args.run_id, args.after, args.follow)
        return 0
    if args.command == "attach":
        runtime.attach(args.run_id)
        return 0
    if args.command == "recover":
        runtime.recover(args.run_id)
        print("recovery worker started; inspect status and events")
        return 0
    if args.command == "send":
        kind = "queue" if args.queue else "send"
        control_id = runtime.submit(args.run_id, kind, args.text)
    elif args.command in {"pause", "resume", "interrupt", "stop"}:
        control_id = runtime.submit(args.run_id, args.command)
    elif args.command == "resolve":
        if args.target_type == "control":
            runtime.store.resolve_control(args.run_id, args.target_id, args.outcome, args.evidence)
            runtime.store.add_event(args.run_id, "supervisor/control_resolved", {
                "control_id": args.target_id, "outcome": args.outcome, "evidence": args.evidence,
            })
            print("control resolution recorded; no operation was retried")
            return 0
        if args.outcome not in {"succeeded", "failed", "not_run"}:
            raise RuntimeErrorMessage("attempt outcomes must be succeeded, failed, or not_run")
        runtime.resolve_attempt(args.run_id, args.target_id, args.outcome, args.evidence)
        print("attempt resolution recorded; no operation was retried")
        return 0
    else:
        raise RuntimeErrorMessage(f"unsupported command: {args.command}")
    print(control_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, RuntimeErrorMessage, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"agentic-supervisor: {exc}", file=sys.stderr)
        raise SystemExit(2)

"""Sandboxed, bounded project validation commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from .codex import RpcClient
from .config import Check
from .store import Store


async def run_check(client: RpcClient, store: Store, run_id: str, check: Check,
                    output_dir: Path, timeout_limit: int | None = None, scope: str | None = None,
                    sandbox_policy: dict | None = None) -> dict[str, object]:
    attempt_name = f"{scope}/{check.name}" if scope is not None else check.name
    previous = [attempt for attempt in store.attempts(run_id) if attempt["name"] == attempt_name]
    if previous and previous[-1]["status"] != "not_run":
        attempt = previous[-1]
        try:
            result = json.loads(attempt["result_json"] or "{}")
        except json.JSONDecodeError:
            result = {}
        return {"attempt_id": attempt["id"], "name": check.name, "status": attempt["status"],
                "exit_code": attempt["exit_code"], "artifacts": result.get("artifacts", {}),
                "reused_after_recovery": True}

    attempt_id = store.create_attempt(run_id, attempt_name, list(check.command))
    attempt_dir = output_dir / "artifacts" / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if check.cwd.resolve() != check.cwd or not check.cwd.is_dir():
        reason = "configured working directory changed or disappeared before dispatch"
        store.finish_attempt(attempt_id, "failed", None, "", "", {"error": reason})
        store.add_event(run_id, "check/completed", {
            "attempt_id": attempt_id, "name": check.name, "status": "failed", "error": reason,
        }, native_id=attempt_id)
        return {"attempt_id": attempt_id, "name": check.name, "status": "failed", "error": reason}
    try:
        timeout_seconds = min(check.timeout_seconds, timeout_limit) if timeout_limit is not None else check.timeout_seconds
        if timeout_seconds < 1:
            store.resolve_attempt(run_id, attempt_id, "not_run", "run time budget expired before dispatch")
            return {"attempt_id": attempt_id, "name": check.name, "status": "not_run"}
        writable_roots = list(dict.fromkeys(str(path) for path in
                                             [check.cwd, *(result.parent for result in check.result_files)]))
        response = await client.call("command/exec", {
            "command": list(check.command),
            "cwd": str(check.cwd),
            "sandboxPolicy": sandbox_policy or {"type": "workspaceWrite", "writableRoots": writable_roots},
            "timeoutMs": timeout_seconds * 1000,
        }, timeout=timeout_seconds + 10)
    except Exception as exc:
        store.resolve_attempt(run_id, attempt_id, "outcome_unknown", str(exc))
        store.add_event(run_id, "check/outcome_unknown", {"attempt_id": attempt_id, "check": check.name, "error": str(exc)})
        return {"attempt_id": attempt_id, "status": "outcome_unknown", "error": str(exc)}

    stdout = response.get("stdout", "")
    stderr = response.get("stderr", "")
    if isinstance(stdout, str):
        (attempt_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    if isinstance(stderr, str):
        (attempt_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    exit_code = response.get("exitCode")
    artifacts: dict[str, str] = {}
    json_results: dict[str, object] = {}
    for path in check.result_files:
        if path.resolve() != path:
            store.add_event(run_id, "check/artifact_path_changed", {
                "check": check.name, "path": str(path),
            })
            continue
        if path.is_file():
            hasher = hashlib.sha256()
            with path.open("rb") as artifact:
                for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            artifacts[str(path)] = digest
            if path.suffix.lower() == ".json" and path.stat().st_size <= 2 * 1024 * 1024:
                try:
                    json_results[str(path)] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
    status = "succeeded" if exit_code == 0 else "failed"
    store.finish_attempt(attempt_id, status, exit_code, str(attempt_dir / "stdout.txt"),
                         str(attempt_dir / "stderr.txt"), {"artifacts": artifacts, "json_results": json_results})
    summary = {"attempt_id": attempt_id, "name": check.name, "status": status,
               "exit_code": exit_code, "artifacts": artifacts}
    store.add_event(run_id, "check/completed", summary, native_id=attempt_id)
    return summary

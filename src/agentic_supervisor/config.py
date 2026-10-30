"""Validated, project-independent run configuration."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    cwd: Path
    timeout_seconds: int
    result_files: tuple[Path, ...]


@dataclass(frozen=True)
class RunConfig:
    source: Path
    version: int
    project: str
    repository: Path
    objective: str
    runtime_root: Path
    context_files: tuple[Path, ...]
    model: str | None
    reasoning_effort: str | None
    approval_policy: str
    token_budget: int
    time_budget_seconds: int
    environment_allowlist: tuple[str, ...]
    writable_paths: tuple[Path, ...]
    checks: tuple[Check, ...]
    codex_executable: str | None = None
    approvals_reviewer: str | None = None
    sandbox_mode: str = "workspace-write"
    artifact_paths: tuple[Path, ...] = ()
    milestone: dict[str, str] | None = None
    initial_task_file: Path | None = None

    def safe_snapshot(self) -> dict[str, Any]:
        return {
            "codex_executable": self.codex_executable,
            "approvals_reviewer": self.approvals_reviewer,
            "sandbox_mode": self.sandbox_mode,
            "artifact_paths": [str(p) for p in self.artifact_paths],
            "milestone": self.milestone,
            "initial_task_file": str(self.initial_task_file) if self.initial_task_file else None,
            "version": self.version,
            "project": self.project,
            "repository": str(self.repository),
            "objective": self.objective,
            "runtime_root": str(self.runtime_root),
            "context_files": [str(p) for p in self.context_files],
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "approval_policy": self.approval_policy,
            "token_budget": self.token_budget,
            "time_budget_seconds": self.time_budget_seconds,
            "environment_allowlist": list(self.environment_allowlist),
            "writable_paths": [str(p) for p in self.writable_paths],
            "checks": [
                {
                    "name": check.name,
                    "command": list(check.command),
                    "cwd": str(check.cwd),
                    "timeout_seconds": check.timeout_seconds,
                    "result_files": [str(p) for p in check.result_files],
                }
                for check in self.checks
            ],
        }


def _positive_int(data: dict[str, Any], name: str, default: int, maximum: int) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise ConfigError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _inside(root: Path, candidate: Path, label: str) -> Path:
    resolved = candidate.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{label} must be inside the repository: {resolved}") from exc
    return resolved


def _path_list(value: Any, root: Path, label: str) -> tuple[Path, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{label} must be an array of paths")
    paths = []
    for item in value:
        path = Path(item)
        if not path.is_absolute():
            path = root / path
        paths.append(_inside(root, path, label))
    return tuple(paths)


def load_config(path: str | Path, runtime_override: str | Path | None = None) -> RunConfig:
    source = Path(path).expanduser().resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a JSON object")
    if data.get("version") != 1:
        raise ConfigError("configuration version must be 1")

    project = data.get("project")
    objective = data.get("objective")
    if not isinstance(project, str) or not project.strip() or len(project) > 100:
        raise ConfigError("project must be a non-empty string of at most 100 characters")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 4000:
        raise ConfigError("objective must be a non-empty string of at most 4000 characters")

    repo_value = data.get("repository")
    if not isinstance(repo_value, str):
        raise ConfigError("repository must be a path string")
    repository = Path(repo_value).expanduser()
    if not repository.is_absolute():
        repository = source.parent / repository
    repository = repository.resolve(strict=True)
    if not repository.is_dir():
        raise ConfigError("repository must be a directory")
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"repository must be an accessible Git working tree: {exc}") from exc
    git_root = Path(result.stdout.strip()).resolve()
    if git_root != repository:
        raise ConfigError(f"repository must name the Git root itself ({git_root})")

    runtime_value = runtime_override or data.get("runtime_root") or os.environ.get("AGENTIC_SUPERVISOR_HOME")
    if runtime_value:
        runtime_root = Path(runtime_value).expanduser()
        if not runtime_root.is_absolute():
            runtime_root = source.parent / runtime_root
        runtime_root = runtime_root.resolve()
    else:
        runtime_root = (Path.home() / ".local" / "state" / "agentic-supervisor").resolve()
    if (runtime_root == repository or repository in runtime_root.parents or
            runtime_root in repository.parents):
        raise ConfigError("runtime_root and target repository must not contain one another")

    context_files = _path_list(data.get("context_files", []), repository, "context_files")
    for context in context_files:
        if not context.is_file():
            raise ConfigError(f"context file does not exist: {context}")
    writable_paths = _path_list(data.get("writable_paths", []), repository, "writable_paths")

    model = data.get("model")
    reasoning = data.get("reasoning_effort")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError("model must be null or a non-empty string")
    if reasoning is not None and reasoning not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ConfigError("reasoning_effort must be null, low, medium, high, xhigh, or ultra")
    approval_policy = data.get("approval_policy", "on-request")
    if approval_policy not in {"untrusted", "on-request"}:
        raise ConfigError("approval_policy must be 'untrusted' or 'on-request'")

    reviewer = data.get("approvals_reviewer")
    if reviewer not in {None, "user", "auto_review"}:
        raise ConfigError("approvals_reviewer must be user or auto_review")
    sandbox = data.get("sandbox_mode", "workspace-write")
    if sandbox not in {"workspace-write", "read-only"}:
        raise ConfigError("sandbox_mode must be workspace-write or read-only")
    executable = data.get("codex_executable")
    if executable is not None:
        if not isinstance(executable, str) or not executable:
            raise ConfigError("codex_executable must be a path")
        ep = Path(executable).expanduser()
        if not ep.is_absolute():
            ep = source.parent / ep
        executable = str(ep.resolve(strict=True))
        if not os.access(executable, os.X_OK):
            raise ConfigError("codex_executable must be executable")
    milestone = validate_milestone(data.get("milestone"))
    raw_artifacts = data.get("artifact_paths", [])
    if not isinstance(raw_artifacts, list) or any(not isinstance(p, str) for p in raw_artifacts):
        raise ConfigError("artifact_paths must be an array of paths")
    artifacts = []
    for value in raw_artifacts:
        ap = Path(value).expanduser()
        ap = (ap if ap.is_absolute() else source.parent / ap).resolve()
        if (ap in {Path('/'), Path.home()} or ap == runtime_root or
                ap in runtime_root.parents or runtime_root in ap.parents):
            raise ConfigError("artifact paths must not expose runtime state or the home/root directory")
        if not ap.is_dir():
            raise ConfigError("artifact_paths directories must already exist")
        artifacts.append(ap)

    task_file = data.get("initial_task_file")
    if task_file is not None:
        if not isinstance(task_file, str):
            raise ConfigError("initial_task_file must be a path")
        tp = Path(task_file).expanduser()
        task_file = (tp if tp.is_absolute() else source.parent / tp).resolve(strict=True)
        if not task_file.is_file() or task_file.stat().st_size > 128 * 1024:
            raise ConfigError("initial_task_file must be a text file of at most 128 KiB")
        task_file.read_text(encoding="utf-8")

    env_names = data.get("environment_allowlist", [])
    if not isinstance(env_names, list) or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        for name in env_names
    ):
        raise ConfigError("environment_allowlist must contain environment variable names")
    if len(set(env_names)) != len(env_names):
        raise ConfigError("environment_allowlist entries must be unique")

    raw_checks = data.get("checks", [])
    if not isinstance(raw_checks, list):
        raise ConfigError("checks must be an array")
    checks: list[Check] = []
    names: set[str] = set()
    for entry in raw_checks:
        if not isinstance(entry, dict):
            raise ConfigError("each check must be an object")
        name = entry.get("name")
        command = entry.get("command")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ConfigError("check name must use letters, numbers, '.', '_' or '-' (max 64)")
        if name in names:
            raise ConfigError(f"duplicate check name: {name}")
        names.add(name)
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
            raise ConfigError(f"check {name} command must be a non-empty argument array")
        cwd_value = entry.get("cwd", ".")
        if not isinstance(cwd_value, str):
            raise ConfigError(f"check {name} cwd must be a path")
        cwd = Path(cwd_value)
        if not cwd.is_absolute():
            cwd = repository / cwd
        cwd = _inside(repository, cwd, f"check {name} cwd")
        if not cwd.is_dir():
            raise ConfigError(f"check {name} cwd must exist and be a directory")
        timeout = entry.get("timeout_seconds", 900)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 86400:
            raise ConfigError(f"check {name} timeout_seconds must be between 1 and 86400")
        raw_results = entry.get("result_files", [])
        if not isinstance(raw_results, list) or any(not isinstance(p, str) for p in raw_results):
            raise ConfigError("result_files must be an array of paths")
        results = []
        for value in raw_results:
            rp = Path(value).expanduser()
            rp = (rp if rp.is_absolute() else repository / rp).resolve()
            if not any(rp == root or root in rp.parents for root in (repository, *artifacts)):
                raise ConfigError("result_files must be in the repository or configured artifact paths")
            results.append(rp)
        results = tuple(results)
        checks.append(Check(name, tuple(command), cwd, timeout, results))

    return RunConfig(
        source=source,
        version=1,
        project=project,
        repository=repository,
        objective=objective.strip(),
        runtime_root=runtime_root,
        context_files=context_files,
        model=model,
        reasoning_effort=reasoning,
        approval_policy=approval_policy,
        token_budget=_positive_int(data, "token_budget", 100000, 10000000),
        time_budget_seconds=_positive_int(data, "time_budget_seconds", 7200, 604800),
        environment_allowlist=tuple(env_names),
        writable_paths=writable_paths,
        checks=tuple(checks),
        codex_executable=executable, approvals_reviewer=reviewer, sandbox_mode=sandbox,
        artifact_paths=tuple(artifacts), milestone=milestone, initial_task_file=task_file,
    )


def validate_milestone(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"name", "objective"}:
        raise ConfigError("milestone must contain name and objective")
    if not isinstance(value["name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value["name"]):
        raise ConfigError("milestone name must use letters, digits, dots, dashes or underscores")
    if not isinstance(value["objective"], str) or not 1 <= len(value["objective"].strip()) <= 3500:
        raise ConfigError("milestone objective must contain 1 to 3500 characters")
    return {"name": value["name"], "objective": value["objective"].strip()}

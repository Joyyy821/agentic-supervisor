"""Operator-supplied evidence for a late, paused workflow checkpoint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import validate_milestone


def load_checkpoint(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("checkpoint version must be 1")
    if set(data) - {"version", "milestone", "completed_milestones", "evidence", "retire_checks"}:
        raise ValueError("unknown checkpoint field")
    milestone = validate_milestone(data.get("milestone"))
    if milestone is None:
        raise ValueError("checkpoint milestone is required")
    if not isinstance(data.get("retire_checks", False), bool):
        raise ValueError("retire_checks must be boolean")

    def evidence(paths: Any) -> list[dict[str, str]]:
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
            raise ValueError("each checkpoint needs a nonempty evidence path array")
        result = []
        for value in paths:
            target = Path(value).expanduser()
            target = (target if target.is_absolute() else path.parent / target).resolve(strict=True)
            with target.open("rb") as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
            result.append({"path": str(target), "sha256": digest.hexdigest()})
        return result

    history = data.get("completed_milestones", [])
    if not isinstance(history, list):
        raise ValueError("completed_milestones must be an array")
    completed = []
    names = {milestone["name"]}
    for entry in history:
        if not isinstance(entry, dict) or set(entry) != {"name", "evidence"}:
            raise ValueError("completed milestones need name and evidence only")
        name = entry["name"]
        validate_milestone({"name": name, "objective": "Recorded historical checkpoint"})
        if name in names:
            raise ValueError("checkpoint milestone names must be unique")
        names.add(name)
        completed.append({"name": name, "evidence": evidence(entry["evidence"])})
    result = {"milestone": milestone, "completed_milestones": completed,
              "evidence": evidence(data.get("evidence")),
              "retire_checks": data.get("retire_checks", False)}
    result["id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result

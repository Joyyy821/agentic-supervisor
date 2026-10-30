"""Shared isolated test directories; outputs are deliberately retained."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path


def test_root() -> Path:
    configured = os.environ.get("AGENTIC_SUPERVISOR_TEST_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        # Keep Unix socket paths below the host's sockaddr limit even when
        # tests create nested runtime directories. .runtime is Git-ignored.
        root = Path(__file__).resolve().parents[2] / ".runtime" / "t"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def new_git_repo(name: str = "fixture") -> Path:
    repo = test_root() / f"{name}-{uuid.uuid4()}"
    repo.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Supervisor Tests"], check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return repo

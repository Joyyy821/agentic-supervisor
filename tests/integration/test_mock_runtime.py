from __future__ import annotations

import asyncio
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agentic_supervisor.config import load_config
from agentic_supervisor.runtime import Runtime
from tests.unit.helpers import new_git_repo, test_root


class FakeRpc:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def call(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-fixture"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-fixture"}}
        return {}

    async def close(self):
        self.closed = True


class MockRuntimeTests(unittest.TestCase):
    def test_start_creates_thread_then_tui_and_bounded_goal(self):
        root = test_root() / f"m-{uuid.uuid4().hex[:8]}"
        repo = new_git_repo("generic-python")
        config_path = root / "run.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            '{"version":1,"project":"generic-python","repository":' +
            __import__("json").dumps(str(repo)) +
            ',"objective":"Write a plain Python greeting and test it.","token_budget":30000,"time_budget_seconds":600}',
            encoding="utf-8",
        )
        config = load_config(config_path, root / "state")
        runtime = Runtime(root / "state", codex="/usr/bin/true")
        client1, client2 = FakeRpc(), FakeRpc()
        clients = iter([client1, client2])

        async def fake_connect(*args, **kwargs):
            return next(clients)

        with patch("agentic_supervisor.runtime.version", new=AsyncMock(return_value="codex-cli 0.144.4")), \
             patch("agentic_supervisor.runtime.subprocess.run", return_value=subprocess.CompletedProcess(
                 ["codex", "login", "status"], 0, "Logged in", "")), \
             patch.object(runtime, "protocol_probe", new=AsyncMock(return_value={
                 "clients": 2, "shared_goal_status": "paused",
             })), \
             patch.object(runtime, "wait_for_worker", new=AsyncMock(return_value=None)), \
             patch.object(runtime, "wait_for_tui", new=AsyncMock(return_value=None)), \
             patch("agentic_supervisor.runtime.git_metadata", return_value=("deadbeef", False)), \
             patch.object(runtime, "tmux", return_value=subprocess.CompletedProcess(["tmux"], 0, "", "")) as tmux, \
             patch.object(runtime, "connect_when_ready", new=fake_connect), \
             patch("agentic_supervisor.runtime.asyncio.sleep", new=AsyncMock(return_value=None)):
            run_id = asyncio.run(runtime.start(config))

        row = runtime.store.get_run(run_id)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["thread_id"], "thread-fixture")
        self.assertEqual(row["git_head"], "deadbeef")
        self.assertEqual([method for method, _ in client1.calls], ["thread/start"])
        self.assertEqual([method for method, _ in client2.calls], ["thread/goal/set", "turn/start"])
        self.assertEqual(client2.calls[0][1]["tokenBudget"], 30000)
        self.assertEqual(client2.calls[1][1]["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertIn(str(repo), client2.calls[1][1]["sandboxPolicy"]["writableRoots"])
        self.assertTrue(any("supervisor" in call.args for call in tmux.call_args_list))
        worker_call = next(call for call in tmux.call_args_list
                           if "supervisor" in call.args and "worker" in str(call.args[-1]))
        self.assertIn("--runtime-root", worker_call.args[-1])
        self.assertIn("agentic_supervisor.worker", worker_call.args[-1])


if __name__ == "__main__":
    unittest.main()

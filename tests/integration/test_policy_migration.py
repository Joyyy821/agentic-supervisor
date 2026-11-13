"""Migration is exercised only against isolated SQLite and mocked native clients."""
from __future__ import annotations

import asyncio
import json
import subprocess
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from agentic_supervisor.config import load_config
from agentic_supervisor.runtime import Runtime, RuntimeErrorMessage, config_from_snapshot
from tests.unit.helpers import new_git_repo, test_root


class PolicyMigrationTests(unittest.TestCase):
    def setUp(self):
        root = test_root() / ("migrate-" + uuid.uuid4().hex[:8])
        root.mkdir()
        repo = new_git_repo("migration-repo")
        path = root / "config.json"
        path.write_text(json.dumps({"version": 1, "project": "generic", "repository": str(repo),
                                    "objective": "Continue research", "token_budget": 500,
                                    "milestone": {"name": "audit", "objective": "Audit first"}}))
        self.config = load_config(path, root / "s")
        self.runtime = Runtime(root / "s", "/usr/bin/true")
        self.run = self.runtime.store.create_run(self.config, "codex-cli 0.166.0", "rev", False)
        self.runtime.store.update_run(self.run, thread_id="existing-thread", status="paused",
                                      desired_status="paused", milestone_status="review")
        self.runtime.store.observe_token_usage(self.run, 400)
        self.pending = self.runtime.store.queue_control(self.run, "send", "retain this instruction")
        self.runtime.store.update_control(self.pending, "waiting_resume")
        with self.runtime.store.connect() as db:
            snapshot = json.loads(db.execute("SELECT config_json FROM runs WHERE id=?", (self.run,)).fetchone()[0])
            snapshot.pop("token_budget_mode")
            snapshot.pop("auto_resume_on_usage_reset")
            db.execute("UPDATE runs SET config_json=? WHERE id=?", (json.dumps(snapshot), self.run))

    def tmux(self, *args, check=True):
        return subprocess.CompletedProcess(args, 0, "1\n" if args[0] == "list-panes" else "", "")

    def test_migration_preserves_thread_usage_review_and_queued_input(self):
        self.runtime.tmux = self.tmux
        self.runtime._check_policy_native = AsyncMock()
        with patch("agentic_supervisor.runtime.version", new=AsyncMock(return_value="codex-cli 0.166.0")):
            self.runtime.migrate_policy(self.run, True, True)
        row = self.runtime.store.get_run(self.run)
        snapshot = json.loads(row["config_json"])
        self.assertEqual((row["thread_id"], row["tokens_used"], row["milestone_status"]),
                         ("existing-thread", 400, "review"))
        self.assertEqual(snapshot["token_budget_mode"], "advisory")
        self.assertTrue(snapshot["auto_resume_on_usage_reset"])
        self.assertEqual(next(c for c in self.runtime.store.controls(self.run) if c["id"] == self.pending)["status"], "waiting_resume")
        self.assertIsNotNone(self.runtime.store.latest_event(self.run, "supervisor/policy_migrated"))
        self.assertEqual(config_from_snapshot(snapshot).token_budget_mode, "advisory")
        with self.assertRaises(RuntimeErrorMessage):
            self.runtime.submit(self.run, "resume")  # review remains a human gate

        self.runtime.store.set_milestone(self.run, "implementation", "Continue after review")
        async def resume_same_thread():
            client = AsyncMock()
            client.call.side_effect = [{"goal": {"tokensUsed": 400}}, {"goal": {"tokensUsed": 0}},
                                       {"turn": {"id": "next-turn"}}, {}]
            self.assertEqual(await self.runtime.activate(client, self.run, config_from_snapshot(snapshot), "continue"), "next-turn")
            self.assertIsNone(client.call.call_args_list[1].args[1]["tokenBudget"])
            self.assertEqual(client.call.call_args_list[2].args[1]["threadId"], "existing-thread")
            self.assertEqual(self.runtime.store.observe_token_usage(self.run, 5)[0], 405)
        asyncio.run(resume_same_thread())

    def test_legacy_cap_remains_until_explicit_migration(self):
        row = self.runtime.store.get_run(self.run)
        config = config_from_snapshot(json.loads(row["config_json"]))
        self.assertEqual(config.token_budget_mode, "legacy_hard_limit")
        async def scenario():
            client = AsyncMock()
            client.call.side_effect = [{"goal": {"tokensUsed": 400}}, {"goal": {"tokensUsed": 400}},
                                       {"turn": {"id": "t"}}, {}]
            await self.runtime.activate(client, self.run, config, "continue")
            self.assertEqual(client.call.call_args_list[1].args[1]["tokenBudget"], 500)
        asyncio.run(scenario())

    def test_migration_rejects_live_worker(self):
        self.runtime.tmux = lambda *args, check=True: subprocess.CompletedProcess(args, 0, "0\n", "")
        with self.assertRaises(RuntimeErrorMessage):
            self.runtime.migrate_policy(self.run, True, True)
        row = self.runtime.store.get_run(self.run)
        self.assertNotIn("token_budget_mode", json.loads(row["config_json"]))

    def test_unfinished_native_effect_prevents_migration(self):
        self.runtime.store.add_event(self.run, "item/started", {"type": "commandExecution"}, "job-1")
        self.runtime.tmux = self.tmux
        with self.assertRaises(RuntimeErrorMessage):
            self.runtime.migrate_policy(self.run, True, True)
        self.assertNotIn("token_budget_mode", json.loads(self.runtime.store.get_run(self.run)["config_json"]))

    def test_native_pending_approval_blocks_migration(self):
        async def scenario():
            client = AsyncMock()
            client.call.side_effect = [{"thread": {"id": "existing-thread", "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}},
                                       {"goal": {"status": "paused"}}]
            self.runtime.connect_when_ready = AsyncMock(return_value=client)
            with self.assertRaises(RuntimeErrorMessage):
                await self.runtime._check_policy_native(self.runtime.store.get_run(self.run))
            client.close.assert_awaited_once()
        asyncio.run(scenario())

    def test_recovery_preserves_completed_goal_at_review(self):
        async def scenario():
            client = AsyncMock()
            client.call.side_effect = [{"thread": {"turns": []}},
                                       {"goal": {"status": "complete", "tokensUsed": 400}}]
            self.runtime.connect_when_ready = AsyncMock(return_value=client)
            row = self.runtime.store.get_run(self.run)
            _, status, observed = await self.runtime._reconcile_native(
                self.runtime.app_socket(self.run), row, self.config, self.runtime.root / "runs" / self.run)
            self.assertEqual((status, observed), ("complete", "paused"))
            self.assertEqual([c.args[0] for c in client.call.call_args_list], ["thread/resume", "thread/goal/get"])
        asyncio.run(scenario())

    def test_native_policy_check_is_read_only_for_idle_review(self):
        async def scenario():
            client = AsyncMock()
            client.call.side_effect = [{"thread": {"id": "existing-thread", "status": {"type": "idle"}}},
                                       {"goal": {"status": "complete"}}]
            self.runtime.connect_when_ready = AsyncMock(return_value=client)
            await self.runtime._check_policy_native(self.runtime.store.get_run(self.run))
            self.assertEqual([c.args[0] for c in client.call.call_args_list], ["thread/read", "thread/goal/get"])
        asyncio.run(scenario())

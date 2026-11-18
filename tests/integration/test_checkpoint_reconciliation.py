"""Late native work is reconciled without authorizing or replaying it."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
import subprocess
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from agentic_supervisor.config import load_config
from agentic_supervisor.cli import _print_status
from agentic_supervisor.reconciliation import load_checkpoint
from agentic_supervisor.runtime import Runtime, RuntimeErrorMessage, WorkerAlreadyRunning, acquire_worker_lock
from tests.unit.helpers import new_git_repo, test_root


class CheckpointReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.root = test_root() / ("checkpoint-" + uuid.uuid4().hex[:8])
        self.root.mkdir()
        self.repo = new_git_repo("checkpoint-repo")
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({"version": 1, "project": "generic", "repository": str(self.repo),
            "objective": "Long-running research", "milestone": {"name": "audit", "objective": "Inspect"},
            "checks": [{"name": "old-audit-check", "command": ["true"]}]}))
        self.config = load_config(config_path, self.root / "state")
        self.runtime = Runtime(self.config.runtime_root, "/usr/bin/true")
        self.run = self.runtime.store.create_run(self.config, "codex-cli 0.166.0", "rev", False)
        self.runtime.store.update_run(self.run, thread_id="same-thread", status="recovering", desired_status="paused",
                                      milestone_status="review")
        self.runtime.store.observe_token_usage(self.run, 400)
        self.evidence = self.root / "report.md"
        self.evidence.write_text("Prior audit completed; implementation awaits review.\n")
        self.manifest = self.root / "checkpoint.json"
        self.manifest.write_text(json.dumps({"version": 1, "milestone": {"name": "implementation", "objective": "Review results"},
            "completed_milestones": [{"name": "audit", "evidence": [str(self.evidence)]}],
            "evidence": [str(self.evidence)], "retire_checks": True}))
        self.runtime.tmux = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "1\n", "")
        self.version = patch("agentic_supervisor.runtime.version", AsyncMock(return_value="codex-cli 0.166.0"))
        self.version.start()
        self.addCleanup(self.version.stop)

    def client(self, *, status="idle", goal="paused", turns=None):
        client = AsyncMock()
        client.dropped_notifications = 0
        client.notifications = asyncio.Queue()
        client.call.side_effect = [
            {"thread": {"id": "same-thread", "cwd": str(self.repo), "status": {"type": status}}},
            {"goal": {"threadId": "same-thread", "status": goal, "tokensUsed": 400}},
            {"data": turns if turns is not None else [{"id": "last-turn", "status": "completed"}]}]
        self.runtime.connect_when_ready = AsyncMock(return_value=client)
        return client

    def test_late_checkpoint_policy_and_worker_recovery_keep_review(self):
        client = self.client()
        self.runtime.reconcile_checkpoint(self.run, self.manifest)
        self.assertEqual([c.args[0] for c in client.call.call_args_list],
                         ["thread/read", "thread/goal/get", "thread/turns/list"])
        row = self.runtime.store.get_run(self.run)
        self.assertEqual((row["status"], row["desired_status"], row["milestone_status"]), ("paused", "paused", "review"))
        self.assertEqual((row["thread_id"], row["tokens_used"], row["native_tokens_seen"]), ("same-thread", 400, 400))
        snapshot = json.loads(row["config_json"])
        self.assertEqual(snapshot["milestone"]["name"], "implementation")
        self.assertEqual(snapshot["checks"], [])
        output = io.StringIO()
        with redirect_stdout(output):
            _print_status(self.runtime, self.run)
        self.assertTrue(json.loads(output.getvalue())["workflow_checkpoint"]["recorded_late"])
        event = self.runtime.store.latest_event(self.run, "supervisor/checkpoint_reconciled")
        self.assertTrue(json.loads(event["payload_json"])["retired_checks"])
        self.assertEqual(self.runtime.store.attempts(self.run), [])
        with self.assertRaises(RuntimeErrorMessage):
            self.runtime.submit(self.run, "resume")
        self.runtime._check_policy_native = AsyncMock()
        self.runtime.migrate_policy(self.run, True, True)
        self.runtime._reconcile_native = AsyncMock(return_value=([], "paused", "paused"))
        self.runtime.recover(self.run)
        recovered = Runtime(self.config.runtime_root, "/usr/bin/true")
        row = recovered.store.get_run(self.run)
        self.assertEqual((row["thread_id"], row["status"], row["milestone_status"]), ("same-thread", "paused", "review"))
        self.assertTrue(json.loads(row["config_json"])["auto_resume_on_usage_reset"])
        with self.assertRaises(RuntimeErrorMessage):
            recovered.submit(self.run, "resume")
        self.assertTrue((self.runtime.root / "lifecycle.lock").exists())

    def test_repeated_checkpoint_is_idempotent(self):
        for _ in range(2):
            self.client()
            self.runtime.reconcile_checkpoint(self.run, self.manifest)
        self.assertEqual(self.runtime.store.get_run(self.run)["milestone_generation"], 1)
        self.assertEqual(sum(e["kind"] == "supervisor/checkpoint_reconciled"
                             for e in self.runtime.store.events(self.run)), 1)

    def test_manual_active_intent_or_terminal_state_cannot_be_overwritten(self):
        for fields in [{"desired_status": "active"}, {"status": "stopped", "desired_status": "paused"}]:
            self.runtime.store.update_run(self.run, **fields)
            with self.assertRaises(RuntimeErrorMessage):
                self.runtime.reconcile_checkpoint(self.run, self.manifest)

    def test_active_native_goal_approval_or_turn_is_refused(self):
        for kwargs in [{"status": "active"}, {"goal": "active"},
                       {"turns": [{"id": "in-flight", "status": "inProgress"}]}]:
            self.client(**kwargs)
            with self.assertRaises(RuntimeErrorMessage):
                self.runtime.reconcile_checkpoint(self.run, self.manifest)
            self.assertEqual(self.runtime.store.get_run(self.run)["status"], "recovering")
        client = self.client()
        client.notifications.put_nowait({"id": 7, "method": "item/commandExecution/requestApproval"})
        with self.assertRaises(RuntimeErrorMessage):
            self.runtime.reconcile_checkpoint(self.run, self.manifest)

    def test_uncertain_or_unfinished_work_is_not_replayed(self):
        control = self.runtime.store.queue_control(self.run, "resume", "")
        self.client()
        with self.assertRaises(ValueError):
            self.runtime.reconcile_checkpoint(self.run, self.manifest)
        self.runtime.store.update_control(control, "confirmed")
        self.runtime.store.add_event(self.run, "item/started", {"type": "commandExecution"}, "unfinished")
        self.client()
        with self.assertRaises(ValueError):
            self.runtime.reconcile_checkpoint(self.run, self.manifest)
        self.assertEqual(self.runtime.store.attempts(self.run), [])

    def test_real_worker_lock_refuses_even_if_tmux_worker_is_dead(self):
        fd = acquire_worker_lock(self.runtime.root)
        try:
            with self.assertRaises(WorkerAlreadyRunning):
                self.runtime.reconcile_checkpoint(self.run, self.manifest)
        finally:
            os.close(fd)

    def test_missing_evidence_cannot_be_recorded_as_completed(self):
        self.evidence.unlink()
        with self.assertRaises(FileNotFoundError):
            load_checkpoint(self.manifest)

    def test_uncertain_attempt_remains_unresolved(self):
        attempt = self.runtime.store.create_attempt(self.run, "external", ["true"])
        self.runtime.store.mark_interrupted_work_unknown(self.run)
        self.client()
        with self.assertRaises(ValueError):
            self.runtime.reconcile_checkpoint(self.run, self.manifest)
        self.assertEqual(self.runtime.store.attempt(self.run, attempt)["status"], "outcome_unknown")


if __name__ == "__main__":
    unittest.main()

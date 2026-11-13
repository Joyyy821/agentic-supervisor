"""Quota recovery is tested with fake App Server replies, never by draining quota."""

from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from unittest.mock import AsyncMock

from agentic_supervisor.config import load_config
from agentic_supervisor.codex import _compact_event
from agentic_supervisor.runtime import Runtime, _enter_usage_wait, _event_summary, _poll_usage_wait
from agentic_supervisor.usage import is_provider_limit_error, rate_limit_state
from tests.unit.helpers import new_git_repo, test_root


def limited(reset: int | None = None, *, allowed: bool | None = False):
    return {"ordinaryUsageAllowed": allowed, "rateLimits": {
        "limitId": "codex", "rateLimitReachedType": "rate_limit_reached",
        "primary": {"usedPercent": 100, "resetsAt": reset}}}


class UsageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = test_root() / ("quota-" + uuid.uuid4().hex[:8])
        self.root.mkdir()
        self.repo = new_git_repo("quota-repo")
        path = self.root / "config.json"
        path.write_text(json.dumps({"version": 1, "project": "plain-python", "repository": str(self.repo),
                                    "objective": "Complete two stages", "runtime_root": str(self.root / "s"),
                                    "auto_resume_on_usage_reset": True,
                                    "milestone": {"name": "analysis", "objective": "Inspect code"}}))
        self.config = load_config(path)
        self.runtime = Runtime(self.config.runtime_root, "/usr/bin/true")
        self.run = self.runtime.store.create_run(self.config, "codex-cli 0.166.0", "revision", False)
        self.runtime.store.update_run(self.run, thread_id="thread-same", status="active")

    def enter(self):
        client = AsyncMock()
        client.call.side_effect = [limited(2000000000), {"goal": {"updatedAt": 100}}]
        self.assertTrue(asyncio.run(_enter_usage_wait(self.runtime, client, self.run,
                                                      self.config, "failed-turn", "usageLimitExceeded")))
        self.assertEqual(self.runtime.store.get_run(self.run)["status"], "usage_wait")
        return client

    def test_wait_survives_worker_restart_and_resumes_same_thread_once(self):
        self.enter()
        recovered = Runtime(self.config.runtime_root, "/usr/bin/true")
        recovered.store.update_usage_wait(self.run, next_check_at=0)
        blocked = AsyncMock()
        blocked.call.return_value = limited(2000000000)
        self.assertIsNone(asyncio.run(_poll_usage_wait(recovered, blocked, self.run, self.config)))
        self.assertEqual(recovered.store.usage_wait(self.run)["checks"], 1)
        recovered.store.update_usage_wait(self.run, next_check_at=0)
        ready = AsyncMock()
        ready.call.side_effect = [limited(2000000000, allowed=True),
                                  {"thread": {"turns": [{"id": "failed-turn", "status": "failed"}]}},
                                  {"goal": {"status": "paused", "updatedAt": 100}}]
        async def activate(client, run_id, config, prompt, origin):
            self.assertEqual(recovered.store.get_run(run_id)["thread_id"], "thread-same")
            self.assertEqual(origin, "automatic")
            recovered.store.update_run(run_id, status="active")
            return "continued-turn"
        recovered.activate = AsyncMock(side_effect=activate)
        self.assertEqual(asyncio.run(_poll_usage_wait(recovered, ready, self.run, self.config)), "continued-turn")
        self.assertEqual(recovered.store.usage_wait(self.run)["state"], "resumed")
        self.assertEqual(asyncio.run(_poll_usage_wait(recovered, ready, self.run, self.config)), None)
        recovered.activate.assert_awaited_once()
        self.assertEqual(recovered.store.get_run(self.run)["thread_id"], "thread-same")

    def test_manual_pause_cancels_wait(self):
        self.enter()
        self.runtime.submit(self.run, "pause")
        self.assertEqual(self.runtime.store.usage_wait(self.run)["state"], "cancelled")
        self.assertEqual(self.runtime.store.get_run(self.run)["desired_status"], "paused")

    def test_review_and_uncertain_effects_block_auto_resume(self):
        self.runtime.store.update_run(self.run, milestone_status="review")
        client = AsyncMock()
        self.assertFalse(asyncio.run(_enter_usage_wait(self.runtime, client, self.run,
                                                       self.config, "failed-turn", "usageLimitExceeded")))
        client.call.assert_not_awaited()
        self.runtime.store.update_run(self.run, milestone_status="active")
        self.runtime.store.add_event(self.run, "item/started", {"type": "commandExecution"}, "effect")
        self.assertTrue(asyncio.run(_enter_usage_wait(self.runtime, client, self.run,
                                                      self.config, "failed-turn", "usageLimitExceeded")))
        self.assertEqual(self.runtime.store.get_run(self.run)["status"], "blocked")
        self.assertTrue(self.runtime.store.has_uncertain_work(self.run))

    def test_native_manual_goal_change_cancels_recovery(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        client = AsyncMock()
        client.call.side_effect = [limited(2000000000, allowed=True),
                                   {"thread": {"turns": [{"id": "failed-turn", "status": "failed"}]}},
                                   {"goal": {"status": "paused", "updatedAt": 101}}]
        self.assertIsNone(asyncio.run(_poll_usage_wait(self.runtime, client, self.run, self.config)))
        self.assertEqual(self.runtime.store.get_run(self.run)["desired_status"], "paused")

    def test_approval_blocked_native_goal_never_starts_turn(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        client = AsyncMock()
        client.call.side_effect = [limited(2000000000, allowed=True),
                                   {"thread": {"turns": [{"id": "failed-turn", "status": "failed"}]}},
                                   {"goal": {"status": "blocked", "updatedAt": 100}}]
        self.assertIsNone(asyncio.run(_poll_usage_wait(self.runtime, client, self.run, self.config)))
        self.assertEqual(self.runtime.store.get_run(self.run)["status"], "blocked")
        self.assertNotIn("turn/start", [call.args[0] for call in client.call.call_args_list])

    def test_credit_depletion_stops_polling_without_model_request(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        snapshot = limited()
        snapshot["rateLimits"]["rateLimitReachedType"] = "workspace_member_credits_depleted"
        client = AsyncMock()
        client.call.return_value = snapshot
        self.assertIsNone(asyncio.run(_poll_usage_wait(self.runtime, client, self.run, self.config)))
        self.assertEqual(self.runtime.store.get_run(self.run)["status"], "blocked")
        self.assertEqual(client.call.await_count, 1)

    def test_error_summary_and_limit_classification(self):
        summary, _ = _event_summary("turn/completed", {"turn": {"id": "x", "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded", "message": "private"}}})
        self.assertEqual(summary["error_code"], "usageLimitExceeded")
        self.assertNotIn("private", json.dumps(summary))
        self.assertTrue(is_provider_limit_error("usageLimitExceeded"))
        self.assertFalse(is_provider_limit_error("contextWindowExceeded"))
        self.assertFalse(is_provider_limit_error("sessionBudgetExceeded"))
        self.assertFalse(is_provider_limit_error("unauthorized"))
        compact = _compact_event({"turn": {"id": "x", "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded", "message": "private"}}})
        self.assertEqual(compact["turn"]["error"]["codexErrorInfo"], "usageLimitExceeded")
        self.assertNotIn("private", json.dumps(compact))
        self.assertEqual(rate_limit_state(limited(2000000000)),
                         (False, "rate_limit_reached", 2000000000.0))
        denied = limited()
        denied["rateLimits"]["rateLimitReachedType"] = "workspace_owner_credits_depleted"
        self.assertEqual(rate_limit_state(denied), (False, None, None))


if __name__ == "__main__":
    unittest.main()

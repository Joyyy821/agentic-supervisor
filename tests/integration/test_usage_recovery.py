"""Quota recovery is tested with fake App Server replies, never by draining quota."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
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


def run_fake_worker(root: str, run_id: str):
    """A real worker process with only its transport replaced by an inert fake."""
    import agentic_supervisor.runtime as module
    os.environ["AGENTIC_SUPERVISOR_CODEX"] = "/usr/bin/true"

    class Client:
        def __init__(self, *args, **kwargs):
            self.dropped_notifications = 0
        async def connect(self, _name):
            pass
        async def call(self, method, params, **kwargs):
            if method == "thread/resume":
                return {"thread": {"status": {"type": "idle"},
                                   "turns": [{"id": "failed-turn", "status": "failed", "items": []}]}}
            if method == "thread/goal/get":
                return {"goal": {"status": "paused", "tokensUsed": 0}}
            raise AssertionError("unexpected model or mutation request: " + method)
        async def next_event(self, timeout):
            await asyncio.sleep(.05)
            raise asyncio.TimeoutError
        async def close(self):
            pass

    module.RpcClient = Client
    asyncio.run(module.run_worker(Path(root), run_id))


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
        client.call.side_effect = [limited(2000000000), {"goal": {"status": "paused", "updatedAt": 100}}]
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
                                  {"thread": {"status": {"type": "idle"}, "turns": [{"id": "failed-turn", "status": "failed"}]}},
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

    def test_steering_is_queued_during_usage_wait(self):
        self.enter()
        control_id = self.runtime.submit(self.run, "send", "preserve this instruction")
        control = next(c for c in self.runtime.store.controls(self.run) if c["id"] == control_id)
        self.assertEqual(control["status"], "waiting_resume")
        self.assertEqual(self.runtime.store.pending_controls(self.run), [])

    def test_pause_during_quota_read_prevents_dispatch(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        async def scenario():
            read_started, finish_read = asyncio.Event(), asyncio.Event()
            client = AsyncMock()
            async def call(method, params, **kwargs):
                if method == "account/rateLimits/read":
                    read_started.set()
                    await finish_read.wait()
                    return limited(allowed=True)
                if method == "thread/resume":
                    return {"thread": {"status": {"type": "idle"},
                                       "turns": [{"id": "failed-turn", "status": "failed"}]}}
                return {"goal": {"status": "paused", "updatedAt": 100}}
            client.call.side_effect = call
            self.runtime.activate = AsyncMock()
            task = asyncio.create_task(_poll_usage_wait(self.runtime, client, self.run, self.config))
            await read_started.wait()
            self.runtime.submit(self.run, "pause")
            finish_read.set()
            await task
            self.runtime.activate.assert_not_awaited()
            self.assertEqual(self.runtime.store.get_run(self.run)["desired_status"], "paused")
        asyncio.run(scenario())

    def test_pending_native_approval_does_not_continue(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        client = AsyncMock()
        client.call.side_effect = [limited(allowed=True),
            {"thread": {"status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
                        "turns": [{"id": "approval-turn", "status": "inProgress"}]}},
            {"goal": {"status": "active", "updatedAt": 100}}]
        self.runtime.activate = AsyncMock()
        self.assertIsNone(asyncio.run(_poll_usage_wait(self.runtime, client, self.run, self.config)))
        self.runtime.activate.assert_not_awaited()
        self.assertIn("approval", self.runtime.store.usage_wait(self.run)["last_error"])

    def test_uncertain_dispatch_is_not_replayed(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        client = AsyncMock()
        client.call.side_effect = [limited(allowed=True),
            {"thread": {"status": {"type": "idle"}, "turns": [{"id": "failed-turn", "status": "failed"}]}},
            {"goal": {"status": "paused", "updatedAt": 100}}]
        self.runtime.activate = AsyncMock(side_effect=asyncio.TimeoutError())
        self.assertIsNone(asyncio.run(_poll_usage_wait(self.runtime, client, self.run, self.config)))
        recovered = Runtime(self.config.runtime_root, "/usr/bin/true")
        recovered.activate = AsyncMock()
        self.assertIsNone(asyncio.run(_poll_usage_wait(recovered, client, self.run, self.config)))
        recovered.activate.assert_not_awaited()
        self.assertTrue(recovered.store.has_uncertain_work(self.run))
        self.assertEqual(recovered.store.usage_wait(self.run)["state"], "outcome_unknown")

    def test_lost_turn_start_ack_is_dispatched_at_most_once(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        client = AsyncMock()
        async def call(method, params, **kwargs):
            if method == "account/rateLimits/read":
                return limited(allowed=True)
            if method == "thread/resume":
                return {"thread": {"status": {"type": "idle"},
                                   "turns": [{"id": "failed-turn", "status": "failed"}]}}
            if method == "thread/goal/get":
                return {"goal": {"status": "paused", "updatedAt": 100, "tokensUsed": 0}}
            if method == "thread/goal/set":
                return {"goal": {"tokensUsed": 0}}
            if method == "turn/start":
                raise asyncio.TimeoutError("accepted by server, acknowledgement lost")
            raise AssertionError(method)
        client.call.side_effect = call
        asyncio.run(_poll_usage_wait(self.runtime, client, self.run, self.config))
        recovered = Runtime(self.config.runtime_root, "/usr/bin/true")
        asyncio.run(_poll_usage_wait(recovered, client, self.run, self.config))
        self.assertEqual(sum(c.args[0] == "turn/start" for c in client.call.call_args_list), 1)
        control = next(c for c in recovered.store.controls(self.run) if c["kind"] == "auto_resume")
        self.assertEqual(control["status"], "outcome_unknown")

    def test_pause_is_serialized_with_automatic_dispatch(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        async def scenario():
            started, finish = asyncio.Event(), asyncio.Event()
            client = AsyncMock()
            client.call.side_effect = [limited(allowed=True),
                {"thread": {"status": {"type": "idle"}, "turns": [{"id": "failed-turn", "status": "failed"}]}},
                {"goal": {"status": "paused", "updatedAt": 100}}]
            async def activate(*args, **kwargs):
                started.set()
                await finish.wait()
                self.runtime.store.update_run(self.run, status="active", desired_status="active")
                return "new-turn"
            self.runtime.activate = AsyncMock(side_effect=activate)
            poll = asyncio.create_task(_poll_usage_wait(self.runtime, client, self.run, self.config))
            done, _ = await asyncio.wait({poll, asyncio.create_task(started.wait())},
                                         timeout=3, return_when=asyncio.FIRST_COMPLETED)
            if poll in done:
                self.fail(f"poll ended before dispatch: {poll.exception()!r}; wait={dict(self.runtime.store.usage_wait(self.run))}")
            self.assertTrue(started.is_set(), "dispatch did not start")
            pause = subprocess.Popen([sys.executable, "-c",
                "from pathlib import Path; from agentic_supervisor.runtime import Runtime; import sys; "
                "Runtime(Path(sys.argv[1]), '/usr/bin/true').submit(sys.argv[2], 'pause')",
                str(self.config.runtime_root), self.run], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await asyncio.sleep(.05)
            self.assertIsNone(pause.poll())
            finish.set()
            await poll
            _, stderr = pause.communicate(timeout=5)
            self.assertEqual(pause.returncode, 0, stderr.decode())
            self.assertEqual(self.runtime.store.get_run(self.run)["desired_status"], "paused")
        asyncio.run(scenario())

    def test_worker_restart_after_claim_never_replays_dispatch(self):
        self.enter()
        control_id = self.runtime.store.claim_usage_dispatch(self.run)
        self.assertIsNotNone(control_id)
        recovered = Runtime(self.config.runtime_root, "/usr/bin/true")
        client = AsyncMock()
        asyncio.run(_poll_usage_wait(recovered, client, self.run, self.config))
        client.call.assert_not_awaited()
        self.assertEqual(recovered.store.usage_wait(self.run)["state"], "outcome_unknown")
        self.assertTrue(recovered.store.has_uncertain_work(self.run))

    def test_wait_survives_actual_worker_process_restart(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=time.time() + 3600)
        context = multiprocessing.get_context("fork")
        def attachments():
            return sum(e["kind"] == "supervisor/worker_attached" for e in self.runtime.store.events(self.run))
        for expected in (1, 2):
            process = context.Process(target=run_fake_worker, args=(str(self.config.runtime_root), self.run))
            process.start()
            try:
                deadline = time.monotonic() + 8
                while attachments() < expected and process.is_alive() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(process.is_alive(), "fake worker exited before attaching")
                self.assertGreaterEqual(attachments(), expected)
                self.assertEqual(self.runtime.store.get_run(self.run)["thread_id"], "thread-same")
                self.assertEqual(self.runtime.store.usage_wait(self.run)["state"], "waiting")
            finally:
                process.terminate()
                process.join(timeout=5)
                self.assertFalse(process.is_alive())

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
                                   {"thread": {"status": {"type": "idle"}, "turns": [{"id": "failed-turn", "status": "failed"}]}},
                                   {"goal": {"status": "paused", "updatedAt": 101}}]
        self.assertIsNone(asyncio.run(_poll_usage_wait(self.runtime, client, self.run, self.config)))
        self.assertEqual(self.runtime.store.get_run(self.run)["desired_status"], "paused")

    def test_approval_blocked_native_goal_never_starts_turn(self):
        self.enter()
        self.runtime.store.update_usage_wait(self.run, next_check_at=0)
        client = AsyncMock()
        client.call.side_effect = [limited(2000000000, allowed=True),
                                   {"thread": {"status": {"type": "idle"}, "turns": [{"id": "failed-turn", "status": "failed"}]}},
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

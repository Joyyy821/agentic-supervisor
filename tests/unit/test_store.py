from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from agentic_supervisor.config import load_config
from agentic_supervisor.store import Store
from tests.unit.helpers import new_git_repo, test_root


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = test_root() / f"store-{uuid.uuid4()}"
        self.repo = new_git_repo("store-repo")
        cfg = self.root / "project.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"version":1,"project":"fixture","repository":' +
                       __import__("json").dumps(str(self.repo)) +
                       ',"objective":"Test persistence"}', encoding="utf-8")
        self.config = load_config(cfg, self.root / "runtime")
        self.store = Store(self.root / "runtime")
        self.run_id = self.store.create_run(self.config, "codex-cli 0.144.4", "abc123", False)

    def test_records_run_snapshot_and_controls(self) -> None:
        row = self.store.get_run(self.run_id)
        self.assertEqual(row["status"], "starting")
        self.assertIn("Test persistence", row["config_json"])
        control_id = self.store.queue_control(self.run_id, "pause", "")
        self.assertEqual(self.store.pending_controls(self.run_id)[0]["id"], control_id)
        self.store.update_control(control_id, "confirmed", "paused")
        self.assertEqual(self.store.pending_controls(self.run_id), [])

    def test_event_deduplication_preserves_distinct_events(self) -> None:
        self.store.add_event(self.run_id, "turn/started", {"turn": {"id": "t1"}}, "t1")
        self.store.add_event(self.run_id, "turn/started", {"turn": {"id": "t1"}}, "t1")
        self.store.add_event(self.run_id, "turn/completed", {"turn": {"id": "t1"}}, "t1")
        self.assertEqual(len(self.store.events(self.run_id)), 2)

    def test_waiting_queue_released_at_turn_boundary(self) -> None:
        control_id = self.store.queue_control(self.run_id, "queue", "continue")
        self.store.update_control(control_id, "waiting_turn", "active")
        self.assertEqual(self.store.pending_controls(self.run_id), [])
        self.store.release_waiting_controls(self.run_id)
        self.assertEqual(len(self.store.pending_controls(self.run_id)), 1)

    def test_attempt_requires_explicit_resolution(self) -> None:
        attempt_id = self.store.create_attempt(self.run_id, "effect", ["python3", "job.py"])
        self.store.resolve_attempt(self.run_id, attempt_id, "outcome_unknown", "worker restarted")
        attempt = self.store.attempts(self.run_id)[0]
        self.assertEqual(attempt["status"], "outcome_unknown")
        self.assertIn("worker restarted", attempt["result_json"])

    def test_interrupted_work_is_marked_unknown_and_never_requeued(self) -> None:
        control_id = self.store.queue_control(self.run_id, "send", "continue")
        self.store.update_control(control_id, "dispatched")
        attempt_id = self.store.create_attempt(self.run_id, "experiment", ["python3", "run.py"])
        self.assertEqual(self.store.mark_interrupted_work_unknown(self.run_id), {"attempts": 1, "controls": 1})
        self.assertEqual(self.store.pending_controls(self.run_id), [])
        self.assertEqual(self.store.attempts(self.run_id)[0]["status"], "outcome_unknown")

    def test_uncertain_control_resolution_records_evidence_without_requeue(self) -> None:
        control_id = self.store.queue_control(self.run_id, "stop", "")
        self.store.update_control(control_id, "outcome_unknown")
        self.store.resolve_control(self.run_id, control_id, "not_applied", "tmux session still exists")
        control = next(item for item in self.store.controls(self.run_id) if item["id"] == control_id)
        self.assertEqual(control["status"], "not_applied")
        self.assertIn("tmux session still exists", control["detail"])
        self.assertEqual(self.store.pending_controls(self.run_id), [])

    def test_token_ledger_accumulates_after_native_goal_budget_reset(self) -> None:
        self.assertEqual(self.store.observe_token_usage(self.run_id, 100), (100, 100))
        self.assertEqual(self.store.observe_token_usage(self.run_id, 125), (125, 25))
        self.assertEqual(self.store.observe_token_usage(self.run_id, 4), (129, 4))
        self.assertEqual(self.store.observe_token_usage(self.run_id, 4), (129, 0))


if __name__ == "__main__":
    unittest.main()

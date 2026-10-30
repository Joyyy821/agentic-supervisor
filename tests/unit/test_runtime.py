from __future__ import annotations

import json
import os
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from agentic_supervisor.config import load_config
from agentic_supervisor.runtime import (Runtime, WorkerAlreadyRunning, _event_summary, _sanitize,
                                        acquire_worker_lock, git_checkpoint)
from tests.unit.helpers import new_git_repo, test_root


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = test_root() / f"runtime-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(mode=0o700)
        self.repo = new_git_repo("runtime-fixture")
        self.config_path = self.root / "project.json"
        self.config_path.write_text(json.dumps({"version": 1, "project": "fixture",
                                                "repository": str(self.repo), "objective": "inspect generic code"}),
                                    encoding="utf-8")
        self.config = load_config(self.config_path, self.root / "state")
        self.runtime = Runtime(self.root / "state", codex="/usr/bin/true")

    def test_events_keep_operational_metadata_without_transcript_text(self):
        summary, native_id = _event_summary("item/agentMessage/delta", {
            "turn": {"id": "turn-1", "status": "inProgress"},
            "delta": "private reasoning",
            "api_key": "do-not-store",
        })
        self.assertIsNone(native_id)
        self.assertNotIn("private reasoning", json.dumps(summary))
        self.assertEqual(summary["delta_length"], len("private reasoning"))
        cleaned = _sanitize({"tokenBudget": 100, "tokensUsed": 12, "access_token": "secret"})
        self.assertEqual(cleaned["tokenBudget"], 100)
        self.assertEqual(cleaned["tokensUsed"], 12)
        self.assertEqual(cleaned["access_token"], "<redacted>")

    def test_send_while_paused_is_persisted_until_resume(self):
        run_id = self.runtime.store.create_run(self.config, "codex-cli 0.144.4", "abc", False)
        self.runtime.store.update_run(run_id, status="paused", desired_status="paused")
        control_id = self.runtime.submit(run_id, "send", "keep the API stable")
        control = next(c for c in self.runtime.store.controls(run_id) if c["id"] == control_id)
        self.assertEqual(control["status"], "waiting_resume")
        self.runtime.submit(run_id, "resume")
        self.assertEqual(self.runtime.store.get_run(run_id)["desired_status"], "active")

    def test_runtime_environment_values_are_not_embedded_in_tmux_command(self):
        command = self.runtime._owned_command("test-run", {"PATH": "/usr/bin", "API_KEY": "sensitive-value"},
                                              ["codex", "app-server"], self.root / "server.log")
        self.assertNotIn("sensitive-value", command)
        env_file = self.runtime.root / "runs" / "test-run" / "runtime.env"
        self.assertIn("sensitive-value", env_file.read_text(encoding="utf-8"))
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_git_checkpoint_tracks_dirty_paths_and_diff_hash(self):
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        (self.repo / "new.py").write_text("print('new')\n", encoding="utf-8")
        checkpoint = git_checkpoint(self.repo, timeout=5)
        self.assertEqual(checkpoint["clean"], False)
        self.assertIn("README.md", checkpoint["changed_paths"])
        self.assertIn("new.py", checkpoint["changed_paths"])
        self.assertGreater(checkpoint["tracked_diff_bytes"], 0)
        self.assertEqual(len(checkpoint["tracked_diff_sha256"]), 64)

    def test_worker_lock_prevents_duplicate_workers(self):
        first = acquire_worker_lock(self.root / "lock-test")
        try:
            with self.assertRaises(WorkerAlreadyRunning):
                acquire_worker_lock(self.root / "lock-test")
        finally:
            os.close(first)
        second = acquire_worker_lock(self.root / "lock-test")
        os.close(second)

    def test_recover_preserves_pause_and_marks_inflight_work_unknown(self):
        run_id = self.runtime.store.create_run(self.config, "codex-cli 0.144.4", "abc", False)
        self.runtime.store.update_run(run_id, thread_id="native-thread", status="recovering",
                                      desired_status="paused")
        control_id = self.runtime.store.queue_control(run_id, "send", "continue")
        self.runtime.store.update_control(control_id, "dispatched")
        attempt_id = self.runtime.store.create_attempt(run_id, "check", ["python3", "check.py"])
        self.runtime._reconcile_native = AsyncMock(return_value=([], "active", "paused"))
        calls = []

        def tmux(*args, check=True):
            calls.append(args)
            if args[0] == "has-session":
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "list-panes":
                pane_state = "1\n" if any(":supervisor" in str(arg) for arg in args) else "0\n"
                return subprocess.CompletedProcess(args, 0, pane_state, "")
            return subprocess.CompletedProcess(args, 0, "", "")

        self.runtime.tmux = tmux
        self.runtime.recover(run_id)
        row = self.runtime.store.get_run(run_id)
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["desired_status"], "paused")
        self.assertEqual(self.runtime.store.attempt(run_id, attempt_id)["status"], "outcome_unknown")
        self.assertEqual(self.runtime.store.pending_controls(run_id), [])
        self.runtime._reconcile_native.assert_awaited_once()
        self.assertTrue(any(call[0] == "respawn-window" and
                            any("supervisor" in str(arg) for arg in call) for call in calls))


if __name__ == "__main__":
    unittest.main()

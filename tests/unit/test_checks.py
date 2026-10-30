from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from pathlib import Path

from agentic_supervisor.checks import run_check
from agentic_supervisor.config import Check, load_config
from agentic_supervisor.store import Store
from tests.unit.helpers import new_git_repo, test_root


class FakeCheckRuntime:
    def __init__(self, result_path: Path):
        self.result_path = result_path
        self.calls = 0

    async def call(self, method, params, timeout=30):
        self.calls += 1
        self.assert_method = method
        self.result_path.write_text('{"passed":true,"count":3}', encoding="utf-8")
        return {"exitCode": 0, "stdout": "ok\n", "stderr": ""}


class CheckTests(unittest.TestCase):
    def test_successful_check_and_recovery_do_not_repeat_command(self):
        root = test_root() / f"check-{uuid.uuid4().hex[:8]}"
        root.mkdir(mode=0o700)
        repo = new_git_repo("check-fixture")
        result_path = repo / "result.json"
        config_path = root / "project.json"
        config_path.write_text(json.dumps({"version": 1, "project": "fixture", "repository": str(repo),
                                           "objective": "collect generic metrics", "checks": []}), encoding="utf-8")
        config = load_config(config_path, root / "runtime")
        store = Store(config.runtime_root)
        run_id = store.create_run(config, "codex-cli 0.144.4", "abc", False)
        runtime = FakeCheckRuntime(result_path)
        check = Check("metrics", ("python3", "evaluate.py"), repo, 60, (result_path,))
        output_dir = config.runtime_root / "runs" / run_id

        first = asyncio.run(run_check(runtime, store, run_id, check, output_dir))
        second = asyncio.run(run_check(runtime, store, run_id, check, output_dir))

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertTrue(second["reused_after_recovery"])
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(len(store.attempts(run_id)), 1)
        result = json.loads(store.attempts(run_id)[0]["result_json"])
        self.assertEqual(result["json_results"][str(result_path)]["count"], 3)
        self.assertTrue(Path(store.attempts(run_id)[0]["stdout_path"]).is_file())


if __name__ == "__main__":
    unittest.main()

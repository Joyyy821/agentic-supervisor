"""Opt-in real Codex smoke test; creates a new Git repository under the test root."""

from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
import uuid
from pathlib import Path

from agentic_supervisor.config import load_config
from agentic_supervisor.runtime import Runtime


@unittest.skipUnless(os.environ.get("AGENTIC_SUPERVISOR_LIVE_TEST") == "1",
                     "set AGENTIC_SUPERVISOR_LIVE_TEST=1 to run the bounded authenticated Codex test")
class LiveSmokeTests(unittest.TestCase):
    def test_plain_python_repository(self):
        test_root = Path(os.environ["AGENTIC_SUPERVISOR_TEST_ROOT"]).expanduser().resolve()
        fixture = test_root / f"live-python-{uuid.uuid4()}"
        fixture.mkdir(parents=True, mode=0o700)
        subprocess.run(["git", "init", "-q", str(fixture)], check=True)
        (fixture / "README.md").write_text("create greeting.py with a main function\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(fixture), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(fixture), "-c", "user.email=test@example.invalid",
                        "-c", "user.name=Smoke Test", "commit", "-qm", "fixture"], check=True)
        runtime_root = test_root / f"r-{uuid.uuid4().hex[:8]}"
        config_path = test_root / f"live-{uuid.uuid4()}.json"
        config_path.write_text(json.dumps({
            "version": 1, "project": "python-smoke", "repository": str(fixture),
            "objective": "Create greeting.py with a main function that prints exactly hello supervisor. "
                         "Do not change README.md.",
            "token_budget": 30000, "time_budget_seconds": 600,
            "checks": [{"name": "greeting", "command": ["python3", "-c",
                        "import subprocess,sys; p=subprocess.run([sys.executable,'greeting.py'],capture_output=True,text=True); "
                        "assert p.returncode==0 and p.stdout.strip()=='hello supervisor'"], "timeout_seconds": 20}]
        }), encoding="utf-8")
        runtime = Runtime(runtime_root)
        run_id = __import__("asyncio").run(runtime.start(load_config(config_path, runtime_root)))
        deadline = time.monotonic() + 610
        while time.monotonic() < deadline:
            row = runtime.store.get_run(run_id)
            if row["status"] in {"complete", "validation_failed", "blocked"}:
                break
            time.sleep(2)
        self.assertEqual(runtime.store.get_run(run_id)["status"], "complete")
        self.assertTrue((fixture / "greeting.py").is_file())


if __name__ == "__main__":
    unittest.main()

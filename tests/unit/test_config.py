from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from agentic_supervisor.config import ConfigError, load_config
from tests.unit.helpers import new_git_repo, test_root


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = new_git_repo("config")
        self.config_file = test_root() / f"config-{uuid.uuid4()}.json"
        self.data = {"version": 1, "project": "plain-python", "repository": str(self.repo),
                     "objective": "Fix the parser and run tests.", "context_files": [],
                     "token_budget": 30000, "time_budget_seconds": 600,
                     "environment_allowlist": [], "writable_paths": [], "checks": []}

    def load(self, **changes):
        self.config_file.write_text(json.dumps(self.data | changes), encoding="utf-8")
        return load_config(self.config_file, test_root() / f"runtime-{uuid.uuid4()}")

    def test_resolves_paths_and_defaults(self) -> None:
        config = self.load()
        self.assertEqual(config.repository, self.repo.resolve())
        self.assertEqual(config.approval_policy, "on-request")
        self.assertEqual(config.token_budget, 30000)
        self.assertEqual(config.safe_snapshot()["project"], "plain-python")

    def test_rejects_non_git_repository(self) -> None:
        outside = test_root() / f"plain-{uuid.uuid4()}"
        outside.mkdir()
        with self.assertRaises(ConfigError):
            self.load(repository=str(outside))

    def test_rejects_artifact_escape(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(writable_paths=["../../outside"])

    def test_rejects_shell_string_command(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(checks=[{"name": "tests", "command": "pytest -q"}])

    def test_validates_check_timeout_and_command(self) -> None:
        config = self.load(checks=[{"name": "unit", "command": ["python3", "-m", "unittest"],
                                   "timeout_seconds": 60, "result_files": ["result.json"]}])
        self.assertEqual(config.checks[0].cwd, self.repo.resolve())
        self.assertEqual(config.checks[0].command[0], "python3")
        with self.assertRaises(ConfigError):
            self.load(checks=[{"name": "unit", "command": ["pytest"], "timeout_seconds": 0}])

    def test_rejects_runtime_inside_or_around_target(self) -> None:
        self.config_file.write_text(json.dumps(self.data | {"runtime_root": str(self.repo / "state")}),
                                    encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.config_file)
        self.config_file.write_text(json.dumps(self.data | {"runtime_root": str(self.repo.parent)}),
                                    encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.config_file)


if __name__ == "__main__":
    unittest.main()

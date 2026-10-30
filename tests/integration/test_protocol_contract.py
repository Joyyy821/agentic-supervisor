"""Opt-in, no-model-turn App Server contract test using a private Unix socket."""

from __future__ import annotations

import asyncio
import os
import subprocess
import unittest
import uuid
from pathlib import Path

from agentic_supervisor.codex import RpcClient, codex_executable
from tests.unit.helpers import new_git_repo, test_root


@unittest.skipUnless(os.environ.get("AGENTIC_SUPERVISOR_PROTOCOL_TEST") == "1",
                     "set AGENTIC_SUPERVISOR_PROTOCOL_TEST=1 for the installed App Server contract")
class ProtocolContractTests(unittest.TestCase):
    def test_two_clients_share_thread_goal_state(self):
        root = test_root() / f"p-{uuid.uuid4().hex[:8]}"
        home = root / "codex-home"
        home.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        socket = root / "s.sock"
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL"}}
        env["CODEX_HOME"] = str(home)
        log_path = root / "server.log"
        server = None
        clients: list[RpcClient] = []

        async def scenario():
            nonlocal server
            thread_id = None
            with log_path.open("wb") as log:
                server = await asyncio.create_subprocess_exec(
                    codex_executable(), "app-server", "--listen", f"unix://{socket}", "--enable", "goals",
                    stdout=log, stderr=log, env=env,
                )
                try:
                    deadline = asyncio.get_running_loop().time() + 12
                    while not socket.exists() and asyncio.get_running_loop().time() < deadline:
                        if server.returncode is not None:
                            break
                        await asyncio.sleep(0.1)
                    self.assertTrue(socket.exists(), f"App Server failed to bind; inspect {log_path}")
                    for name in ("contract_first", "contract_second"):
                        client = RpcClient(codex_executable(), socket, home)
                        await client.connect(name)
                        clients.append(client)
                    models = await clients[0].call("model/list", {})
                    self.assertIsInstance(models.get("data"), list)

                    repository = new_git_repo("protocol-repo")
                    created = await clients[0].call("thread/start", {
                        "cwd": str(repository), "sandbox": "read-only", "approvalPolicy": "on-request",
                    })
                    thread_id = created["thread"]["id"]
                    await clients[0].call("thread/goal/set", {
                        "threadId": thread_id, "objective": "Protocol contract", "status": "paused",
                        "tokenBudget": 100, "origin": "user",
                    })
                    goal = await clients[1].call("thread/goal/get", {"threadId": thread_id})
                    self.assertEqual(goal["goal"]["status"], "paused")
                finally:
                    if thread_id and clients:
                        try:
                            await clients[0].call("thread/goal/clear", {"threadId": thread_id}, timeout=5)
                            await clients[0].call("thread/delete", {"threadId": thread_id}, timeout=5)
                        except Exception:
                            pass
                    for client in clients:
                        await client.close()
                    if server and server.returncode is None:
                        server.terminate()
                        try:
                            await asyncio.wait_for(server.wait(), 5)
                        except asyncio.TimeoutError:
                            server.kill()
                            await server.wait()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

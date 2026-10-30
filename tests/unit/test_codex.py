from __future__ import annotations

import asyncio
import json
import struct
import unittest
from pathlib import Path

from agentic_supervisor.codex import RpcClient


class RpcTests(unittest.TestCase):
    def test_notification_queue_is_bounded_and_discards_transcript_content(self) -> None:
        client = RpcClient("codex", Path("/unused.sock"), Path("/unused"))
        client._queue_event({"method": "item/agentMessage/delta", "params": {
            "item": {"id": "item-1", "type": "agentMessage", "text": "do-not-retain"},
            "delta": "long private response",
        }})
        event = client.notifications.get_nowait()
        self.assertEqual(event["params"]["item"], {"id": "item-1", "type": "agentMessage"})
        self.assertEqual(event["params"]["delta"], {"length": len("long private response")})
        for index in range(300):
            client._queue_event({"method": "turn/started", "params": {"turn": {"id": str(index)}}})
        self.assertEqual(client.notifications.qsize(), 256)
        self.assertEqual(client.dropped_notifications, 44)

    def test_reader_correlates_response_and_queues_notification(self) -> None:
        async def scenario():
            client = RpcClient("codex", Path("/unused.sock"), Path("/unused"))
            class Reader:
                def __init__(self):
                    messages = [
                        {"id": 7, "result": {"ok": True}},
                        {"method": "turn/started", "params": {"turn": {"id": "t1"}}},
                    ]
                    self.data = bytearray()
                    for message in messages:
                        payload = json.dumps(message).encode()
                        if len(payload) < 126:
                            self.data.extend(bytes((0x81, len(payload))))
                        else:
                            self.data.extend(bytes((0x81, 126)) + struct.pack("!H", len(payload)))
                        self.data.extend(payload)

                async def readexactly(self, count):
                    result = bytes(self.data[:count])
                    del self.data[:count]
                    if len(result) != count:
                        raise asyncio.IncompleteReadError(result, count)
                    return result

            class Writer:
                def __init__(self):
                    self.closed = False
                def is_closing(self):
                    return self.closed
                def write(self, _data):
                    pass
                async def drain(self):
                    pass

            client.reader = Reader()
            client.writer = Writer()
            client._websocket_open = True
            future = asyncio.get_running_loop().create_future()
            client._pending[7] = future
            await client._read_loop()
            self.assertEqual((await future)["result"]["ok"], True)
            event = await client.next_event(timeout=0.1)
            self.assertEqual(event["params"]["turn"]["id"], "t1")
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

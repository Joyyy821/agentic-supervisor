"""Small JSON-RPC client for Codex App Server's Unix WebSocket transport."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path
from typing import Any


class CodexError(RuntimeError):
    pass


_EVENT_SCALARS = {"id", "type", "status", "method", "code", "threadId", "turnId", "itemId",
                  "requestId", "tokensUsed", "totalTokens", "tokenBudget", "timeUsedSeconds"}


def _compact_event(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 8:
        return None
    if key in {"delta", "message"} and isinstance(value, str):
        return {"length": len(value)}
    if key in {"delta", "message"} and isinstance(value, dict):
        length = value.get("length")
        return {"length": length} if isinstance(length, int) else {}
    if isinstance(value, dict):
        compact = {}
        for child_key, child in value.items():
            if child_key in _EVENT_SCALARS or isinstance(child, (dict, list)) or child_key in {"delta", "message"}:
                compact[child_key] = _compact_event(child, child_key, depth + 1)
        return compact
    if isinstance(value, list):
        return [_compact_event(item, key, depth + 1) for item in value[:100]]
    if key in _EVENT_SCALARS and isinstance(value, (str, int, float, bool)):
        return value if not isinstance(value, str) or len(value) <= 256 else value[:256]
    return None


def codex_executable(configured: str | None = None) -> str:
    executable = configured or shutil.which("codex")
    if not executable:
        raise CodexError("Codex CLI was not found on PATH")
    return str(Path(executable).expanduser().resolve())


async def version(executable: str) -> str:
    proc = await asyncio.create_subprocess_exec(executable, "--version", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
    if proc.returncode != 0:
        raise CodexError(stderr.decode(errors="replace").strip() or "codex --version failed")
    return stdout.decode(errors="replace").strip()


class RpcClient:
    def __init__(self, executable: str, socket_path: Path, codex_home: Path,
                 environment: dict[str, str] | None = None):
        self.executable = executable
        self.socket_path = socket_path
        self.codex_home = codex_home
        self.environment = environment or {}
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._websocket_open = False
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self.dropped_notifications = 0

    def _queue_event(self, message: dict[str, Any]) -> None:
        compact = {key: value for key, value in message.items() if key in {"method", "id"}}
        compact["params"] = _compact_event(message.get("params", {}), "params")
        if self.notifications.full():
            try:
                self.notifications.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped_notifications += 1
        self.notifications.put_nowait(compact)

    async def connect(self, client_name: str) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.writer.write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                           "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                           f"Sec-WebSocket-Key: {key}\r\n\r\n").encode("ascii"))
        await self.writer.drain()
        try:
            header = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise CodexError("App Server did not complete the Unix WebSocket handshake") from exc
        lines = header.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        status_parts = lines[0].split() if lines else []
        if len(status_parts) < 2 or status_parts[1] != "101" or headers.get("sec-websocket-accept") != expected:
            raise CodexError(f"App Server rejected the Unix WebSocket handshake: {lines[0] if lines else 'empty reply'}")
        self._websocket_open = True
        self._reader_task = asyncio.create_task(self._read_loop())
        await self.call("initialize", {
            "clientInfo": {"name": client_name, "title": "Agentic Supervisor", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=20)
        await self.notify("initialized", {})

    async def _write(self, message: dict[str, Any]) -> None:
        if self.writer is None or self.writer.is_closing() or not self._websocket_open:
            raise CodexError("Codex App Server is not connected")
        await self._write_frame(1, json.dumps(message, separators=(",", ":")).encode("utf-8"))

    async def _write_frame(self, opcode: int, payload: bytes) -> None:
        assert self.writer is not None
        first = bytes((0x80 | opcode,))
        mask_flag = 0x80
        size = len(payload)
        if size < 126:
            header = first + bytes((mask_flag | size,))
        elif size < 65536:
            header = first + bytes((mask_flag | 126,)) + struct.pack("!H", size)
        else:
            header = first + bytes((mask_flag | 127,)) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.writer.write(header + mask + masked)
        await self.writer.drain()

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        assert self.reader is not None
        first, second = await self.reader.readexactly(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        if length > 64 * 1024 * 1024:
            raise CodexError("App Server frame exceeded the 64 MiB safety limit")
        mask = await self.reader.readexactly(4) if masked else b""
        payload = await self.reader.readexactly(length)
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return final, opcode, payload

    async def _read_loop(self) -> None:
        assert self.reader is not None
        try:
            fragments = bytearray()
            fragmented_opcode: int | None = None
            while True:
                final, opcode, payload = await self._read_frame()
                if opcode == 9:
                    await self._write_frame(10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode == 8:
                    await self._write_frame(8, payload[:125])
                    break
                if opcode in (1, 2):
                    if fragmented_opcode is not None:
                        raise CodexError("unexpected new WebSocket message before continuation")
                    fragmented_opcode = opcode
                    fragments = bytearray(payload)
                elif opcode == 0 and fragmented_opcode is not None:
                    fragments.extend(payload)
                else:
                    raise CodexError(f"unsupported WebSocket opcode: {opcode}")
                if len(fragments) > 64 * 1024 * 1024:
                    raise CodexError("App Server message exceeded the 64 MiB safety limit")
                if not final:
                    continue
                if fragmented_opcode != 1:
                    raise CodexError("App Server sent a non-text JSON-RPC message")
                try:
                    message = json.loads(fragments)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._queue_event({"method": "supervisor/protocolError", "params": {"error": str(exc)}})
                    fragmented_opcode = None
                    continue
                fragmented_opcode = None
                if not isinstance(message, dict):
                    raise CodexError("App Server sent a non-object JSON-RPC message")
                message_id = message.get("id")
                if isinstance(message_id, int) and message_id in self._pending and "method" not in message:
                    future = self._pending.pop(message_id)
                    if not future.done():
                        future.set_result(message)
                else:
                    self._queue_event(message)
        except (asyncio.CancelledError, BrokenPipeError):
            pass
        except (asyncio.IncompleteReadError, ConnectionError, OSError, CodexError) as exc:
            self._queue_event({"method": "supervisor/disconnected", "params": {"error": str(exc)}})
        finally:
            self._websocket_open = False
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexError("Codex proxy disconnected before replying"))
            self._pending.clear()

    async def call(self, method: str, params: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
        except BaseException:
            self._pending.pop(request_id, None)
            raise
        if "error" in response:
            error = response["error"]
            message = error.get("message")
            detail = f": {message}" if isinstance(message, str) and message else ""
            raise CodexError(f"{method} failed ({error.get('code')}){detail}")
        return response.get("result", {})

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self.notifications.get()
        return await asyncio.wait_for(self.notifications.get(), timeout)

    async def close(self) -> None:
        if self.writer is None:
            return
        if not self.writer.is_closing() and self._websocket_open:
            try:
                await self._write_frame(8, b"")
            except (OSError, ConnectionError):
                pass
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass

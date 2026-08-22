"""A small ASGI WebSocket client.

Starlette's `TestClient` runs the app on an event loop of its own, which would
fight the one the async fixtures already use. Speaking the ASGI websocket
protocol directly keeps everything on one loop, and it is short enough to read.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class SocketClosed(Exception):
    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"closed with {code}: {reason}")
        self.code = code
        self.reason = reason


class ASGIWebSocket:
    def __init__(self, app, path: str, headers: dict[str, str] | None = None) -> None:
        self._app = app
        self._path, _, self._query = path.partition("?")
        self._headers = headers or {}
        self._to_app: asyncio.Queue = asyncio.Queue()
        self._from_app: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> ASGIWebSocket:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": self._query.encode(),
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in self._headers.items()],
            "client": ("127.0.0.1", 51000),
            "server": ("localhost", 8000),
            "subprotocols": [],
            "state": {},
        }
        self._task = asyncio.create_task(self._app(scope, self._to_app.get, self._from_app.put))
        await self._to_app.put({"type": "websocket.connect"})

        first = await self._next(timeout=5)
        if first["type"] == "websocket.close":
            raise SocketClosed(first.get("code", 1000), first.get("reason", ""))
        assert first["type"] == "websocket.accept", first
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _next(self, timeout: float) -> dict[str, Any]:
        if self._task is not None and self._task.done() and self._from_app.empty():
            self._task.result()
        return await asyncio.wait_for(self._from_app.get(), timeout=timeout)

    async def receive_json(self, timeout: float = 5) -> dict[str, Any]:
        message = await self._next(timeout)
        if message["type"] == "websocket.close":
            raise SocketClosed(message.get("code", 1000), message.get("reason", ""))
        return json.loads(message["text"])

    async def expect_silence(self, seconds: float = 0.4) -> None:
        """Nothing arrives — used to prove one clinic's events stay put."""
        try:
            message = await self._next(timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise AssertionError(f"expected silence, got {message}")


async def connect(app, path: str, headers: dict[str, str] | None = None) -> ASGIWebSocket:
    return await ASGIWebSocket(app, path, headers).__aenter__()

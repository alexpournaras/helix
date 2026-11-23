"""httpx transports: a no-network guard for tests, and record/replay for eval runs (PLAN 1A.10, Part G).

Every research module that talks HTTP creates its client through :func:`client`, so a single
switch decides whether requests go to the network, to a recording, or nowhere.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx


class NetworkBlocked(RuntimeError):
    """Raised when code tries to reach the network while the guard is on."""


class BlockedTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise NetworkBlocked(f"Network access blocked in tests: {request.method} {request.url.host}{request.url.path}")


def _request_key(request: httpx.Request) -> str:
    """Stable key for a request. Auth headers are never part of it, so recordings hold no secrets."""
    body = request.content or b""
    url = request.url.copy_remove_param("api_key").copy_remove_param("email")
    digest = hashlib.sha256(request.method.encode() + b" " + str(url).encode() + b"\n" + body).hexdigest()
    return digest[:32]


class ReplayTransport(httpx.AsyncBaseTransport):
    """Serves responses from a recordings folder; unknown requests fail loudly."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        path = self.folder / f"{_request_key(request)}.json"
        if not path.exists():
            raise NetworkBlocked(f"No recording for {request.method} {request.url.host}{request.url.path}")
        data = json.loads(path.read_text("utf-8"))
        return httpx.Response(data["status"], headers=data.get("headers") or {},
                              content=data["body"].encode("utf-8"), request=request)


class RecordTransport(httpx.AsyncBaseTransport):
    """Forwards to the network and stores each response (bodies only, no request headers)."""

    def __init__(self, folder: Path, inner: httpx.AsyncBaseTransport | None = None):
        self.folder = Path(folder)
        self.inner = inner or httpx.AsyncHTTPTransport(retries=1)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        self.folder.mkdir(parents=True, exist_ok=True)
        keep = {k: v for k, v in response.headers.items() if k.lower() in ("content-type", "retry-after")}
        (self.folder / f"{_request_key(request)}.json").write_text(json.dumps({
            "method": request.method, "host": request.url.host, "path": request.url.path,
            "status": response.status_code, "headers": keep, "body": body.decode("utf-8", "replace"),
        }, ensure_ascii=False), "utf-8")
        return httpx.Response(response.status_code, headers=response.headers, content=body, request=request)


_override: contextvars.ContextVar[httpx.AsyncBaseTransport | None] = contextvars.ContextVar("transport", default=None)
_global_override: list[httpx.AsyncBaseTransport | None] = [None]


def current_transport() -> httpx.AsyncBaseTransport | None:
    return _override.get() or _global_override[0]


def client(**kwargs: Any) -> httpx.AsyncClient:
    """An AsyncClient that honours the active transport override."""
    transport = current_transport()
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


@contextmanager
def use_transport(transport: httpx.AsyncBaseTransport) -> Iterator[None]:
    """Route every research HTTP client in this context (and all threads/tasks it starts) to `transport`."""
    token = _override.set(transport)
    previous = _global_override[0]
    _global_override[0] = transport
    try:
        yield
    finally:
        _override.reset(token)
        _global_override[0] = previous


def block_network() -> None:
    """Install the guard process-wide (used by the test kit)."""
    _global_override[0] = BlockedTransport()

"""Tests for accepting MCP revisions newer than the bundled SDK knows."""

import json

from mcp.types import LATEST_PROTOCOL_VERSION

from mcp_superset.protocol_compat import (
    PROTOCOL_VERSION_HEADER,
    ProtocolVersionCompatMiddleware,
    compat_middleware,
)

BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()


class _Recorder:
    """ASGI app that records the protocol header and the body it was handed."""

    def __init__(self):
        self.seen: str | None = None
        self.body: bytes = b""

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers") or [])
        raw = headers.get(PROTOCOL_VERSION_HEADER.encode())
        self.seen = raw.decode() if raw is not None else None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                break


def _scope(version: str | None) -> dict:
    headers = [(b"content-type", b"application/json")]
    if version is not None:
        headers.append((PROTOCOL_VERSION_HEADER.encode(), version.encode()))
    return {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}


def _receive(chunks: list[bytes]):
    """Return an ASGI receive that yields the given body chunks."""
    queue = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)
    ]

    async def receive() -> dict:
        return queue.pop(0)

    return receive


async def _run(version: str | None, chunks: list[bytes] | None = None) -> _Recorder:
    app = _Recorder()
    middleware = ProtocolVersionCompatMiddleware(app)
    await middleware(_scope(version), _receive(chunks or [BODY]), None)
    return app


async def test_newer_revision_is_handled_as_the_latest_supported():
    """The failure that cut the connector off: client asked for a 2026 revision."""
    assert (await _run("2026-07-28")).seen == LATEST_PROTOCOL_VERSION


async def test_supported_revision_passes_through():
    assert (await _run(LATEST_PROTOCOL_VERSION)).seen == LATEST_PROTOCOL_VERSION


async def test_older_revision_is_left_for_the_transport_to_judge():
    assert (await _run("2024-11-05")).seen == "2024-11-05"


async def test_malformed_revision_is_left_alone():
    assert (await _run("draft")).seen == "draft"


async def test_missing_header_is_not_invented():
    assert (await _run(None)).seen is None


async def test_body_reaches_the_app_unchanged():
    """Reading the body to log its method must not consume it."""
    assert (await _run("2026-07-28")).body == BODY


async def test_chunked_body_is_replayed_in_full():
    half = len(BODY) // 2
    app = await _run(None, [BODY[:half], BODY[half:]])
    assert app.body == BODY


async def test_non_http_scope_is_passed_through():
    app = _Recorder()

    async def lifespan_receive() -> dict:
        return {"type": "lifespan.startup"}

    await ProtocolVersionCompatMiddleware(app)({"type": "lifespan"}, lifespan_receive, None)
    assert app.seen is None
    assert app.body == b""


def test_middleware_can_be_disabled():
    assert compat_middleware(enabled=False) == []
    assert len(compat_middleware(enabled=True)) == 1

"""Tests for accepting MCP revisions newer than the bundled SDK knows."""

import json

from mcp.types import LATEST_PROTOCOL_VERSION

from mcp_superset.protocol_compat import (
    PROTOCOL_VERSION_HEADER,
    ProtocolVersionCompatMiddleware,
    compat_middleware,
    sdk_known_methods,
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


class _Sink:
    """Collects the ASGI response messages the middleware sends itself."""

    def __init__(self):
        self.status: int | None = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


async def _call(body: bytes, version: str | None = None) -> tuple[_Recorder, _Sink]:
    app, sink = _Recorder(), _Sink()
    middleware = ProtocolVersionCompatMiddleware(app)
    await middleware(_scope(version), _receive([body]), sink)
    return app, sink


def test_sdk_method_list_is_readable():
    """The refusal below is only safe while this reflects the SDK's own union."""
    methods = sdk_known_methods()
    assert {"initialize", "tools/call", "tools/list"} <= methods
    assert "server/discover" not in methods


async def test_unknown_method_gets_method_not_found():
    """server/discover - the probe that cut the connector off - must say -32601."""
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "server/discover"}).encode()
    app, sink = await _call(body, "2026-07-28")

    assert sink.status == 200
    answer = json.loads(sink.body)
    assert answer["id"] == 7
    assert answer["error"]["code"] == -32601
    assert "server/discover" in answer["error"]["message"]
    assert app.body == b""  # never reached the SDK


async def test_unknown_notification_is_acknowledged_without_a_body():
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/whatever"}).encode()
    app, sink = await _call(body)

    assert sink.status == 202
    assert sink.body == b""
    assert app.body == b""


async def test_known_method_still_reaches_the_sdk():
    app, sink = await _call(BODY)
    assert sink.status is None
    assert app.body == BODY


async def test_non_jsonrpc_body_reaches_the_sdk():
    """A token form post must not be mistaken for a JSON-RPC call."""
    app, sink = await _call(b"grant_type=refresh_token&refresh_token=x")
    assert sink.status is None
    assert app.body


async def test_refusal_is_disabled_when_the_method_list_is_unavailable():
    """Introspection failure must not turn into wrongly refusing valid methods."""
    app, sink = _Recorder(), _Sink()
    middleware = ProtocolVersionCompatMiddleware(app, known_methods=frozenset())
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}).encode()
    await middleware(_scope(None), _receive([body]), sink)

    assert sink.status is None
    assert app.body == body

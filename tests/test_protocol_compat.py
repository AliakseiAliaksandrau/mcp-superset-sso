"""Tests for refusing unsupported revisions and unknown methods per the MCP spec."""

import json

from mcp.types import LATEST_PROTOCOL_VERSION

from mcp_superset.protocol_compat import (
    PROTOCOL_VERSION_HEADER,
    PROTOCOL_VERSION_META_KEY,
    ProtocolCompatMiddleware,
    compat_middleware,
    sdk_known_methods,
)

# The revision Claude asks for, and the probe it sends before anything else.
MODERN = "2026-07-28"
BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
DISCOVER = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "server/discover"}).encode()


class _Recorder:
    """ASGI app that records the body it was handed."""

    def __init__(self):
        self.body = b""
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                break


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

    @property
    def json(self) -> dict:
        return json.loads(self.body)


def _scope(version: str | None = None, method: str = "POST") -> dict:
    headers = [(b"content-type", b"application/json")]
    if version is not None:
        headers.append((PROTOCOL_VERSION_HEADER.encode(), version.encode()))
    return {"type": "http", "method": method, "path": "/mcp", "headers": headers}


def _receive(chunks: list[bytes]):
    """Return an ASGI receive that yields the given body chunks."""
    queue = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)
    ]

    async def receive() -> dict:
        return queue.pop(0)

    return receive


async def _call(body: bytes = BODY, version: str | None = None, **kwargs):
    app, sink = _Recorder(), _Sink()
    middleware = ProtocolCompatMiddleware(app, **kwargs)
    await middleware(_scope(version), _receive([body]), sink)
    return app, sink


async def test_unsupported_version_gets_400_and_the_supported_list():
    """The refusal the client needs to retry: 400 + -32022 naming our versions."""
    app, sink = await _call(BODY, MODERN)

    assert sink.status == 400
    error = sink.json["error"]
    assert error["code"] == -32022
    assert sink.json["id"] == 1
    assert error["data"]["requested"] == MODERN
    assert LATEST_PROTOCOL_VERSION in error["data"]["supported"]
    assert not app.called


async def test_version_is_also_read_from_the_request_body():
    """Revision 2026-07-28 carries it in _meta as well as the header."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/list",
            "params": {"_meta": {PROTOCOL_VERSION_META_KEY: MODERN}},
        }
    ).encode()
    app, sink = await _call(body)

    assert sink.status == 400
    assert sink.json["error"]["code"] == -32022
    assert not app.called


async def test_supported_version_reaches_the_sdk():
    app, sink = await _call(BODY, LATEST_PROTOCOL_VERSION)
    assert sink.status is None
    assert app.body == BODY


async def test_request_without_a_version_reaches_the_sdk():
    """Legacy clients negotiate inside initialize and send no header."""
    app, sink = await _call(BODY)
    assert sink.status is None
    assert app.body == BODY


async def test_unknown_method_gets_404_and_method_not_found():
    """server/discover on a supported version: 404 + -32601, as the spec requires."""
    app, sink = await _call(DISCOVER, LATEST_PROTOCOL_VERSION)

    assert sink.status == 404
    assert sink.json["id"] == 7
    assert sink.json["error"]["code"] == -32601
    assert "server/discover" in sink.json["error"]["message"]
    assert not app.called


async def test_version_refusal_takes_precedence_over_the_method():
    """A modern probe is refused on the version, which is what the client retries on."""
    _app, sink = await _call(DISCOVER, MODERN)
    assert sink.status == 400
    assert sink.json["error"]["code"] == -32022


async def test_notification_refusal_carries_no_id():
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/whatever"}).encode()
    _app, sink = await _call(body, LATEST_PROTOCOL_VERSION)

    assert sink.status == 404
    assert "id" not in sink.json
    assert sink.json["error"]["code"] == -32601


async def test_non_jsonrpc_body_reaches_the_sdk():
    """A token form post must not be mistaken for a JSON-RPC call."""
    app, sink = await _call(b"grant_type=refresh_token&refresh_token=x")
    assert sink.status is None
    assert app.body


async def test_body_reaches_the_app_unchanged():
    """Reading the body to inspect it must not consume it."""
    app, _sink = await _call(BODY)
    assert app.body == BODY


async def test_chunked_body_is_replayed_in_full():
    app, sink = _Recorder(), _Sink()
    half = len(BODY) // 2
    await ProtocolCompatMiddleware(app)(_scope(), _receive([BODY[:half], BODY[half:]]), sink)
    assert app.body == BODY


async def test_get_requests_are_passed_through():
    app, sink = _Recorder(), _Sink()

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    await ProtocolCompatMiddleware(app)(_scope(MODERN, method="GET"), receive, sink)
    assert app.called
    assert sink.status is None


async def test_non_http_scope_is_passed_through():
    app, sink = _Recorder(), _Sink()

    async def lifespan_receive() -> dict:
        return {"type": "lifespan.startup"}

    await ProtocolCompatMiddleware(app)({"type": "lifespan"}, lifespan_receive, sink)
    assert app.called


async def test_method_refusal_is_disabled_when_the_method_list_is_unavailable():
    """Introspection failure must not turn into wrongly refusing valid methods."""
    app, sink = await _call(DISCOVER, LATEST_PROTOCOL_VERSION, known_methods=frozenset())
    assert sink.status is None
    assert app.body == DISCOVER


def test_sdk_method_list_is_readable():
    """The method refusal is only safe while this reflects the SDK's own union."""
    methods = sdk_known_methods()
    assert {"initialize", "tools/call", "tools/list"} <= methods
    assert "server/discover" not in methods


def test_middleware_can_be_disabled():
    assert compat_middleware(enabled=False) == []
    assert len(compat_middleware(enabled=True)) == 1

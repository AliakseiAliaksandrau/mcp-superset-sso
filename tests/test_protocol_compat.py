"""Tests for answering the modern discovery probe without posing as a modern server."""

import json

from mcp.types import LATEST_PROTOCOL_VERSION

from mcp_superset.protocol_compat import (
    DISCOVER_METHOD,
    PROTOCOL_VERSION_HEADER,
    PROTOCOL_VERSION_META_KEY,
    ProtocolCompatMiddleware,
    compat_middleware,
    sdk_known_methods,
)

# The revision Claude asks for, and the probe it sends before anything else.
MODERN = "2026-07-28"
BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
DISCOVER = json.dumps({"jsonrpc": "2.0", "id": 7, "method": DISCOVER_METHOD}).encode()
UNKNOWN = json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tasks/whatever"}).encode()


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


async def test_discovery_probe_is_answered_with_the_versions_we_speak():
    """The probe that took the connector down: answer it, do not refuse it."""
    app, sink = await _call(DISCOVER, MODERN, server_info={"name": "superset", "version": "0.4.0"})

    assert sink.status == 200
    result = sink.json["result"]
    assert sink.json["id"] == 7
    assert result["resultType"] == "complete"
    assert LATEST_PROTOCOL_VERSION in result["supportedVersions"]
    assert MODERN not in result["supportedVersions"]
    assert result["capabilities"] == {"tools": {}}
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "superset"
    assert not app.called


async def test_probe_is_answered_even_without_a_version_header():
    """The revision carries the version in _meta as well as the header."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": DISCOVER_METHOD,
            "params": {"_meta": {PROTOCOL_VERSION_META_KEY: MODERN}},
        }
    ).encode()
    app, sink = await _call(body)

    assert sink.status == 200
    assert sink.json["result"]["resultType"] == "complete"
    assert not app.called


async def test_request_on_an_unsupported_revision_is_left_to_the_sdk():
    """Its plain 400 is what makes a dual-era client fall back to initialize.

    Answering a recognized modern error here instead (e.g. -32022) marks this
    server as modern, and the client then loops on the modern probe - observed
    live as six identical probes and a failed connection.
    """
    app, sink = await _call(BODY, MODERN)
    assert sink.status is None
    assert app.body == BODY


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
    """Any other method the SDK cannot parse: 404 + -32601, as the spec requires."""
    app, sink = await _call(UNKNOWN, LATEST_PROTOCOL_VERSION)

    assert sink.status == 404
    assert sink.json["id"] == 8
    assert sink.json["error"]["code"] == -32601
    assert "tasks/whatever" in sink.json["error"]["message"]
    assert not app.called


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
    app, sink = await _call(UNKNOWN, LATEST_PROTOCOL_VERSION, known_methods=frozenset())
    assert sink.status is None
    assert app.body == UNKNOWN


def test_sdk_method_list_is_readable():
    """The method refusal is only safe while this reflects the SDK's own union."""
    methods = sdk_known_methods()
    assert {"initialize", "tools/call", "tools/list"} <= methods
    assert DISCOVER_METHOD not in methods


def test_middleware_can_be_disabled():
    assert compat_middleware(enabled=False) == []

    stack = compat_middleware(enabled=True, server_info={"name": "superset", "version": "1"})
    assert len(stack) == 1
    assert stack[0].kwargs["server_info"]["name"] == "superset"

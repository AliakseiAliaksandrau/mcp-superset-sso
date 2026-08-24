"""Tests for accepting MCP revisions newer than the bundled SDK knows."""

from mcp.types import LATEST_PROTOCOL_VERSION

from mcp_superset.protocol_compat import (
    PROTOCOL_VERSION_HEADER,
    ProtocolVersionCompatMiddleware,
    compat_middleware,
)


class _Recorder:
    """ASGI app that records the protocol header it was handed."""

    def __init__(self):
        self.seen: str | None = None

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers") or [])
        raw = headers.get(PROTOCOL_VERSION_HEADER.encode())
        self.seen = raw.decode() if raw is not None else None


def _scope(version: str | None) -> dict:
    headers = [(b"content-type", b"application/json")]
    if version is not None:
        headers.append((PROTOCOL_VERSION_HEADER.encode(), version.encode()))
    return {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}


async def _run(version: str | None) -> str | None:
    app = _Recorder()
    await ProtocolVersionCompatMiddleware(app)(_scope(version), None, None)
    return app.seen


async def test_newer_revision_is_handled_as_the_latest_supported():
    """The failure that cut the connector off: client asked for a 2026 revision."""
    assert await _run("2026-01-01") == LATEST_PROTOCOL_VERSION


async def test_supported_revision_passes_through():
    assert await _run(LATEST_PROTOCOL_VERSION) == LATEST_PROTOCOL_VERSION


async def test_older_revision_is_left_for_the_transport_to_judge():
    assert await _run("2024-11-05") == "2024-11-05"


async def test_malformed_revision_is_left_alone():
    assert await _run("draft") == "draft"


async def test_missing_header_is_not_invented():
    assert await _run(None) is None


async def test_non_http_scope_is_passed_through():
    app = _Recorder()
    await ProtocolVersionCompatMiddleware(app)({"type": "lifespan"}, None, None)
    assert app.seen is None


def test_middleware_can_be_disabled():
    assert compat_middleware(enabled=False) == []
    assert len(compat_middleware(enabled=True)) == 1

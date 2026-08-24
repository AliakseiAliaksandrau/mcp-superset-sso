"""Keep working when a client speaks a newer MCP revision than the SDK knows.

The streamable-HTTP transport answers 400 to any ``MCP-Protocol-Version`` header
that is not in the SDK's supported list. That follows the spec - the client is
then expected to retry with a supported revision - but a client that stops
retrying is simply cut off, and the SDK cannot be upgraded away from the problem:
fastmcp 3.4.x pins ``mcp<2.0`` and the newer revisions only landed in ``mcp`` 2.x.

This middleware rewrites an unknown but *newer* revision down to the newest one
the SDK supports, so those clients keep working. Revisions the SDK already knows
pass through untouched, and anything older or not shaped like a revision date is
left for the transport to reject as before.

Turn it off with ``SUPERSET_MCP_PROTOCOL_COMPAT=false`` once the SDK supports the
revisions clients ask for.
"""

import json
import logging
import re

from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

PROTOCOL_VERSION_HEADER = "mcp-protocol-version"

# Only the head of a body is parsed to name the JSON-RPC method; the whole body is
# always replayed downstream untouched.
_BODY_PEEK_LIMIT = 8192

# Revisions are dates ("2025-11-25"), so a string compare orders them correctly.
_REVISION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ProtocolVersionCompatMiddleware:
    """Rewrite a too-new MCP-Protocol-Version header, and name each request in the log.

    The SDK logs a JSON-RPC method only when validation of it fails, which leaves an
    incident like "client stopped talking after three successful responses" unreadable.
    One line per request - method plus the revision the client asked for - makes the
    sequence obvious afterwards.
    """

    def __init__(
        self,
        app: ASGIApp,
        supported: frozenset[str] | None = None,
        latest: str = LATEST_PROTOCOL_VERSION,
        log_requests: bool = True,
    ):
        self.app = app
        self.supported = frozenset(supported or SUPPORTED_PROTOCOL_VERSIONS)
        self.latest = latest
        self.log_requests = log_requests
        self._reported: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Normalise the protocol header, then hand the request on."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = MutableHeaders(scope=scope)
        requested = headers.get(PROTOCOL_VERSION_HEADER)
        if requested is not None and self._should_rewrite(requested):
            headers[PROTOCOL_VERSION_HEADER] = self.latest
            if requested not in self._reported:
                self._reported.add(requested)
                logger.warning(
                    "Client asked for MCP protocol %s, which this build does not know; "
                    "handling the request as %s. Upgrade the mcp SDK when it supports %s.",
                    requested,
                    self.latest,
                    requested,
                )

        if self.log_requests and scope.get("method") == "POST":
            body, receive = await _buffer_body(receive)
            logger.info(
                "MCP request: method=%s protocol=%s",
                _peek_method(body),
                requested or "(none)",
            )

        await self.app(scope, receive, send)

    def _should_rewrite(self, requested: str) -> bool:
        """True for a well-formed revision that is newer than anything supported."""
        if requested in self.supported:
            return False
        return bool(_REVISION_RE.match(requested)) and requested > self.latest


async def _buffer_body(receive: Receive) -> tuple[bytes, Receive]:
    """Read the whole request body and return it with a receive that replays it.

    Args:
        receive: The original ASGI receive callable.

    Returns:
        (body, receive) where the returned receive yields the same messages again.
    """
    messages: list[Message] = []
    body = b""
    more = True
    while more:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        more = message.get("more_body", False)

    async def replay() -> Message:
        if messages:
            return messages.pop(0)
        return await receive()

    return body, replay


def _peek_method(body: bytes) -> str:
    """Return the JSON-RPC method named in a body, or a marker when unreadable."""
    if not body:
        return "(empty)"
    try:
        payload = json.loads(body[:_BODY_PEEK_LIMIT])
    except (ValueError, UnicodeDecodeError):
        return "(unparsed)"
    if isinstance(payload, list):
        return "batch:" + ",".join(str(item.get("method")) for item in payload if isinstance(item, dict))
    if isinstance(payload, dict):
        return str(payload.get("method", "(no method)"))
    return "(unexpected)"


def compat_middleware(enabled: bool = True) -> list[Middleware]:
    """Return the HTTP middleware stack for the server.

    Args:
        enabled: False returns an empty stack, restoring plain SDK behaviour.

    Returns:
        A list to pass as ``middleware=`` to FastMCP's HTTP app.
    """
    if not enabled:
        return []
    return [Middleware(ProtocolVersionCompatMiddleware)]

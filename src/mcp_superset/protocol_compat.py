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

The same drift brings methods the SDK has never heard of - clients probe
``server/discover`` before the classic handshake. The SDK fails to validate such a
request and answers ``-32602 Invalid request parameters``, which tells the client
"you called it wrong" instead of "I do not have that method". This middleware
answers unknown methods with ``-32601 Method not found`` instead, the reply a
client needs in order to fall back to the handshake it does support.

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
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

PROTOCOL_VERSION_HEADER = "mcp-protocol-version"

# Only the head of a body is parsed to name the JSON-RPC method; the whole body is
# always replayed downstream untouched.
_BODY_PEEK_LIMIT = 8192

# Revisions are dates ("2025-11-25"), so a string compare orders them correctly.
_REVISION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# JSON-RPC: the answer a client expects for a method the server does not implement.
_METHOD_NOT_FOUND = -32601


def sdk_known_methods() -> frozenset[str]:
    """Return the JSON-RPC methods the installed SDK can parse.

    Read from the SDK's own request/notification unions - the very definition its
    validator uses - so "not in this set" means "the transport would reject it".
    An empty set (introspection failed against a future SDK layout) disables
    answering unknown methods rather than risking a wrong refusal.
    """
    try:
        from typing import get_args

        from mcp.types import ClientNotification, ClientRequest

        methods: set[str] = set()
        for model in (ClientRequest, ClientNotification):
            annotation = model.model_fields["root"].annotation
            for member in get_args(annotation):
                field = getattr(member, "model_fields", {}).get("method")
                if field is None:
                    continue
                methods.update(a for a in get_args(field.annotation) if isinstance(a, str))
        return frozenset(methods)
    except Exception as exc:  # noqa: BLE001 - never break startup over introspection
        logger.warning("Could not read the SDK method list (%s); unknown methods pass through", exc)
        return frozenset()


class ProtocolVersionCompatMiddleware:
    """Bridge the gap between a newer client revision and the bundled SDK.

    Three jobs, all at the HTTP layer, before the SDK's transport sees anything:

    * rewrite a too-new ``MCP-Protocol-Version`` header to the newest supported one;
    * answer a method the SDK cannot parse with ``-32601 Method not found``, so the
      client learns the method is absent and falls back to what it does support;
    * name every request in the log. The SDK logs a method only when validation of
      it fails, which leaves an incident like "client stopped talking after three
      successful responses" unreadable.
    """

    def __init__(
        self,
        app: ASGIApp,
        supported: frozenset[str] | None = None,
        latest: str = LATEST_PROTOCOL_VERSION,
        log_requests: bool = True,
        known_methods: frozenset[str] | None = None,
    ):
        self.app = app
        self.supported = frozenset(supported or SUPPORTED_PROTOCOL_VERSIONS)
        self.latest = latest
        self.log_requests = log_requests
        self.known_methods = sdk_known_methods() if known_methods is None else known_methods
        self._reported: set[str] = set()
        self._reported_methods: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Normalise the request, answer what the SDK cannot, hand the rest on."""
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

        if scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body, receive = await _buffer_body(receive)
        call = _peek_call(body)
        if self.log_requests:
            logger.info(
                "MCP request: method=%s protocol=%s",
                call.method or "(none)",
                requested or "(none)",
            )

        if self._is_unknown_method(call):
            await self._answer_method_not_found(call, scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _should_rewrite(self, requested: str) -> bool:
        """True for a well-formed revision that is newer than anything supported."""
        if requested in self.supported:
            return False
        return bool(_REVISION_RE.match(requested)) and requested > self.latest

    def _is_unknown_method(self, call: "_JsonRpcCall") -> bool:
        """True for a JSON-RPC call naming a method the SDK cannot parse."""
        if not self.known_methods or not call.is_jsonrpc or call.method is None:
            return False
        return call.method not in self.known_methods

    async def _answer_method_not_found(self, call: "_JsonRpcCall", scope: Scope, receive: Receive, send: Send) -> None:
        """Reply -32601 to a request, or acknowledge a notification."""
        if call.method not in self._reported_methods:
            self._reported_methods.add(str(call.method))
            logger.info(
                "Method %s is not in this SDK build; answering -32601 (method not found)",
                call.method,
            )

        if call.id is None:
            # A notification takes no response body, only an acknowledgement.
            await Response(status_code=202)(scope, receive, send)
            return

        payload = {
            "jsonrpc": "2.0",
            "id": call.id,
            "error": {
                "code": _METHOD_NOT_FOUND,
                "message": f"Method not found: {call.method}",
            },
        }
        await JSONResponse(payload)(scope, receive, send)


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


class _JsonRpcCall:
    """What could be read out of a request body without consuming it."""

    __slots__ = ("method", "id", "is_jsonrpc")

    def __init__(self, method: str | None = None, id: object = None, is_jsonrpc: bool = False):
        self.method = method
        self.id = id
        self.is_jsonrpc = is_jsonrpc


def _peek_call(body: bytes) -> _JsonRpcCall:
    """Read method and id from a body, leaving the body itself untouched.

    Anything that is not a single JSON-RPC object (a batch, a form post, a
    truncated body) comes back with ``is_jsonrpc`` false, so it is passed
    downstream rather than answered here.
    """
    if not body:
        return _JsonRpcCall("(empty)")
    try:
        payload = json.loads(body[:_BODY_PEEK_LIMIT])
    except (ValueError, UnicodeDecodeError):
        return _JsonRpcCall("(unparsed)")
    if isinstance(payload, list):
        methods = ",".join(str(item.get("method")) for item in payload if isinstance(item, dict))
        return _JsonRpcCall(f"batch:{methods}")
    if not isinstance(payload, dict):
        return _JsonRpcCall("(unexpected)")

    method = payload.get("method")
    return _JsonRpcCall(
        method=str(method) if isinstance(method, str) else "(no method)",
        id=payload.get("id"),
        is_jsonrpc=payload.get("jsonrpc") == "2.0" and isinstance(method, str),
    )


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

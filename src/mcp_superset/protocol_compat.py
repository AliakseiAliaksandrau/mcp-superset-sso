"""Answer a modern MCP client without pretending to be one.

Clients now speak revision ``2026-07-28``, which dropped the ``initialize``
handshake and probes ``server/discover`` before anything else. The bundled SDK
speaks up to ``2025-11-25`` and cannot be upgraded away from it - fastmcp 3.4.x
pins ``mcp<2.0`` while the newer revisions only exist in ``mcp`` 2.x.

The probe is answered here, truthfully: a ``DiscoverResult`` whose
``supportedVersions`` lists only the revisions this build actually speaks. The
spec says of that field, "The client should choose one of these for subsequent
requests", so one round trip is enough for the client to settle on ``2025-11-25``
and use the legacy handshake.

What must *not* happen is answering with a modern protocol error such as
``-32022 UnsupportedProtocolVersionError``. Those codes identify a *modern*
server, and the compatibility matrix then has the client "retry with a supported
version rather than falling back" - which it does by re-sending the modern
``server/discover`` it just failed on. Observed live: six identical probes and
then "the server didn't respond". A legacy server has to look legacy, so a
request on an unsupported revision is left to the SDK, whose plain ``400`` is not
a recognized modern error and therefore triggers the client's fallback to
``initialize``.

Anything else the SDK cannot parse gets ``404`` + ``-32601 Method not found``,
the answer the revision defines for an unknown method.

Spec: https://modelcontextprotocol.io/specification/2026-07-28/server/discover
      https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
      https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http

Turn it off with ``SUPERSET_MCP_PROTOCOL_COMPAT=false`` once the SDK speaks the
revisions clients ask for.
"""

import json
import logging
from typing import Any

from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
# Where revision 2026-07-28 carries the same value inside the request body.
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"

# Only the head of a body is parsed; the whole body is replayed downstream untouched.
_BODY_PEEK_LIMIT = 8192

# The modern discovery probe, which every 2026-07-28 client sends first.
DISCOVER_METHOD = "server/discover"

# The answer the revision defines for a method a server does not implement.
_METHOD_NOT_FOUND = -32601


def sdk_known_methods() -> frozenset[str]:
    """Return the JSON-RPC methods the installed SDK can parse.

    Read from the SDK's own request/notification unions - the definition its
    validator uses - so "not in this set" means "the transport would reject it".
    An empty set (introspection failed against a future SDK layout) disables the
    method refusal rather than risking a wrong one.
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


class ProtocolCompatMiddleware:
    """Answer the modern discovery probe; leave everything else to the SDK.

    Every POST is named in the log (method plus requested revision), because the
    SDK logs a method only when its validation fails, which makes an incident
    unreadable afterwards.
    """

    def __init__(
        self,
        app: ASGIApp,
        supported: tuple[str, ...] | None = None,
        known_methods: frozenset[str] | None = None,
        server_info: dict[str, Any] | None = None,
        capabilities: dict[str, Any] | None = None,
        instructions: str | None = None,
        log_requests: bool = True,
    ):
        self.app = app
        self.supported = supported or tuple(sorted(SUPPORTED_PROTOCOL_VERSIONS, reverse=True))
        self.known_methods = sdk_known_methods() if known_methods is None else known_methods
        self.server_info = server_info
        self.capabilities = capabilities if capabilities is not None else {"tools": {}}
        self.instructions = instructions
        self.log_requests = log_requests
        self._reported_methods: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer the discovery probe and unknown methods; pass the rest through."""
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body, receive = await _buffer_body(receive)
        call = _peek_call(body)
        requested = Headers(scope=scope).get(PROTOCOL_VERSION_HEADER) or call.protocol_version

        if self.log_requests:
            logger.info(
                "MCP request: method=%s protocol=%s",
                call.method or "(none)",
                requested or "(none)",
            )

        if call.is_jsonrpc and call.method == DISCOVER_METHOD:
            await self._answer_discover(call, scope, receive, send)
            return

        if self._is_unknown_method(call):
            await self._refuse_method(call, scope, receive, send)
            return

        # A request on a revision the SDK does not speak is left to the SDK: its
        # plain 400 is not a recognized modern error, which is exactly the signal
        # that makes a dual-era client fall back to the initialize handshake.
        await self.app(scope, receive, send)

    def _is_unknown_method(self, call: "_JsonRpcCall") -> bool:
        """True for a JSON-RPC call naming a method the SDK cannot parse."""
        if not self.known_methods or not call.is_jsonrpc or call.method is None:
            return False
        return call.method not in self.known_methods

    async def _answer_discover(self, call: "_JsonRpcCall", scope: Scope, receive: Receive, send: Send) -> None:
        """Reply with a DiscoverResult naming the revisions this build speaks."""
        logger.info(
            "Discovery probe answered: supportedVersions=%s",
            ",".join(self.supported),
        )
        result: dict[str, Any] = {
            "resultType": "complete",
            "supportedVersions": list(self.supported),
            "capabilities": self.capabilities,
        }
        if self.instructions:
            result["instructions"] = self.instructions
        if self.server_info:
            result["_meta"] = {"io.modelcontextprotocol/serverInfo": self.server_info}

        payload: dict[str, Any] = {"jsonrpc": "2.0", "result": result}
        if call.id is not None:
            payload["id"] = call.id
        await JSONResponse(payload)(scope, receive, send)

    async def _refuse_method(self, call: "_JsonRpcCall", scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 404 + -32601 for a method this build does not implement."""
        if call.method not in self._reported_methods:
            self._reported_methods.add(str(call.method))
            logger.info("Method %s is not in this SDK build; answering -32601", call.method)
        await _json_rpc_error(
            status=404,
            code=_METHOD_NOT_FOUND,
            message=f"Method not found: {call.method}",
            request_id=call.id,
            scope=scope,
            receive=receive,
            send=send,
        )


async def _json_rpc_error(
    status: int,
    code: int,
    message: str,
    request_id: object,
    scope: Scope,
    receive: Receive,
    send: Send,
    data: dict[str, Any] | None = None,
) -> None:
    """Send a JSON-RPC error response with the given HTTP status.

    The id is echoed when the request carried one; a notification (no id) gets the
    error without one, as the spec allows.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    payload: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if request_id is not None:
        payload["id"] = request_id
    await JSONResponse(payload, status_code=status)(scope, receive, send)


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

    __slots__ = ("method", "id", "is_jsonrpc", "protocol_version")

    def __init__(
        self,
        method: str | None = None,
        id: object = None,
        is_jsonrpc: bool = False,
        protocol_version: str | None = None,
    ):
        self.method = method
        self.id = id
        self.is_jsonrpc = is_jsonrpc
        self.protocol_version = protocol_version


def _peek_call(body: bytes) -> _JsonRpcCall:
    """Read method, id and declared revision from a body, leaving the body alone.

    Anything that is not a single JSON-RPC object (a batch, a form post, a
    truncated body) comes back with ``is_jsonrpc`` false and no version, so it is
    passed downstream rather than answered here.
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
    params = payload.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    version = meta.get(PROTOCOL_VERSION_META_KEY) if isinstance(meta, dict) else None
    return _JsonRpcCall(
        method=str(method) if isinstance(method, str) else "(no method)",
        id=payload.get("id"),
        is_jsonrpc=payload.get("jsonrpc") == "2.0" and isinstance(method, str),
        protocol_version=version if isinstance(version, str) else None,
    )


def compat_middleware(
    enabled: bool = True,
    server_info: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    instructions: str | None = None,
) -> list[Middleware]:
    """Return the HTTP middleware stack for the server.

    Args:
        enabled: False returns an empty stack, restoring plain SDK behaviour.
        server_info: Name and version reported in the discovery result.
        capabilities: Capabilities reported in the discovery result.
        instructions: Optional guidance included in the discovery result.

    Returns:
        A list to pass as ``middleware=`` to FastMCP's HTTP app.
    """
    if not enabled:
        return []
    return [
        Middleware(
            ProtocolCompatMiddleware,
            server_info=server_info,
            capabilities=capabilities,
            instructions=instructions,
        )
    ]

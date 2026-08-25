"""Answer a modern MCP client in the terms its revision defines.

Clients now speak revision ``2026-07-28``, which dropped the ``initialize``
handshake: the protocol version travels in every request (header
``MCP-Protocol-Version`` plus ``_meta``), and a client probes ``server/discover``
before anything else. The bundled SDK speaks up to ``2025-11-25`` and cannot be
upgraded away from it - fastmcp 3.4.x pins ``mcp<2.0`` while the newer revisions
only exist in ``mcp`` 2.x.

That revision states exactly how a server refuses what it does not implement,
and the answers are what a client uses to decide whether to retry or to fall back
to the legacy handshake:

* unsupported protocol version -> ``400 Bad Request`` with ``-32022``
  ``UnsupportedProtocolVersionError`` carrying ``data.supported`` (the versions
  this server does speak) and ``data.requested``. The client then "SHOULD select a
  mutually supported version from the supported list and retry the request";
* unknown method -> ``404 Not Found`` with ``-32601`` ``Method not found``.

The SDK produces neither: it answers a plain ``400`` for the version and lets its
validator emit ``-32602 Invalid request parameters`` for the method. Both leave the
client guessing - in practice it sometimes fell back to ``initialize`` and
sometimes declared the server unreachable, which is what took the connector down
twice. This middleware supplies the two documented answers before the SDK sees the
request, so the outcome is deterministic.

Spec: https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
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

# Error codes defined by the MCP specification for these two refusals.
_UNSUPPORTED_PROTOCOL_VERSION = -32022
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
    """Refuse unsupported versions and unknown methods the way the spec prescribes.

    Anything the SDK can handle is passed through untouched. Every POST is named
    in the log (method plus requested revision), because the SDK logs a method
    only when its validation fails, which makes an incident unreadable.
    """

    def __init__(
        self,
        app: ASGIApp,
        supported: tuple[str, ...] | None = None,
        known_methods: frozenset[str] | None = None,
        log_requests: bool = True,
    ):
        self.app = app
        self.supported = supported or tuple(sorted(SUPPORTED_PROTOCOL_VERSIONS, reverse=True))
        self.known_methods = sdk_known_methods() if known_methods is None else known_methods
        self.log_requests = log_requests
        self._reported_versions: set[str] = set()
        self._reported_methods: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer what the SDK cannot, and hand everything else on."""
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

        if requested is not None and requested not in self.supported:
            await self._refuse_version(requested, call, scope, receive, send)
            return

        if self._is_unknown_method(call):
            await self._refuse_method(call, scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _is_unknown_method(self, call: "_JsonRpcCall") -> bool:
        """True for a JSON-RPC call naming a method the SDK cannot parse."""
        if not self.known_methods or not call.is_jsonrpc or call.method is None:
            return False
        return call.method not in self.known_methods

    async def _refuse_version(
        self, requested: str, call: "_JsonRpcCall", scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Answer 400 + -32022, naming the versions this server does speak."""
        if requested not in self._reported_versions:
            self._reported_versions.add(requested)
            logger.info(
                "Client asked for MCP protocol %s; answering -32022 with supported=%s",
                requested,
                ",".join(self.supported),
            )
        await _json_rpc_error(
            status=400,
            code=_UNSUPPORTED_PROTOCOL_VERSION,
            message="Unsupported protocol version",
            request_id=call.id,
            data={"supported": list(self.supported), "requested": requested},
            scope=scope,
            receive=receive,
            send=send,
        )

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


def compat_middleware(enabled: bool = True) -> list[Middleware]:
    """Return the HTTP middleware stack for the server.

    Args:
        enabled: False returns an empty stack, restoring plain SDK behaviour.

    Returns:
        A list to pass as ``middleware=`` to FastMCP's HTTP app.
    """
    if not enabled:
        return []
    return [Middleware(ProtocolCompatMiddleware)]

"""Which Superset client the current tool call runs on.

Tool modules bind their client once, at registration time
(``register_*_tools``), so per-user behaviour cannot come from re-binding them.
Instead they bind :data:`current_client` - a proxy that resolves the right client
on every call:

* ``google-sso`` mode: the caller's Google identity (verified by FastMCP) is
  mapped to a Superset user, and the call runs on a client that acts as them.
* ``service`` mode: everything runs on the single service-account client, exactly
  as upstream does.

:func:`configure` is called once by :mod:`mcp_superset.server` at start-up.
"""

import logging
from typing import Any

from fastmcp.server.dependencies import get_access_token

from mcp_superset.client import SupersetClient
from mcp_superset.identity import SupersetIdentityError, SupersetUser, UserClientRegistry

logger = logging.getLogger(__name__)

_service_client: SupersetClient | None = None
_registry: UserClientRegistry | None = None
_allowed_domains: frozenset[str] = frozenset()


def configure(
    service_client: SupersetClient,
    registry: UserClientRegistry | None = None,
    allowed_domains: frozenset[str] | set[str] | None = None,
) -> None:
    """Wire up the clients used for tool calls.

    Args:
        service_client: Client authenticated as the service account. Used for
            directory lookups, and for every call when no registry is given.
        registry: Per-user client registry; enables per-user mode when set.
        allowed_domains: E-mail domains allowed to use the server. Empty means
            every domain the identity provider accepts.
    """
    global _service_client, _registry, _allowed_domains
    _service_client = service_client
    _registry = registry
    _allowed_domains = frozenset(d.strip().lower().lstrip("@") for d in (allowed_domains or ()) if d.strip())


def per_user_mode() -> bool:
    """Return True when calls are executed as the authenticated SSO user."""
    return _registry is not None


def caller_email() -> str:
    """Return the verified e-mail address of the current caller.

    Returns:
        The caller's e-mail address, lower-cased.

    Raises:
        SupersetIdentityError: If the request carries no usable identity, or the
            e-mail's domain is not allowed.
    """
    token = get_access_token()
    if token is None:
        raise SupersetIdentityError(
            "This request is not authenticated. Connect to the MCP server with Google "
            "sign-in so it can act on Superset as you."
        )
    email = (token.claims or {}).get("email")
    if not email:
        raise SupersetIdentityError(
            "The access token carries no e-mail address. Re-authenticate and grant the "
            "'email' scope so the server can find your Superset account."
        )
    email = str(email).strip().lower()
    domain = email.rpartition("@")[2]
    if _allowed_domains and domain not in _allowed_domains:
        logger.warning("Rejected caller %s: domain not allowed", email)
        raise SupersetIdentityError(
            f"Account {email} is not allowed on this server (allowed domains: {', '.join(sorted(_allowed_domains))})."
        )
    return email


async def resolve_client() -> SupersetClient:
    """Return the Superset client for the current call.

    Returns:
        The per-user client in ``google-sso`` mode, else the service client.

    Raises:
        SupersetIdentityError: If no client can be resolved for the caller.
    """
    if _registry is None:
        if _service_client is None:
            raise SupersetIdentityError("The MCP server is not configured yet (no Superset client).")
        return _service_client
    client, _user = await _registry.client_for(caller_email())
    return client


async def resolve_identity() -> SupersetUser | None:
    """Return the Superset user the current call acts as, if per-user mode is on."""
    if _registry is None:
        return None
    _client, user = await _registry.client_for(caller_email())
    return user


class _CurrentClientProxy:
    """Delegates the ``SupersetClient`` API to the client of the current caller.

    Only the methods the tools actually use are exposed, so an unsupported call
    fails loudly at import/lint time rather than silently reaching the wrong user's
    client.
    """

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a GET request as the current caller."""
        client = await resolve_client()
        return await client.get(endpoint, params=params)

    async def post(self, endpoint: str, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a POST request as the current caller."""
        client = await resolve_client()
        return await client.post(endpoint, json_data=json_data)

    async def put(self, endpoint: str, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a PUT request as the current caller."""
        client = await resolve_client()
        return await client.put(endpoint, json_data=json_data)

    async def delete(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a DELETE request as the current caller."""
        client = await resolve_client()
        return await client.delete(endpoint, params=params)

    async def get_raw(self, endpoint: str, params: dict[str, Any] | None = None) -> bytes:
        """Send a GET request as the current caller and return raw bytes."""
        client = await resolve_client()
        return await client.get_raw(endpoint, params=params)

    async def post_form(self, endpoint: str, files: dict, data: dict | None = None) -> dict[str, Any]:
        """Send a multipart POST as the current caller."""
        client = await resolve_client()
        return await client.post_form(endpoint, files=files, data=data)

    async def get_page(
        self,
        endpoint: str,
        page: int = 0,
        page_size: int = 100,
        q: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch one RISON-paginated page as the current caller."""
        client = await resolve_client()
        return await client.get_page(endpoint, page=page, page_size=page_size, q=q, extra_params=extra_params)

    async def get_all(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> dict[str, Any]:
        """Fetch every page as the current caller."""
        client = await resolve_client()
        return await client.get_all(endpoint, params=params, page_size=page_size, max_pages=max_pages)

    @property
    def base_url(self) -> str:
        """Base URL of the Superset instance (identical for every caller)."""
        if _service_client is None:
            raise SupersetIdentityError("The MCP server is not configured yet (no Superset client).")
        return _service_client.base_url


current_client = _CurrentClientProxy()

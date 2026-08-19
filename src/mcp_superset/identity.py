"""Per-user identity: act on Superset as the SSO user who called the tool.

The MCP server authenticates callers with Google (see :mod:`mcp_superset.server`),
so every tool call carries a verified e-mail address. Superset users provisioned
through SSO have no password usable with ``POST /api/v1/security/login``, so this
module instead mints the very same Flask-JWT-Extended access token Superset issues
itself: HS256, ``sub`` = the user's id, signed with Superset's JWT secret. Superset
then applies that user's own roles, row-level security and ownership to every
request - no permission logic is duplicated here.

Trust model: the process holds Superset's JWT secret and can therefore act as any
Superset user. It only ever does so for an e-mail address verified by Google and
allowed by ``SUPERSET_MCP_ALLOWED_DOMAINS``.
"""

import asyncio
import logging
import time
import uuid

import httpx
import jwt as pyjwt

from mcp_superset.client import SupersetClient

logger = logging.getLogger(__name__)

# Re-mint slightly before the token really expires so a request never travels
# with a token that dies in flight.
_TOKEN_SAFETY_MARGIN = 60


class SupersetIdentityError(Exception):
    """The caller could not be mapped onto a usable Superset user.

    The message is user-facing: it is returned to the caller, so it explains what
    to do rather than what failed internally.
    """


class ImpersonationJwtAuth:
    """Auth strategy that signs Superset API tokens for one specific user.

    Implements the same :class:`~mcp_superset.auth.AuthStrategy` protocol as
    ``JwtAuthManager`` and ``CookieAuthManager``, so ``SupersetClient`` needs no
    changes. The token layout mirrors what Superset's own
    ``/api/v1/security/login`` returns (``fresh``/``iat``/``jti``/``type``/``sub``/
    ``nbf``/``csrf``/``exp``, HS256).
    """

    def __init__(
        self,
        base_url: str,
        user_id: int,
        secret: str,
        algorithm: str = "HS256",
        ttl_seconds: int = 600,
    ):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.secret = secret
        self.algorithm = algorithm
        self.ttl_seconds = ttl_seconds

        self._token: str | None = None
        self._token_expires_at: float = 0
        self._csrf_token: str | None = None

    @property
    def auth_failure_hint(self) -> str | None:
        """Explain a 401 in impersonation mode."""
        return (
            "Superset rejected the minted token - check that SUPERSET_JWT_SECRET matches "
            "Superset's SECRET_KEY (JWT_SECRET_KEY) and that the user is still active."
        )

    def _mint(self) -> str:
        """Sign a fresh access token for this user and cache it."""
        now = int(time.time())
        payload = {
            "fresh": False,
            "iat": now,
            "jti": str(uuid.uuid4()),
            "type": "access",
            "sub": str(self.user_id),
            "nbf": now,
            "csrf": str(uuid.uuid4()),
            "exp": now + self.ttl_seconds,
        }
        self._token = pyjwt.encode(payload, self.secret, algorithm=self.algorithm)
        self._token_expires_at = now + self.ttl_seconds
        # A CSRF token is bound to the token it was fetched with.
        self._csrf_token = None
        return self._token

    def _current_token(self) -> str:
        """Return the cached token, minting a new one when close to expiry."""
        if self._token and time.time() < self._token_expires_at - _TOKEN_SAFETY_MARGIN:
            return self._token
        return self._mint()

    async def apply_auth(self, client: httpx.AsyncClient, headers: dict[str, str]) -> None:
        """Set the Authorization header with a token minted for this user.

        Args:
            client: httpx async client (unused; kept for interface parity).
            headers: Mutable header dict to inject the token into.
        """
        headers["Authorization"] = f"Bearer {self._current_token()}"

    async def get_csrf_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid CSRF token, fetching one as this user if necessary.

        Args:
            client: httpx async client used for HTTP requests.

        Returns:
            A CSRF token string.
        """
        if self._csrf_token:
            return self._csrf_token
        url = f"{self.base_url}/api/v1/security/csrf_token/"
        headers = {
            "Authorization": f"Bearer {self._current_token()}",
            "Referer": self.base_url,
        }
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        self._csrf_token = resp.json()["result"]
        return self._csrf_token

    def invalidate(self) -> None:
        """Drop the cached token so the next request mints a new one."""
        self._token = None
        self._token_expires_at = 0
        self._csrf_token = None

    def invalidate_csrf(self) -> None:
        """Reset only the cached CSRF token."""
        self._csrf_token = None


class SupersetUser:
    """Minimal view of the Superset user a call is executed as."""

    __slots__ = ("id", "username", "email")

    def __init__(self, id: int, username: str, email: str):
        self.id = id
        self.username = username
        self.email = email

    def __repr__(self) -> str:
        return f"SupersetUser(id={self.id}, username={self.username!r})"


class UserDirectory:
    """Resolves an SSO e-mail address to a Superset user, with caching.

    Lookups go through a service account client (an Admin), because the caller's
    own token cannot be minted before their user id is known.
    """

    def __init__(
        self,
        service_client: SupersetClient,
        cache_ttl: int = 300,
        auto_create: bool = False,
        default_role: str = "Gamma",
    ):
        self.service_client = service_client
        self.cache_ttl = cache_ttl
        self.auto_create = auto_create
        self.default_role = default_role
        self._cache: dict[str, tuple[SupersetUser, float]] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, email: str) -> SupersetUser:
        """Return the Superset user owning this e-mail address.

        Args:
            email: Verified e-mail address of the caller.

        Returns:
            The matching :class:`SupersetUser`.

        Raises:
            SupersetIdentityError: If no single active Superset user matches.
        """
        key = email.strip().lower()
        cached = self._cache.get(key)
        if cached and time.time() < cached[1]:
            return cached[0]

        async with self._lock:
            # Another coroutine may have populated the cache while we waited.
            cached = self._cache.get(key)
            if cached and time.time() < cached[1]:
                return cached[0]

            user = await self._lookup(key)
            if user is None and self.auto_create:
                user = await self._create(key)
            if user is None:
                raise SupersetIdentityError(
                    f"No Superset account found for {email}. Sign in to Superset once "
                    "with Google (the SSO login creates the account), then retry."
                )
            self._cache[key] = (user, time.time() + self.cache_ttl)
            return user

    async def _lookup(self, email: str) -> SupersetUser | None:
        """Find an active Superset user by e-mail (filtered query, then full scan)."""
        if "'" in email:
            raise SupersetIdentityError(f"Invalid e-mail address: {email!r}")

        candidates: list[dict] = []
        try:
            resp = await self.service_client.get(
                "/api/v1/security/users/",
                params={"q": f"(filters:!((col:email,opr:eq,value:'{email}')))"},
            )
            candidates = resp.get("result", [])
        except Exception as exc:  # noqa: BLE001 - fall back to a full scan
            logger.debug("Filtered user lookup failed (%s); falling back to full scan", exc)

        if not candidates:
            resp = await self.service_client.get_all("/api/v1/security/users/")
            candidates = resp.get("result", [])

        matches = [u for u in candidates if (u.get("email") or "").strip().lower() == email]
        if not matches:
            return None
        if len(matches) > 1:
            ids = ", ".join(str(u.get("id")) for u in matches)
            raise SupersetIdentityError(
                f"Several Superset accounts share the e-mail {email} (ids: {ids}). "
                "Ask an administrator to de-duplicate them."
            )

        found = matches[0]
        if found.get("active") is False:
            raise SupersetIdentityError(
                f"The Superset account for {email} is deactivated. Ask an administrator to enable it."
            )
        return SupersetUser(
            id=int(found["id"]),
            username=found.get("username") or email,
            email=found.get("email") or email,
        )

    async def _create(self, email: str) -> SupersetUser:
        """Create a Superset user for this e-mail with the configured default role.

        Opt-in (``SUPERSET_MCP_AUTO_CREATE_USERS``). Prefer letting Superset's own
        SSO login create the account: Flask-AppBuilder derives the username from
        the OAuth provider, so an account pre-created here under a different
        username can collide with it on the unique e-mail column at first UI login.
        """
        role_id = await self._role_id(self.default_role)
        local_part = email.split("@", 1)[0]
        payload = {
            "username": email,
            "first_name": local_part[:60] or "SSO",
            "last_name": "SSO",
            "email": email,
            "active": True,
            "roles": [role_id],
            # SSO users authenticate through Google; this password is never used
            # or shown, it only satisfies the API schema.
            "password": uuid.uuid4().hex + uuid.uuid4().hex,
        }
        created = await self.service_client.post("/api/v1/security/users/", json_data=payload)
        user_id = created.get("id") or (created.get("result") or {}).get("id")
        if not user_id:
            raise SupersetIdentityError(f"Could not create a Superset account for {email}.")
        logger.info("Created Superset user for %s (id=%s, role=%s)", email, user_id, self.default_role)
        return SupersetUser(id=int(user_id), username=email, email=email)

    async def _role_id(self, role_name: str) -> int:
        """Return the id of a role by name."""
        resp = await self.service_client.get(
            "/api/v1/security/roles/",
            params={"q": f"(filters:!((col:name,opr:eq,value:'{role_name}')))"},
        )
        for role in resp.get("result", []):
            if role.get("name") == role_name:
                return int(role["id"])
        raise SupersetIdentityError(f"Role {role_name!r} does not exist in Superset (SUPERSET_MCP_DEFAULT_ROLE).")


class UserClientRegistry:
    """Keeps one :class:`SupersetClient` per SSO user, evicting idle ones."""

    def __init__(
        self,
        base_url: str,
        jwt_secret: str,
        directory: UserDirectory,
        algorithm: str = "HS256",
        token_ttl: int = 600,
        idle_ttl: int = 1800,
        max_clients: int = 200,
    ):
        self.base_url = base_url.rstrip("/")
        self.jwt_secret = jwt_secret
        self.directory = directory
        self.algorithm = algorithm
        self.token_ttl = token_ttl
        self.idle_ttl = idle_ttl
        self.max_clients = max_clients
        self._clients: dict[str, tuple[SupersetClient, SupersetUser, float]] = {}
        self._lock = asyncio.Lock()

    async def client_for(self, email: str) -> tuple[SupersetClient, SupersetUser]:
        """Return the client that acts as the Superset user behind this e-mail.

        Args:
            email: Verified e-mail address of the caller.

        Returns:
            Tuple of (client, resolved Superset user).
        """
        key = email.strip().lower()
        user = await self.directory.resolve(key)

        async with self._lock:
            await self._evict_idle()
            entry = self._clients.get(key)
            if entry and entry[1].id == user.id:
                self._clients[key] = (entry[0], entry[1], time.time())
                return entry[0], entry[1]

            if entry:
                # The e-mail now maps to a different user id - drop the stale client.
                await entry[0].close()

            client = SupersetClient(
                auth_manager=ImpersonationJwtAuth(
                    base_url=self.base_url,
                    user_id=user.id,
                    secret=self.jwt_secret,
                    algorithm=self.algorithm,
                    ttl_seconds=self.token_ttl,
                ),
                base_url=self.base_url,
            )
            self._clients[key] = (client, user, time.time())
            logger.info("Acting as Superset user %s (id=%s) for %s", user.username, user.id, key)
            return client, user

    async def _evict_idle(self) -> None:
        """Close clients unused for longer than idle_ttl, then enforce max_clients."""
        cutoff = time.time() - self.idle_ttl
        for key in [k for k, v in self._clients.items() if v[2] < cutoff]:
            client, _, _ = self._clients.pop(key)
            await client.close()

        while len(self._clients) > self.max_clients:
            oldest = min(self._clients, key=lambda k: self._clients[k][2])
            client, _, _ = self._clients.pop(oldest)
            await client.close()

    async def aclose(self) -> None:
        """Close every cached client."""
        async with self._lock:
            for client, _, _ in self._clients.values():
                await client.close()
            self._clients.clear()

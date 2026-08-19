"""MCP server entry point for Apache Superset."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_superset import __version__, context
from mcp_superset.auth import build_auth_strategy
from mcp_superset.client import SupersetClient
from mcp_superset.identity import UserClientRegistry, UserDirectory
from mcp_superset.tools import register_all_tools

logger = logging.getLogger(__name__)

# Load .env - custom path via env var, or auto-detect from package directory
_custom_env = os.environ.get("SUPERSET_MCP_ENV_FILE")
if _custom_env:
    load_dotenv(Path(_custom_env))
else:
    _env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    load_dotenv(_env_path)


def _flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Configuration
SUPERSET_BASE_URL = os.getenv("SUPERSET_BASE_URL", "")
SUPERSET_USERNAME = os.getenv("SUPERSET_USERNAME")
SUPERSET_PASSWORD = os.getenv("SUPERSET_PASSWORD")
SUPERSET_AUTH_PROVIDER = os.getenv("SUPERSET_AUTH_PROVIDER", "db")
SUPERSET_SESSION_COOKIE = os.getenv("SUPERSET_SESSION_COOKIE")
SUPERSET_SESSION_COOKIE_NAME = os.getenv("SUPERSET_SESSION_COOKIE_NAME", "session")

# Per-user (SSO) mode
AUTH_MODE = os.getenv("SUPERSET_MCP_AUTH_MODE", "service").strip().lower()
PUBLIC_URL = os.getenv("SUPERSET_MCP_PUBLIC_URL", "").rstrip("/")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SUPERSET_JWT_SECRET = os.getenv("SUPERSET_JWT_SECRET") or os.getenv("SUPERSET_SECRET_KEY")
SUPERSET_JWT_ALGORITHM = os.getenv("SUPERSET_JWT_ALGORITHM", "HS256")
ALLOWED_DOMAINS = frozenset(
    d.strip().lower().lstrip("@") for d in os.getenv("SUPERSET_MCP_ALLOWED_DOMAINS", "").split(",") if d.strip()
)
TOKEN_TTL = int(os.getenv("SUPERSET_MCP_TOKEN_TTL", "600"))
AUTO_CREATE_USERS = _flag("SUPERSET_MCP_AUTO_CREATE_USERS", False)
DEFAULT_ROLE = os.getenv("SUPERSET_MCP_DEFAULT_ROLE", "Gamma")

if AUTH_MODE not in ("service", "google-sso"):
    raise ValueError(f"SUPERSET_MCP_AUTH_MODE must be 'service' or 'google-sso', got {AUTH_MODE!r}.")

# The service account authenticates the server itself. In google-sso mode it is
# only used to look up (and optionally create) the Superset user behind an e-mail
# address; the tool calls themselves run as that user.
auth_manager = build_auth_strategy(
    base_url=SUPERSET_BASE_URL,
    session_cookie=SUPERSET_SESSION_COOKIE,
    cookie_name=SUPERSET_SESSION_COOKIE_NAME,
    username=SUPERSET_USERNAME,
    password=SUPERSET_PASSWORD,
    provider=SUPERSET_AUTH_PROVIDER,
)

superset_client = SupersetClient(auth_manager=auth_manager, base_url=SUPERSET_BASE_URL)

fastmcp_auth = None
user_registry: UserClientRegistry | None = None

if AUTH_MODE == "google-sso":
    from fastmcp.server.auth.providers.google import GoogleProvider

    missing = [
        name
        for name, value in (
            ("SUPERSET_MCP_PUBLIC_URL", PUBLIC_URL),
            ("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID),
            ("GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET),
            ("SUPERSET_JWT_SECRET", SUPERSET_JWT_SECRET),
        )
        if not value
    ]
    if missing:
        raise ValueError("SUPERSET_MCP_AUTH_MODE=google-sso requires: " + ", ".join(missing) + ".")

    # The check above guarantees these are set; bind them as plain strings.
    google_client_id: str = GOOGLE_CLIENT_ID or ""
    google_client_secret: str = GOOGLE_CLIENT_SECRET or ""
    jwt_secret: str = SUPERSET_JWT_SECRET or ""

    # Restrict Google's account chooser to the Workspace domain when exactly one
    # domain is allowed - users then cannot even pick a personal account.
    extra_authorize_params = {}
    hosted_domain = os.getenv("GOOGLE_HOSTED_DOMAIN") or (
        next(iter(ALLOWED_DOMAINS)) if len(ALLOWED_DOMAINS) == 1 else ""
    )
    if hosted_domain:
        extra_authorize_params["hd"] = hosted_domain

    fastmcp_auth = GoogleProvider(
        client_id=google_client_id,
        client_secret=google_client_secret,
        base_url=PUBLIC_URL,
        required_scopes=["openid", "email", "profile"],
        extra_authorize_params=extra_authorize_params or None,
    )

    user_registry = UserClientRegistry(
        base_url=SUPERSET_BASE_URL,
        jwt_secret=jwt_secret,
        algorithm=SUPERSET_JWT_ALGORITHM,
        token_ttl=TOKEN_TTL,
        directory=UserDirectory(
            service_client=superset_client,
            auto_create=AUTO_CREATE_USERS,
            default_role=DEFAULT_ROLE,
        ),
    )

context.configure(
    service_client=superset_client,
    registry=user_registry,
    allowed_domains=ALLOWED_DOMAINS,
)

# Create MCP server
mcp = FastMCP(
    name="superset",
    instructions=(
        "MCP server for managing Apache Superset. "
        "Provides tools for dashboards, charts, databases, datasets, "
        "SQL queries, users, roles, permissions, and other Superset resources."
        + (
            " Every call runs as the signed-in Superset user, so results and permissions are theirs."
            if AUTH_MODE == "google-sso"
            else ""
        )
    ),
    auth=fastmcp_auth,
)

# Register all tools
register_all_tools(mcp)


# Health check endpoint (no auth, no Superset API calls)
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "superset_url": SUPERSET_BASE_URL,
            "auth_mode": AUTH_MODE,
        }
    )


logger.info(
    "mcp-superset %s starting: superset=%s auth_mode=%s allowed_domains=%s",
    __version__,
    SUPERSET_BASE_URL,
    AUTH_MODE,
    ",".join(sorted(ALLOWED_DOMAINS)) or "*",
)


if __name__ == "__main__":
    host = os.getenv("SUPERSET_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("SUPERSET_MCP_PORT", "8001"))
    mcp.run(transport="streamable-http", host=host, port=port, stateless_http=True)

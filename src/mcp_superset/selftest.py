"""Diagnostic: prove that the server can act as a given Superset user.

Runs the same code path a tool call takes in ``google-sso`` mode - resolve the
e-mail to a Superset user, mint that user's API token, then call Superset - but
without needing a browser or a Google login. Use it after deployment or whenever
Superset's secret key changes:

    python -m mcp_superset.selftest someone@example.com

Prints the identity Superset reports back and what that user can see, so a
mismatch (wrong secret, wrong user, inactive account) shows up immediately. No
token or secret is ever printed.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from mcp_superset.auth import build_auth_strategy
from mcp_superset.client import SupersetAPIError, SupersetClient
from mcp_superset.identity import SupersetIdentityError, UserClientRegistry, UserDirectory


async def run(email: str) -> int:
    """Resolve the e-mail, act as that user and report what Superset says.

    Args:
        email: E-mail address of the Superset user to impersonate.

    Returns:
        Process exit code (0 on success).
    """
    base_url = os.getenv("SUPERSET_BASE_URL", "")
    jwt_secret = os.getenv("SUPERSET_JWT_SECRET") or os.getenv("SUPERSET_SECRET_KEY")
    if not base_url or not jwt_secret:
        print("SUPERSET_BASE_URL and SUPERSET_JWT_SECRET (or SUPERSET_SECRET_KEY) must be set.")
        return 2

    service_client = SupersetClient(
        auth_manager=build_auth_strategy(
            base_url=base_url,
            session_cookie=os.getenv("SUPERSET_SESSION_COOKIE"),
            cookie_name=os.getenv("SUPERSET_SESSION_COOKIE_NAME", "session"),
            username=os.getenv("SUPERSET_USERNAME"),
            password=os.getenv("SUPERSET_PASSWORD"),
            provider=os.getenv("SUPERSET_AUTH_PROVIDER", "db"),
        ),
        base_url=base_url,
    )
    registry = UserClientRegistry(
        base_url=base_url,
        jwt_secret=jwt_secret,
        algorithm=os.getenv("SUPERSET_JWT_ALGORITHM", "HS256"),
        directory=UserDirectory(service_client=service_client),
    )

    try:
        print(f"Superset      : {base_url}")
        service_me = await service_client.get("/api/v1/me/")
        print(f"service account: {_describe(service_me)}")

        user_client, user = await registry.client_for(email)
        print(f"resolved      : {email} -> id={user.id} username={user.username}")

        me = await user_client.get("/api/v1/me/")
        print(f"acting as     : {_describe(me)}")

        for label, endpoint in (
            ("dashboards", "/api/v1/dashboard/"),
            ("charts", "/api/v1/chart/"),
            ("datasets", "/api/v1/dataset/"),
        ):
            visible = await user_client.get(endpoint, params={"q": "(page_size:1)"})
            print(f"visible {label:<11}: {visible.get('count')}")

        reported = (me.get("result") or {}).get("email") or ""
        if reported.strip().lower() != email.strip().lower():
            print(f"MISMATCH: Superset reports {reported!r} for a token minted for {email!r}.")
            return 1
        print("OK: Superset executes requests as this user.")
        return 0
    except (SupersetIdentityError, SupersetAPIError) as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        await registry.aclose()
        await service_client.close()


def _describe(me: dict) -> str:
    """Format the /api/v1/me/ payload as a one-liner."""
    result = me.get("result") or me
    roles = result.get("roles") or []
    role_names = [r.get("name") if isinstance(r, dict) else str(r) for r in roles]
    return (
        f"id={result.get('id')} username={result.get('username')} "
        f"email={result.get('email')} roles={role_names or 'n/a'}"
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="mcp-superset-selftest",
        description="Verify that the server can act as a given Superset user.",
    )
    parser.add_argument("email", help="E-mail address of the Superset user to act as")
    parser.add_argument("--env-file", default=None, help="Path to a .env file to load first")
    args = parser.parse_args()

    env_file = args.env_file or os.environ.get("SUPERSET_MCP_ENV_FILE")
    if env_file:
        load_dotenv(Path(env_file))
    else:
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

    sys.exit(asyncio.run(run(args.email)))


if __name__ == "__main__":
    main()

"""Diagnostic: prove that the server can act as a given Superset user.

Runs the same code path a tool call takes in ``google-sso`` mode - resolve the
e-mail to a Superset user, mint that user's API token, then call Superset - but
without needing a browser or a Google login. Use it after deployment or whenever
Superset's secret key changes:

    python -m mcp_superset.selftest someone@example.com

Prints the identity Superset reports back and what that user can see, so a
mismatch (wrong secret, wrong user, inactive account) shows up immediately. No
token or secret is ever printed.

A tightly restricted role is a normal outcome, not a failure: 401 means Superset
rejected the token, while 403 means it accepted it and then applied that user's
permissions - which is exactly what this server is for. The two are reported
differently.
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

VISIBILITY_ENDPOINTS = (
    ("dashboards", "/api/v1/dashboard/"),
    ("charts", "/api/v1/chart/"),
    ("datasets", "/api/v1/dataset/"),
)


async def _probe(client: SupersetClient, endpoint: str, params: dict | None = None) -> tuple[dict | None, int | None]:
    """Call an endpoint as the client's user.

    Args:
        client: Client to call with.
        endpoint: API endpoint path.
        params: Optional query parameters.

    Returns:
        (payload, None) on success, or (None, status_code) when Superset refused.
    """
    try:
        return await client.get(endpoint, params=params), None
    except SupersetAPIError as exc:
        return None, exc.status_code


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
        print(f"Superset       : {base_url}")
        service_me, _ = await _probe(service_client, "/api/v1/me/")
        print(f"service account: {_describe(service_me) if service_me else 'unavailable'}")

        user_client, user = await registry.client_for(email)
        print(f"resolved       : {email} -> id={user.id} username={user.username}")

        statuses: list[int] = []
        me, me_status = await _probe(user_client, "/api/v1/me/")
        if me is not None:
            print(f"acting as      : {_describe(me)}")
        else:
            if me_status is not None:
                statuses.append(me_status)
            print(
                f"acting as      : Superset did not return the profile ({me_status}) - "
                "expected for a minimal role without read access to /api/v1/me/"
            )

        for label, endpoint in VISIBILITY_ENDPOINTS:
            data, status = await _probe(user_client, endpoint, {"q": "(page_size:1)"})
            if data is not None:
                print(f"visible {label:<8}: {data.get('count')}")
            else:
                if status is not None:
                    statuses.append(status)
                reason = "no permission" if status == 403 else "refused"
                print(f"visible {label:<8}: {reason} ({status})")

        if 401 in statuses:
            print(
                "FAILED: Superset rejected the minted token (401). Check that "
                "SUPERSET_JWT_SECRET matches Superset's SECRET_KEY."
            )
            return 1

        if me is not None:
            reported = (me.get("result") or {}).get("email") or ""
            if reported.strip().lower() != email.strip().lower():
                print(f"MISMATCH: Superset reports {reported!r} for a token minted for {email!r}.")
                return 1
            print("OK: Superset executes requests as this user.")
            return 0

        print(
            "OK: the token was accepted (403 means authenticated but not authorised), "
            "so requests run with this user's own - here very limited - permissions."
        )
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

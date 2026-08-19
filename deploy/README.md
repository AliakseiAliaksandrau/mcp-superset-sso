# Deployment

Reference configuration for running the server in `google-sso` mode next to a
Superset docker-compose deployment.

| File | Purpose |
|---|---|
| `Dockerfile` | Image build (`docker build -f deploy/Dockerfile .`) |
| `docker-compose.mcp-sso.yml` | Service block to merge into Superset's compose file |
| `nginx-mcp-sso.conf.example` | TLS reverse proxy vhost |

## Order of operations

1. **DNS + TLS** — point the public hostname at the reverse proxy and issue a
   certificate (`certbot --nginx -d mcp-sso.example.com`). Google only redirects
   to HTTPS, so this comes first. No DNS access? An existing hostname works too:
   add the server's routes as extra `location` blocks on a vhost whose root paths
   are free (variant B in `nginx-mcp-sso.conf.example`) and set
   `SUPERSET_MCP_PUBLIC_URL` to that hostname.
2. **Google OAuth client** (Web application) — add
   `https://mcp-sso.example.com/auth/callback` as an authorized redirect URI.
   Put the client id and secret in `docker/.env-local`:

   ```env
   GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=GOCSPX-...
   ```

3. **Clone and start** — clone this repository next to the compose file, copy the
   service block from `docker-compose.mcp-sso.yml` into it (adjust
   `SUPERSET_MCP_PUBLIC_URL` and `SUPERSET_MCP_ALLOWED_DOMAINS`), then:

   ```bash
   docker compose -f docker-compose-non-dev.yml up -d --build superset-mcp-sso
   ```

4. **Verify before handing out the URL**:

   ```bash
   curl https://mcp-sso.example.com/health                 # {"status":"ok","auth_mode":"google-sso"}
   curl -i -X POST https://mcp-sso.example.com/mcp         # 401 + WWW-Authenticate
   docker exec superset_mcp_sso mcp-superset-selftest someone@example.com
   ```

   The self-test resolves the e-mail, mints that user's Superset token and reports
   the identity Superset gives back — it catches a wrong `SUPERSET_JWT_SECRET` or a
   missing account without involving a browser.

5. **Connect a client**:

   ```bash
   claude mcp add --transport http superset-sso https://mcp-sso.example.com/mcp
   ```

   Then authenticate through `/mcp` in Claude Code, or add the same URL as a
   custom connector in the Claude apps. `superset_get_current_user` confirms whose
   permissions apply.

## Notes

- The Superset user behind an e-mail must already exist. With Superset's own Google
  SSO enabled, one UI login creates it; otherwise create it manually or set
  `SUPERSET_MCP_AUTO_CREATE_USERS=true` (see the caveat in the main README).
- The container needs Superset's `SECRET_KEY`. In the compose block above it is
  passed through as `SUPERSET_JWT_SECRET: ${SUPERSET_SECRET_KEY}`, which the
  Superset `docker/.env` already defines.
- Optionally firewall the published port so only the reverse proxy can reach it —
  unauthenticated requests are already rejected, but there is no reason to expose
  the origin directly.

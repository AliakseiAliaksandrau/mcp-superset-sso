# Container image for mcp-superset-sso.
# Build from the repository root:  docker build -f deploy/Dockerfile -t mcp-superset-sso .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SUPERSET_MCP_HOST=0.0.0.0 \
    SUPERSET_MCP_PORT=8001

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 mcp \
    && mkdir -p /data \
    && chown mcp /data

# Mount a volume here and point SUPERSET_MCP_CLIENT_STORE_DIR at it so OAuth client
# registrations survive a restart (otherwise every user re-authorizes after a deploy).
VOLUME ["/data"]

USER mcp

EXPOSE 8001

# Configuration comes from the environment (see .env.example); no secrets are baked in.
ENTRYPOINT ["mcp-superset"]
CMD ["--transport", "streamable-http"]

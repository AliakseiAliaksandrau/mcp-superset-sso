"""Tools for SQL Lab and query management in Superset."""

import asyncio
import json
import re
import time

_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$]")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _blank_sql_literals(sql: str) -> str:
    """Replace comments, string literals and quoted identifiers with a space.

    Required for correct DDL/DML detection: a comment before a dangerous command
    must not hide it ('/* */ DROP TABLE ...'), and a keyword inside a literal must
    not trigger a false positive.

    Done in a single left-to-right pass, because stripping comments and strings in
    separate passes lets one confuse the other: in ``SELECT '--'; DROP TABLE t`` a
    comment pass eats the rest of the line, DROP included. Handles '...' (with ''
    escapes), E'...' (with backslash escapes), $tag$...$tag$, "quoted identifiers",
    -- line comments and nested /* */ comments. An unterminated literal or comment
    swallows the rest of the text; the database rejects such SQL anyway.

    Args:
        sql: Raw SQL string.

    Returns:
        SQL with every comment, literal and quoted identifier replaced by a space.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
        elif ch == "/" and nxt == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
        elif ch == "'":
            backslash_escapes = i > 0 and sql[i - 1] in "eE" and (i < 2 or not _IDENT_CHAR.match(sql[i - 2]))
            i += 1
            while i < n:
                if backslash_escapes and sql[i] == "\\":
                    i += 2
                elif sql[i] == "'":
                    if sql.startswith("''", i):
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            out.append(" ")
        elif ch == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if sql.startswith('""', i):
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            out.append(" ")
        elif ch == "$" and (i == 0 or not _IDENT_CHAR.match(sql[i - 1])) and (tag := _DOLLAR_TAG.match(sql, i)):
            end = sql.find(tag.group(0), tag.end())
            i = n if end == -1 else end + len(tag.group(0))
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# DDL/DML keywords that must never be executed via MCP. Detected as
# WHOLE WORDS ANYWHERE in the query (not just at the start) to prevent
# bypasses via CTE (WITH ... DELETE), statement chaining (SELECT 1; DROP ...),
# parentheses ((DELETE FROM t)), a leading EXPLAIN/ANALYZE, or a PL/pgSQL
# anonymous block (DO $$ BEGIN EXECUTE '...' END $$) that hides the DDL in a
# string literal. Order matters: keywords that subsume others (TRUNCATE, MERGE)
# come first so the rejection message names the most specific operation.
_DANGEROUS_KEYWORDS = (
    "DROP",
    "TRUNCATE",
    "MERGE",
    "DELETE",
    "UPDATE",
    "INSERT",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "COPY",
    "DO",  # PL/pgSQL anonymous block
    "EXECUTE",  # dynamic SQL inside a DO block
)


def _detect_dangerous_sql(sql: str) -> str | None:
    """Return the first DDL/DML keyword found in the query, or None if safe.

    Blanks out comments, literals and quoted identifiers first, then matches each
    dangerous keyword as a whole word anywhere in the statement. Word boundaries
    keep legitimate identifiers safe (e.g. update_date, deleted_at, call_datetime).

    Args:
        sql: Raw SQL query.

    Returns:
        The matched dangerous keyword (uppercase), or None.
    """
    cleaned = _blank_sql_literals(sql).upper()
    for keyword in _DANGEROUS_KEYWORDS:
        if re.search(rf"\b{keyword}\b", cleaned):
            return keyword
    return None


SQLLAB_WAIT_SECONDS = 50.0
SQLLAB_MAX_WAIT_SECONDS = 300.0
_POLL_INTERVAL_SECONDS = 1.0
_FAILED_STATES = {"failed", "stopped", "timed_out"}


async def database_allows_async(client, database_id: int) -> bool:
    """Whether a database connection has "Asynchronous query execution" enabled."""
    info = await client.get(f"/api/v1/database/{database_id}")
    return bool(info.get("result", {}).get("allow_run_async"))


async def run_sqllab_query(
    client,
    payload: dict,
    *,
    run_async: bool,
    wait_seconds: float = SQLLAB_WAIT_SECONDS,
) -> dict:
    """Execute a SQL Lab query, synchronously or asynchronously.

    Synchronous execution holds the HTTP request open, so anything longer than the
    SQL Lab / HTTP client timeout (30-60 s) fails, and the query may keep running in
    the database with its outcome unknown. Asynchronous execution queues the query
    on a Celery worker and returns at once; this function then polls the query's
    status and, once it succeeds, fetches the result from the results backend, so a
    finished query looks exactly like a synchronous one. A query still running after
    ``wait_seconds`` is returned as ``status: running`` with its id, to be checked
    later with superset_query_get / superset_sqllab_results.

    Args:
        client: SupersetClient to use.
        payload: Body for /api/v1/sqllab/execute/ (runAsync is set here).
        run_async: Execute asynchronously (the connection must allow it).
        wait_seconds: How long to wait for an asynchronous query to finish.

    Returns:
        The SQL Lab result, or a status dict for failed / still-running queries.
    """
    payload = {**payload, "runAsync": run_async}
    started = await client.post("/api/v1/sqllab/execute/", json_data=payload)
    if not run_async:
        return started

    query = started.get("query") or {}
    query_id = query.get("queryId") or query.get("serverId")
    if not query_id or started.get("data") is not None:
        # Nothing to wait for: the response already is the result.
        return started

    deadline = time.monotonic() + max(0.0, min(wait_seconds, SQLLAB_MAX_WAIT_SECONDS))
    while True:
        info = (await client.get(f"/api/v1/query/{query_id}")).get("result", {})
        status = info.get("status")
        if status == "success":
            if info.get("results_key"):
                return await client.get(
                    "/api/v1/sqllab/results/",
                    params={"q": f"(key:{info['results_key']})"},
                )
            return {"status": status, "query_id": query_id, "rows": info.get("rows")}
        if status in _FAILED_STATES:
            return {"status": status, "query_id": query_id, "error": info.get("error_message")}
        if time.monotonic() >= deadline:
            return {
                "status": status or "running",
                "query_id": query_id,
                "client_id": query.get("id"),
                "hint": (
                    "The query is still running in the background. Check it later with "
                    "superset_query_get(query_id); when status is success, fetch rows with "
                    "superset_sqllab_results(results_key). Stop it with superset_query_stop(client_id)."
                ),
            }
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def register_query_tools(mcp):
    from mcp_superset.context import current_client as client

    @mcp.tool
    async def superset_sqllab_execute(
        database_id: int,
        sql: str,
        schema: str | None = None,
        catalog: str | None = None,
        tab_name: str | None = None,
        template_params: str | None = None,
        run_async: bool | None = None,
        wait_seconds: float = SQLLAB_WAIT_SECONDS,
    ) -> str:
        """Execute a SQL query via SQL Lab and return the result.

        IMPORTANT: before executing, make sure the SQL query is correct.
        Use superset_database_table_metadata or superset_database_tables
        to find actual table and column names.
        Maximum 1000 rows in the result (queryLimit).

        Long queries: on a connection with "Asynchronous query execution" enabled the
        query runs in the background and this tool waits up to wait_seconds for it.
        If it is still running then, the result is {"status": "running", "query_id": ...};
        check it later with superset_query_get and fetch rows with superset_sqllab_results.

        Args:
            database_id: Database connection ID (from superset_database_list).
            sql: SQL query to execute. Examples:
                - SELECT * FROM public.my_table LIMIT 10
                - SELECT count(*) FROM source.stat
            schema: Default schema for the query (e.g. "public", "source").
                If not specified, the database default schema is used.
            catalog: Database catalog (for databases with catalog support, optional).
            tab_name: Tab name in SQL Lab UI (optional, for organization).
            template_params: JSON string with Jinja template parameters (optional).
                Example: '{"start_date": "2024-01-01"}'
            run_async: Run in the background. Default: whatever the connection allows.
                Pass false to force synchronous execution.
            wait_seconds: How long to wait for a background query (max 300, default 50).
        """
        # Guard against DDL/DML. Detects dangerous keywords as whole words
        # ANYWHERE in the query (after stripping comments and string literals),
        # so CTE/chained/parenthesised/EXPLAIN-prefixed bypasses are caught.
        # NOTE: this is a best-effort safety net, not a full SQL parser — the
        # authoritative protection is allow_dml=false on the DB connection.
        dangerous = _detect_dangerous_sql(sql)
        if dangerous:
            return json.dumps(
                {
                    "error": (
                        f"REJECTED: SQL query contains '{dangerous}' — "
                        f"this is a modifying operation (DDL/DML). "
                        f"Executing such queries via MCP is prohibited. "
                        f"If the operation is truly needed — execute it "
                        f"directly via SQL Lab in the Superset UI."
                    )
                },
                ensure_ascii=False,
            )

        payload = {
            "database_id": database_id,
            "sql": sql,
            "queryLimit": 1000,
        }
        if schema is not None:
            payload["schema"] = schema
        if catalog is not None:
            payload["catalog"] = catalog
        if tab_name is not None:
            payload["tab"] = tab_name
        if template_params is not None:
            payload["templateParams"] = template_params
        if run_async is None:
            run_async = await database_allows_async(client, database_id)
        result = await run_sqllab_query(client, payload, run_async=run_async, wait_seconds=wait_seconds)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_sqllab_format_sql(sql: str) -> str:
        """Format a SQL query (pretty print with indentation).

        Args:
            sql: SQL query to format.
        """
        result = await client.post("/api/v1/sqllab/format_sql/", json_data={"sql": sql})
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_sqllab_results(results_key: str) -> str:
        """Retrieve results of a previously executed query by key.

        IMPORTANT: requires a configured Results Backend (Redis/S3) in Superset.
        Without it, returns 500. The key is taken from the results_key field of sqllab_execute response.

        Args:
            results_key: Results key from the superset_sqllab_execute response.
        """
        result = await client.get(
            "/api/v1/sqllab/results/",
            params={"q": f"(key:{results_key})"},
        )
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_sqllab_estimate_cost(
        database_id: int,
        sql: str,
        schema: str | None = None,
    ) -> str:
        """Estimate the cost of executing a SQL query (EXPLAIN).

        Not all database engines support this feature. PostgreSQL does.

        Args:
            database_id: Database connection ID.
            sql: SQL query to estimate.
            schema: Schema for context (e.g. "public").
        """
        payload = {"database_id": database_id, "sql": sql}
        if schema is not None:
            payload["schema"] = schema
        result = await client.post("/api/v1/sqllab/estimate/", json_data=payload)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_sqllab_export_csv(client_id: str) -> str:
        """Export query results to CSV format.

        IMPORTANT: requires a configured Results Backend (Redis/S3) in Superset.

        Args:
            client_id: Query client_id (from the superset_sqllab_execute result).
        """
        result = await client.get(f"/api/v1/sqllab/export/{client_id}/")
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_query_list(
        page: int = 0,
        page_size: int = 25,
        q: str | None = None,
        get_all: bool = False,
    ) -> str:
        """Retrieve the history of executed SQL queries.

        Args:
            page: Page number (starting from 0).
            page_size: Number of records per page (max 100).
            q: RISON filter for search. Examples:
                - By status: (filters:!((col:status,opr:eq,value:success)))
                - By database: (filters:!((col:database,opr:rel_o_m,value:1)))
            get_all: Retrieve ALL records with automatic pagination (ignores page/page_size).
        """
        if get_all:
            params = {}
            if q:
                params["q"] = q
            result = await client.get_all("/api/v1/query/", params=params)
        else:
            result = await client.get_page("/api/v1/query/", page, page_size, q)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_query_get(query_id: int) -> str:
        """Retrieve detailed information about a query from the history by ID.

        Args:
            query_id: Query ID (integer from query_list result).
        """
        result = await client.get(f"/api/v1/query/{query_id}")
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_query_stop(query_id: str) -> str:
        """Stop a running asynchronous query.

        Args:
            query_id: Query client_id to stop (string from sqllab_execute result).
        """
        result = await client.post("/api/v1/query/stop", json_data={"client_id": query_id})
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_saved_query_list(
        page: int = 0,
        page_size: int = 25,
        q: str | None = None,
        get_all: bool = False,
    ) -> str:
        """Retrieve a list of saved SQL queries.

        Args:
            page: Page number (starting from 0).
            page_size: Number of records per page (max 100).
            q: RISON filter for search. Examples:
                - By label: (filters:!((col:label,opr:ct,value:search_term)))
                - By database: (filters:!((col:database,opr:rel_o_m,value:1)))
            get_all: Retrieve ALL records with automatic pagination (ignores page/page_size).
        """
        if get_all:
            params = {}
            if q:
                params["q"] = q
            result = await client.get_all("/api/v1/saved_query/", params=params)
        else:
            result = await client.get_page("/api/v1/saved_query/", page, page_size, q)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_saved_query_create(
        label: str,
        db_id: int,
        sql: str,
        schema: str | None = None,
        description: str | None = None,
    ) -> str:
        """Create a saved SQL query for reuse.

        Args:
            label: Query name (displayed in the list).
            db_id: Database connection ID (from superset_database_list).
            sql: SQL query to save.
            schema: Default schema (e.g. "public").
            description: Query description.
        """
        payload = {"label": label, "db_id": db_id, "sql": sql}
        if schema is not None:
            payload["schema"] = schema
        if description is not None:
            payload["description"] = description
        result = await client.post("/api/v1/saved_query/", json_data=payload)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_saved_query_get(saved_query_id: int) -> str:
        """Retrieve a saved query by ID: SQL text, schema, description.

        Args:
            saved_query_id: Saved query ID (from saved_query_list).
        """
        result = await client.get(f"/api/v1/saved_query/{saved_query_id}")
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_saved_query_update(
        saved_query_id: int,
        label: str | None = None,
        sql: str | None = None,
        schema: str | None = None,
        description: str | None = None,
    ) -> str:
        """Update a saved query. Pass only the fields to change.

        Args:
            saved_query_id: Saved query ID.
            label: New name.
            sql: New SQL query.
            schema: New default schema.
            description: New description.
        """
        payload = {}
        if label is not None:
            payload["label"] = label
        if sql is not None:
            payload["sql"] = sql
        if schema is not None:
            payload["schema"] = schema
        if description is not None:
            payload["description"] = description
        result = await client.put(f"/api/v1/saved_query/{saved_query_id}", json_data=payload)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_saved_query_delete(
        saved_query_id: int,
        confirm_delete: bool = False,
    ) -> str:
        """Delete a saved query.

        Args:
            saved_query_id: Saved query ID to delete.
            confirm_delete: Deletion confirmation (REQUIRED).
        """
        if not confirm_delete:
            try:
                info = await client.get(f"/api/v1/saved_query/{saved_query_id}")
                r = info.get("result", {})
                label = r.get("label", "?")
                db_name = r.get("database", {}).get("database_name", "?")
            except Exception:
                label = f"ID={saved_query_id}"
                db_name = "?"
            return json.dumps(
                {
                    "error": (
                        f"REJECTED: deletion of saved query '{label}' "
                        f"(ID={saved_query_id}, DB={db_name}). "
                        f"Pass confirm_delete=True to confirm."
                    )
                },
                ensure_ascii=False,
            )

        result = await client.delete(f"/api/v1/saved_query/{saved_query_id}")
        return json.dumps(result, ensure_ascii=False)

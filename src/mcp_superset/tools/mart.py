"""Tools for marts: materialized, pre-computed copies of heavy datasets.

A mart is a PostgreSQL materialized view in the ``mart`` schema, managed by the
functions installed there by the mart install script: ``mart.apply`` builds a new
version next to the current one and swaps it in, ``mart._registry`` keeps the SQL,
schedule and state, and a cron job calls ``mart.run_due`` every few minutes.

These tools only call those functions, through SQL Lab on the write-enabled
connection named by ``SUPERSET_MCP_MART_DATABASE_ID``, as the calling user; they
never talk to the database directly. Writes run as a ``DO`` block because Superset
commits only statements it recognises as changes - a plain ``SELECT mart.apply(...)``
would run and then be rolled back.
"""

import json
import os
import re
import secrets
import uuid
from typing import Any

from mcp_superset import context
from mcp_superset.tools.queries import run_sqllab_query
from mcp_superset.tools.types import StrList

MART_WAIT_SECONDS = 280.0

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_INTERVAL = re.compile(r"^\d+\s*(minute|minutes|hour|hours|day|days)$")
_JINJA = re.compile(r"\{\{|\{%|\{#")

GUIDE = """\
MARTS - pre-computed copies of heavy datasets (PostgreSQL materialized views in schema "mart").

When: a virtual dataset whose SQL takes seconds or minutes on every chart query. The mart
computes it once; dashboards then read the ready rows in milliseconds.

How it works
- mart_create_from_dataset / mart_apply build the new version next to the current one and
  swap it in atomically. A failed build changes nothing: the current version keeps working.
- mart._registry holds each mart's SQL, version, schedule and state; mart._registry_history
  holds every applied version (for rollback: mart_apply with an old version's SQL).
- Refresh is automatic: after a source load (refresh_on - the loader calls
  mart.on_source_loaded), at fixed Moscow times (refresh_at, always rebuilds), or every N
  (refresh_every, optionally only when the source tables changed). A cron job runs the queue
  every 5 minutes. mart_refresh asks for a rebuild now.
- Everything is logged in mart._refresh_log (mart_log), with a diagnosis when a source column
  was renamed, dropped or changed type (mart_get shows it).

Rules
- The mart SQL must be plain SQL: no Jinja ({{ }}, {% %}). Dashboard filters that a dataset
  applies through Jinja cannot be baked in - keep them in a thin virtual dataset over the mart
  (e.g. store one row per filter variant and pick it in the dataset's WHERE).
- Changing a mart: pass expected_version (from mart_get); if someone applied a newer version,
  the change is refused. Removing columns needs allow_column_removal=true - check first which
  charts use them.
- Never create, change or drop marts through superset_sqllab_execute; use these tools.
- Charts and datasets keep using the normal (read-only) connection: a mart is just a table
  "mart.<name>" there. Point a dataset at it (e.g. "select * from mart.<name>") only on a copy
  unless the owner of the original asked for the switch.
- Changing a column type or dropping a column in a SOURCE table that a mart reads is blocked by
  PostgreSQL. The table owner runs SELECT mart.before_alter('schema.table'), makes the change,
  then SELECT mart.after_alter('schema.table').
"""


def mart_database_id() -> int | None:
    """The write-enabled connection the mart tools use, or None if marts are not set up."""
    raw = os.getenv("SUPERSET_MCP_MART_DATABASE_ID", "").strip()
    return int(raw) if raw.isdigit() else None


def _lit(value: Any) -> str:
    """A SQL literal: NULL, true/false, a number, or a quoted string."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _dollar_tag(*texts: str) -> str:
    """A $tag$ that occurs in none of the texts it will enclose."""
    while True:
        tag = f"$m{secrets.token_hex(4)}$"
        if not any(tag in text for text in texts):
            return tag


def _text_array(values: list[str], element_type: str = "text") -> str:
    """ARRAY[...]::type[] literal (an empty list gives an empty array)."""
    if not values:
        return f"'{{}}'::{element_type}[]"
    return "ARRAY[" + ", ".join(f"{_lit(v)}::{element_type}" for v in values) + "]"


def _check_text(**fields: str | None) -> str | None:
    """Reject Jinja markers: SQL Lab renders the whole script as a Jinja template."""
    for field, value in fields.items():
        if value and _JINJA.search(value):
            return (
                f"{field} contains Jinja ({{{{ }}}}, {{% %}} or {{# #}}). SQL Lab renders Jinja "
                "before running anything, and a mart holds plain SQL only: filters applied through "
                "Jinja belong in a virtual dataset over the mart."
            )
    return None


def _check_name(name: str) -> str | None:
    if not _NAME.match(name or "") or "__" in name:
        return (
            f"Invalid mart name {name!r}: lowercase latin letters, digits and _, 3-41 characters, "
            "starting with a letter, no '__'."
        )
    return None


def _actor() -> str:
    """Who is making the change, as recorded in the registry and the log."""
    if context.per_user_mode():
        try:
            return context.caller_email()
        except Exception:  # noqa: BLE001 - fall back to a label rather than fail the call
            pass
    return "mcp-service"


def _call_script(call: str) -> str:
    """Run a mart.* function in a DO block and return its text result in the same script.

    The DO block makes Superset commit; the function's return value is passed out through
    a temporary table that is dropped at commit.
    """
    tag = _dollar_tag(call)
    return (
        f"DO {tag}\n"
        "BEGIN\n"
        "  CREATE TEMP TABLE IF NOT EXISTS _mart_call (result text) ON COMMIT DROP;\n"
        f"  INSERT INTO _mart_call SELECT {call};\n"
        f"END\n{tag};\n"
        "SELECT result FROM _mart_call"
    )


async def _run(client, sql: str, *, write: bool, tab: str, wait_seconds: float = MART_WAIT_SECONDS) -> dict:
    database_id = mart_database_id()
    payload = {"database_id": database_id, "sql": sql, "queryLimit": 1000, "tab": tab}
    return await run_sqllab_query(client, payload, run_async=write, wait_seconds=wait_seconds)


def _rows(result: dict) -> list[dict]:
    return result.get("data") or []


def _outcome(result: dict) -> dict:
    """Turn the SQL Lab answer to a _call_script into {result} or {error / status}."""
    if result.get("status") == "success" and result.get("data") is not None:
        rows = _rows(result)
        return {"result": rows[0].get("result") if rows else None}
    if result.get("error") or result.get("status") in ("failed", "stopped", "timed_out"):
        return {"error": result.get("error") or result.get("errors") or result.get("status"), "status": "failed"}
    return result  # still running: status + query_id + hint


def _not_configured() -> str:
    return json.dumps(
        {"error": "Marts are not set up on this server (SUPERSET_MCP_MART_DATABASE_ID is not set)."},
        ensure_ascii=False,
    )


def register_mart_tools(mcp):
    from mcp_superset.context import current_client as client

    async def _log_for(request_id: str) -> dict | None:
        sql = (
            "SELECT id, name, version, action, status, row_count, duration_ms, error_code, error, "
            "diagnosis, columns_diff, warnings FROM mart._refresh_log "
            f"WHERE request_id = {_lit(request_id)} ORDER BY id DESC LIMIT 1"
        )
        rows = _rows(await _run(client, sql, write=False, tab="mart: log"))
        return rows[0] if rows else None

    async def _write(call: str, tab: str, wait_seconds: float, request_id: str | None = None) -> str:
        outcome = _outcome(await _run(client, _call_script(call), write=True, tab=tab, wait_seconds=wait_seconds))
        if request_id and "result" in outcome:
            outcome["log"] = await _log_for(request_id)
        return json.dumps(outcome, ensure_ascii=False, default=str)

    async def _apply(
        name: str,
        sql: str,
        comment: str,
        expected_version: int | None,
        description: str | None,
        indexes: list[str] | None,
        source_dataset_id: int | None,
        allow_column_removal: bool,
        wait_seconds: float,
    ) -> str:
        for error in (_check_name(name), _check_text(sql=sql, comment=comment, description=description)):
            if error:
                return json.dumps({"error": error}, ensure_ascii=False)
        if not (comment or "").strip():
            return json.dumps({"error": "comment is required: say why the mart is created or changed."})
        if not (sql or "").strip():
            return json.dumps({"error": "sql is empty."})
        for spec in indexes or []:
            if not all(_COLUMN.match(col.strip()) for col in spec.split(",")):
                return json.dumps({"error": f"Invalid index {spec!r}: comma-separated lowercase column names."})

        request_id = str(uuid.uuid4())
        tag = _dollar_tag(sql)
        call = (
            "mart.apply("
            f"p_name => {_lit(name)}, "
            f"p_sql => {tag}{sql}{tag}, "
            f"p_comment => {_lit(comment.strip())}, "
            f"p_by => {_lit(_actor())}, "
            f"p_expected_version => {_lit(expected_version)}, "
            f"p_description => {_lit(description)}, "
            f"p_indexes => {'NULL' if indexes is None else _text_array([s.strip() for s in indexes])}, "
            f"p_source_dataset_id => {_lit(source_dataset_id)}, "
            f"p_allow_column_removal => {_lit(bool(allow_column_removal))}, "
            f"p_request_id => {_lit(request_id)})"
        )
        return await _write(call, f"mart: apply {name}", wait_seconds, request_id)

    @mcp.tool
    async def mart_guide() -> str:
        """How marts (materialized copies of heavy datasets) work here, and the rules.

        Call this before creating, changing or refreshing a mart. Also returns the current
        settings and a one-line state of every mart.
        """
        if mart_database_id() is None:
            return _not_configured()
        settings = _rows(
            await _run(client, "SELECT key, value FROM mart._settings ORDER BY key", write=False, tab="mart: guide")
        )
        marts = _rows(
            await _run(
                client,
                "SELECT name, version, status, data_as_of, row_count, rebuild_pending FROM mart._status ORDER BY name",
                write=False,
                tab="mart: guide",
            )
        )
        return json.dumps(
            {"guide": GUIDE, "database_id": mart_database_id(), "settings": settings, "marts": marts},
            ensure_ascii=False,
            default=str,
        )

    @mcp.tool
    async def mart_list() -> str:
        """List marts with their state: version, status, data time (Moscow), rows, schedule, last error."""
        if mart_database_id() is None:
            return _not_configured()
        result = await _run(client, "SELECT * FROM mart._status ORDER BY name", write=False, tab="mart: list")
        return json.dumps(_rows(result) if "data" in result else result, ensure_ascii=False, default=str)

    @mcp.tool
    async def mart_get(name: str, include_sql: bool = True) -> str:
        """Everything about one mart: registry row (SQL, schedule, state), version history,
        the last 10 log entries, the source tables it reads and, when its last build failed,
        a diagnosis of what changed in the sources.

        Args:
            name: Mart name (without the "mart." prefix).
            include_sql: Include the current SQL (can be tens of KB).
        """
        if mart_database_id() is None:
            return _not_configured()
        if error := _check_name(name):
            return json.dumps({"error": error}, ensure_ascii=False)
        n = _lit(name)
        registry_row = "to_jsonb(r)" if include_sql else "to_jsonb(r) - 'sql'"
        registry = f"(SELECT {registry_row} FROM mart._registry r WHERE r.name = {n})"
        sql = (
            "SELECT json_build_object("
            f"'registry', {registry}, "
            "'history', (SELECT json_agg(json_build_object('version', h.version, 'comment', h.comment, "
            "'changed_by', h.changed_by, 'changed_at', h.changed_at) ORDER BY h.version DESC) "
            f"FROM mart._registry_history h WHERE h.name = {n}), "
            "'log', (SELECT json_agg(l ORDER BY l.id DESC) FROM (SELECT id, version, action, reason, status, "
            "requested_by, started_at, duration_ms, row_count, error_code, error, diagnosis, columns_diff, warnings "
            f"FROM mart._refresh_log WHERE name = {n} ORDER BY id DESC LIMIT 10) l), "
            "'sources', (SELECT json_agg(json_build_object('table', s.table_name, 'kind', s.relkind, "
            f"'direct', s.direct) ORDER BY s.table_name) FROM mart._sources s WHERE s.name = {n}), "
            f"'diagnosis', (SELECT mart.diagnose(r.name) FROM mart._registry r WHERE r.name = {n} AND r.status <> 'ok')"
            ") AS mart"
        )
        rows = _rows(await _run(client, sql, write=False, tab="mart: get"))
        info = rows[0]["mart"] if rows else None
        if isinstance(info, str):
            info = json.loads(info)
        if not info or not info.get("registry"):
            return json.dumps({"error": f"No mart named {name!r}. See mart_list."}, ensure_ascii=False)
        return json.dumps(info, ensure_ascii=False, default=str)

    @mcp.tool
    async def mart_log(name: str | None = None, limit: int = 20) -> str:
        """The mart log: builds, refreshes, schedule changes, removals - newest first, times in Moscow.

        Args:
            name: Only this mart (default: all).
            limit: How many entries (max 200).
        """
        if mart_database_id() is None:
            return _not_configured()
        where = ""
        if name:
            if error := _check_name(name):
                return json.dumps({"error": error}, ensure_ascii=False)
            where = f"WHERE name = {_lit(name)} "
        sql = (
            "SELECT id, name, version, action, reason, status, requested_by, "
            "(started_at AT TIME ZONE mart._setting('timezone'))::timestamp(0) AS started, "
            "duration_ms, row_count, error_code, left(error, 1000) AS error, diagnosis, columns_diff, warnings "
            f"FROM mart._refresh_log {where}ORDER BY id DESC LIMIT {max(1, min(int(limit), 200))}"
        )
        return json.dumps(_rows(await _run(client, sql, write=False, tab="mart: log")), ensure_ascii=False, default=str)

    @mcp.tool
    async def mart_apply(
        name: str,
        sql: str,
        comment: str,
        expected_version: int | None = None,
        description: str | None = None,
        indexes: StrList | None = None,
        source_dataset_id: int | None = None,
        allow_column_removal: bool = False,
        wait_seconds: float = MART_WAIT_SECONDS,
    ) -> str:
        """Create a mart, or apply a new version of its SQL.

        The new version is built next to the current one and swapped in only if the build
        succeeds; otherwise nothing changes. Takes as long as the SQL itself (seconds to a
        few minutes). Read mart_guide first.

        Args:
            name: Mart name, e.g. "rnp_sku_day" (becomes the table mart.<name>).
            sql: One SELECT (plain SQL, no Jinja). Tables must be schema-qualified or in public.
            comment: Why the mart is created / changed (kept in the version history).
            expected_version: For an existing mart: the version you based the change on
                (mart_get). Omit for a new mart. A newer version applied meanwhile -> refused.
            description: What the mart contains (shown in mart_list).
            indexes: Indexes to build, each a comma-separated column list, e.g.
                ["date_s", "brand_title,mp,date_s"]. Omit to keep the current ones.
            source_dataset_id: Superset dataset the SQL came from (for reference).
            allow_column_removal: Allow a version that drops columns of the current one.
                Check first which charts use them.
            wait_seconds: How long to wait for the build (max 300); a longer build keeps
                running and its outcome appears in mart_log.
        """
        if mart_database_id() is None:
            return _not_configured()
        return await _apply(
            name,
            sql,
            comment,
            expected_version,
            description,
            indexes,
            source_dataset_id,
            allow_column_removal,
            wait_seconds,
        )

    @mcp.tool
    async def mart_create_from_dataset(
        dataset_id: int,
        name: str,
        comment: str,
        description: str | None = None,
        indexes: StrList | None = None,
        wait_seconds: float = MART_WAIT_SECONDS,
    ) -> str:
        """Create a mart from a virtual dataset's SQL (the dataset itself is not changed).

        Refuses datasets whose SQL uses Jinja (filter_values, url_param, ...): a mart holds
        plain SQL, so such filters must first be reworked (see mart_guide) and the result
        applied with mart_apply.

        Args:
            dataset_id: Virtual dataset whose SQL to materialize.
            name: Mart name, e.g. "rnp_sku_day".
            comment: Why the mart is created.
            description: What the mart contains (default: the dataset's name).
            indexes: Indexes to build, each a comma-separated column list.
            wait_seconds: How long to wait for the build (max 300).
        """
        if mart_database_id() is None:
            return _not_configured()
        dataset = (await client.get(f"/api/v1/dataset/{dataset_id}")).get("result", {})
        sql = dataset.get("sql")
        if not sql:
            return json.dumps(
                {"error": f"Dataset {dataset_id} is not a virtual dataset (no SQL): there is nothing to materialize."},
                ensure_ascii=False,
            )
        if _JINJA.search(sql):
            snippets = sorted({m.group(0) for m in re.finditer(r"\{[{%#].{0,60}?[}%#]\}", sql, re.S)})[:5]
            return json.dumps(
                {
                    "error": (
                        f"Dataset {dataset_id} uses Jinja, which a mart cannot hold. Rework these parts into "
                        "plain SQL (e.g. one row per filter variant, picked by a thin dataset over the mart), "
                        "then create the mart with mart_apply."
                    ),
                    "jinja": snippets,
                },
                ensure_ascii=False,
            )
        return await _apply(
            name,
            sql,
            comment,
            None,
            description or f"From dataset {dataset_id} ({dataset.get('table_name')})",
            indexes,
            dataset_id,
            False,
            wait_seconds,
        )

    @mcp.tool
    async def mart_refresh(name: str, now: bool = False, wait_seconds: float = MART_WAIT_SECONDS) -> str:
        """Rebuild a mart from its current SQL.

        Args:
            name: Mart name.
            now: False (default) - queue it; the cron job rebuilds it within 5 minutes, in the
                background. True - rebuild right now and wait for it (takes as long as the SQL).
            wait_seconds: With now=True, how long to wait (max 300).
        """
        if mart_database_id() is None:
            return _not_configured()
        if error := _check_name(name):
            return json.dumps({"error": error}, ensure_ascii=False)
        if not now:
            return await _write(f"mart.request_refresh({_lit(name)}, {_lit(_actor())})", f"mart: refresh {name}", 60)
        request_id = str(uuid.uuid4())
        call = f"mart.refresh({_lit(name)}, {_lit(_actor())}, {_lit(request_id)})"
        return await _write(call, f"mart: refresh {name}", wait_seconds, request_id)

    @mcp.tool
    async def mart_set_schedule(
        name: str,
        refresh_on: StrList | None = None,
        refresh_at: StrList | None = None,
        refresh_every: str | None = None,
        every_from: str | None = None,
        every_to: str | None = None,
        only_if_changed: bool | None = None,
        enabled: bool | None = None,
        clear_every: bool = False,
    ) -> str:
        """Set when a mart is rebuilt. Omitted arguments stay as they are; [] clears a list.

        Args:
            name: Mart name.
            refresh_on: Rebuild after these source loads, e.g. ["t_unpivot_metrics"] (the
                loader calls mart.on_source_loaded('<source>') at its end).
            refresh_at: Rebuild at these Moscow times, e.g. ["00:05", "12:45"]. Always rebuilds.
            refresh_every: Rebuild periodically, e.g. "1 hour", "30 minutes" (min 15 minutes).
            every_from: With refresh_every: only from this time, e.g. "08:00".
            every_to: With refresh_every: only until this time, e.g. "20:00".
            only_if_changed: With refresh_every: skip when the source tables had no writes.
            enabled: false pauses all automatic rebuilds of this mart.
            clear_every: Remove refresh_every together with its window.
        """
        if mart_database_id() is None:
            return _not_configured()
        if error := _check_name(name):
            return json.dumps({"error": error}, ensure_ascii=False)
        for value in (refresh_at or []) + [v for v in (every_from, every_to) if v]:
            if not _TIME.match(value):
                return json.dumps({"error": f"Invalid time {value!r}: use HH:MM."})
        if refresh_every and not _INTERVAL.match(refresh_every.strip()):
            return json.dumps({"error": f"Invalid period {refresh_every!r}: e.g. '30 minutes', '1 hour'."})
        for source in refresh_on or []:
            if not re.match(r"^([a-z_][a-z0-9_]*\.)?[a-z_][a-z0-9_]*$", source.strip().lower()):
                return json.dumps({"error": f"Invalid source name {source!r}."})
        call = (
            "mart.set_schedule("
            f"p_name => {_lit(name)}, "
            f"p_by => {_lit(_actor())}, "
            f"p_refresh_on => {'NULL' if refresh_on is None else _text_array([s.strip() for s in refresh_on])}, "
            f"p_refresh_at => {'NULL' if refresh_at is None else _text_array(refresh_at, 'time')}, "
            f"p_refresh_every => {'NULL' if not refresh_every else _lit(refresh_every.strip()) + '::interval'}, "
            f"p_every_from => {'NULL' if not every_from else _lit(every_from) + '::time'}, "
            f"p_every_to => {'NULL' if not every_to else _lit(every_to) + '::time'}, "
            f"p_only_if_changed => {_lit(only_if_changed)}, "
            f"p_enabled => {_lit(enabled)}, "
            f"p_clear_every => {_lit(bool(clear_every))})"
        )
        return await _write(call, f"mart: schedule {name}", 60)

    @mcp.tool
    async def mart_remove(name: str, comment: str) -> str:
        """Remove a mart (its table and registry entry; the version history is kept).

        Refused while a Superset dataset is switched to the mart.

        Args:
            name: Mart name.
            comment: Why it is removed.
        """
        if mart_database_id() is None:
            return _not_configured()
        for error in (_check_name(name), _check_text(comment=comment)):
            if error:
                return json.dumps({"error": error}, ensure_ascii=False)
        if not (comment or "").strip():
            return json.dumps({"error": "comment is required: say why the mart is removed."})
        call = f"mart.remove({_lit(name)}, {_lit(comment.strip())}, {_lit(_actor())})"
        return await _write(call, f"mart: remove {name}", 120)

"""mart_* tools: what they send to SQL Lab, and what they refuse to send."""

import json

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP

import mcp_superset.server as server_module  # noqa: F401 - imported for env/config parity
from mcp_superset import context
from mcp_superset.auth import CookieAuthManager
from mcp_superset.client import SupersetClient
from mcp_superset.tools import queries, register_all_tools
from mcp_superset.tools.mart import _call_script, _dollar_tag, _lit, _text_array

BASE = "https://superset.example.com"


@pytest.fixture
def mcp_server(monkeypatch):
    monkeypatch.setenv("SUPERSET_MCP_MART_DATABASE_ID", "3")
    monkeypatch.setattr(queries, "_POLL_INTERVAL_SECONDS", 0)
    client = SupersetClient(auth_manager=CookieAuthManager(base_url=BASE, cookie_value="c"), base_url=BASE)
    context.configure(service_client=client)
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    yield mcp


def _text(result):
    content = getattr(result, "content", result)
    if isinstance(content, list) and content:
        return content[0].text
    return str(content)


def _mock_sqllab(result_rows, log_rows=None):
    """Async SQL Lab: the DO script finishes at once with result_rows; reads return log_rows."""
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    calls = []

    def execute(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["runAsync"]:
            return httpx.Response(202, json={"query": {"id": "c1", "queryId": 70, "state": "pending"}})
        return httpx.Response(200, json={"status": "success", "data": log_rows or [], "query": {"queryId": 71}})

    respx.post(f"{BASE}/api/v1/sqllab/execute/").mock(side_effect=execute)
    respx.get(f"{BASE}/api/v1/query/70").mock(
        return_value=httpx.Response(200, json={"result": {"status": "success", "results_key": "k"}})
    )
    respx.get(f"{BASE}/api/v1/sqllab/results/").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": result_rows})
    )
    return calls


def test_literals_and_arrays():
    assert _lit(None) == "NULL"
    assert _lit(True) == "true"
    assert _lit(7) == "7"
    assert _lit("it's") == "'it''s'"
    assert _text_array([]) == "'{}'::text[]"
    assert _text_array(["00:05"], "time") == "ARRAY['00:05'::time]"


def test_dollar_tag_avoids_the_enclosed_text(monkeypatch):
    tokens = iter(["aaaa0000", "bbbb1111"])
    monkeypatch.setattr("mcp_superset.tools.mart.secrets.token_hex", lambda n: next(tokens))
    assert _dollar_tag("select '$maaaa0000$'") == "$mbbbb1111$"


def test_call_script_commits_through_do_and_returns_the_result():
    script = _call_script("mart.request_refresh('x', 'me')")
    assert script.startswith("DO $m")
    assert "INSERT INTO _mart_call SELECT mart.request_refresh('x', 'me');" in script
    assert script.rstrip().endswith("SELECT result FROM _mart_call")


@respx.mock
async def test_mart_apply_sends_a_do_block_on_the_mart_connection(mcp_server):
    calls = _mock_sqllab([{"result": "mart.x_mart v1 применена"}], log_rows=[{"id": 5, "status": "ok"}])
    sql = "select 'it''s' as a, 1 as b; -- trailing"

    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "mart_apply",
            {"name": "x_mart", "sql": sql, "comment": "тест 'кавычек'", "indexes": ["a", "a,b"], "expected_version": 2},
        )

    write = calls[0]
    assert write["database_id"] == 3
    assert write["runAsync"] is True
    body = write["sql"]
    assert body.startswith("DO $m")
    assert "p_name => 'x_mart'" in body
    assert "p_sql => $m" in body and sql in body
    assert "p_comment => 'тест ''кавычек'''" in body
    assert "p_expected_version => 2" in body
    assert "p_indexes => ARRAY['a'::text, 'a,b'::text]" in body
    assert "p_allow_column_removal => false" in body
    # the request id is what the follow-up read looks the log entry up by
    request_id = body.split("p_request_id => '")[1].split("'")[0]
    assert request_id in calls[1]["sql"] and calls[1]["runAsync"] is False

    payload = json.loads(_text(result))
    assert payload["result"] == "mart.x_mart v1 применена"
    assert payload["log"] == {"id": 5, "status": "ok"}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"name": "X", "sql": "select 1", "comment": "c"}, "Invalid mart name"),
        ({"name": "a__b", "sql": "select 1", "comment": "c"}, "Invalid mart name"),
        ({"name": "ok_mart", "sql": "select {{ x }}", "comment": "c"}, "Jinja"),
        ({"name": "ok_mart", "sql": "select 1", "comment": "   "}, "comment is required"),
        ({"name": "ok_mart", "sql": "select 1", "comment": "c", "indexes": ["a; drop"]}, "Invalid index"),
    ],
)
@respx.mock
async def test_mart_apply_refuses_bad_input_without_calling_superset(mcp_server, args, message):
    execute = respx.post(f"{BASE}/api/v1/sqllab/execute/")
    async with Client(mcp_server) as c:
        result = await c.call_tool("mart_apply", args)
    assert message in json.loads(_text(result))["error"]
    assert not execute.called


@respx.mock
async def test_mart_apply_reports_a_refused_change(mcp_server):
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    respx.post(f"{BASE}/api/v1/sqllab/execute/").mock(
        return_value=httpx.Response(202, json={"query": {"id": "c1", "queryId": 72, "state": "pending"}})
    )
    respx.get(f"{BASE}/api/v1/query/72").mock(
        return_value=httpx.Response(
            200,
            json={"result": {"status": "failed", "error_message": "витрину mart.x_mart уже изменили: сейчас версия 3"}},
        )
    )
    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "mart_apply", {"name": "x_mart", "sql": "select 1", "comment": "c", "expected_version": 2}
        )
    payload = json.loads(_text(result))
    assert payload["status"] == "failed"
    assert "уже изменили" in payload["error"]


@respx.mock
async def test_create_from_dataset_refuses_jinja(mcp_server):
    respx.get(f"{BASE}/api/v1/dataset/197").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {"id": 197, "table_name": "rnp", "sql": "select '{{ (filter_values('x') or ['ALL'])[0] }}'"}
            },
        )
    )
    execute = respx.post(f"{BASE}/api/v1/sqllab/execute/")
    async with Client(mcp_server) as c:
        result = await c.call_tool("mart_create_from_dataset", {"dataset_id": 197, "name": "rnp_copy", "comment": "c"})
    payload = json.loads(_text(result))
    assert "uses Jinja" in payload["error"]
    assert payload["jinja"]
    assert not execute.called


@respx.mock
async def test_create_from_dataset_applies_the_dataset_sql(mcp_server):
    respx.get(f"{BASE}/api/v1/dataset/42").mock(
        return_value=httpx.Response(
            200, json={"result": {"id": 42, "table_name": "sales_core", "sql": "select 1 as a"}}
        )
    )
    calls = _mock_sqllab([{"result": "ok"}], log_rows=[])
    async with Client(mcp_server) as c:
        await c.call_tool("mart_create_from_dataset", {"dataset_id": 42, "name": "sales_mart", "comment": "c"})
    body = calls[0]["sql"]
    assert "select 1 as a" in body
    assert "p_source_dataset_id => 42" in body
    assert "p_description => 'From dataset 42 (sales_core)'" in body


@respx.mock
async def test_mart_refresh_queues_by_default_and_builds_with_now(mcp_server):
    calls = _mock_sqllab([{"result": "ok"}], log_rows=[])
    async with Client(mcp_server) as c:
        await c.call_tool("mart_refresh", {"name": "rnp_sku_day"})
        await c.call_tool("mart_refresh", {"name": "rnp_sku_day", "now": True})
    assert "mart.request_refresh('rnp_sku_day'" in calls[0]["sql"]
    assert "mart.refresh('rnp_sku_day'" in calls[1]["sql"]


@respx.mock
async def test_mart_set_schedule_builds_typed_arguments(mcp_server):
    calls = _mock_sqllab([{"result": "ok"}])
    async with Client(mcp_server) as c:
        await c.call_tool(
            "mart_set_schedule",
            {
                "name": "rnp_sku_day",
                "refresh_on": ["t_unpivot_metrics"],
                "refresh_at": ["00:05", "12:45"],
                "refresh_every": "1 hour",
                "every_from": "08:00",
                "every_to": "20:00",
                "only_if_changed": True,
            },
        )
    body = calls[0]["sql"]
    assert "p_refresh_on => ARRAY['t_unpivot_metrics'::text]" in body
    assert "p_refresh_at => ARRAY['00:05'::time, '12:45'::time]" in body
    assert "p_refresh_every => '1 hour'::interval" in body
    assert "p_every_from => '08:00'::time" in body
    assert "p_only_if_changed => true" in body
    assert "p_enabled => NULL" in body


@pytest.mark.parametrize(
    "args",
    [
        {"name": "m_one", "refresh_at": ["25:00"]},
        {"name": "m_one", "refresh_every": "1 hour; drop"},
        {"name": "m_one", "refresh_on": ["x'; drop"]},
    ],
)
@respx.mock
async def test_mart_set_schedule_refuses_bad_values(mcp_server, args):
    execute = respx.post(f"{BASE}/api/v1/sqllab/execute/")
    async with Client(mcp_server) as c:
        result = await c.call_tool("mart_set_schedule", args)
    assert "error" in json.loads(_text(result))
    assert not execute.called


async def test_mart_tools_are_not_registered_without_a_mart_connection(monkeypatch):
    monkeypatch.delenv("SUPERSET_MCP_MART_DATABASE_ID", raising=False)
    client = SupersetClient(auth_manager=CookieAuthManager(base_url=BASE, cookie_value="c"), base_url=BASE)
    context.configure(service_client=client)
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    async with Client(mcp) as c:
        names = {tool.name for tool in await c.list_tools()}
    assert "superset_sqllab_execute" in names
    assert not {n for n in names if n.startswith("mart_")}

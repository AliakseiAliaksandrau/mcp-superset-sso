"""Tools for building a sandbox copy of a dashboard without touching the original.

Covers: the SQL guard's single-pass literal handling, asynchronous SQL Lab execution,
dashboard_copy(duplicate_charts=True) and chart_update(datasource_id=...).
"""

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
from mcp_superset.tools.dashboards import _remap_chart_ids
from mcp_superset.tools.queries import _detect_dangerous_sql

BASE = "https://superset.example.com"


@pytest.fixture
def mcp_server():
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


def _csrf():
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))


# --- SQL guard -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # a comment marker inside a string must not hide what follows it
        ("SELECT '--'; DROP TABLE t", "DROP"),
        ("SELECT 'a''b -- c'; DELETE FROM t", "DELETE"),
        ("SELECT E'it\\'s'; DROP TABLE t", "DROP"),
        ("SELECT '/*'; UPDATE t SET a = 1", "UPDATE"),
        # a quote inside a comment must not open a string that hides the rest
        ("SELECT 1 -- it's fine\n; DROP TABLE t", "DROP"),
        ("/* */ DROP TABLE x", "DROP"),
        ("SELECT $1, 2; DROP TABLE t", "DROP"),
        ("DO $$ BEGIN EXECUTE 'drop table t'; END $$", "DO"),
        # keywords inside literals, identifiers and comments are not statements
        ("SELECT $$DROP TABLE t$$ AS txt", None),
        ("SELECT $tag$ delete $tag$ AS txt", None),
        ('SELECT "update" FROM t', None),
        ("SELECT 1 -- DROP TABLE t", None),
        ("/* outer /* DROP */ still comment */ SELECT 1", None),
        ("SELECT update_date, deleted_at FROM t", None),
        ("SELECT 'x -- y' AS a, count(*) FROM t -- where status = 'EXECUTE'", None),
    ],
)
def test_dangerous_sql_detection(sql, expected):
    assert _detect_dangerous_sql(sql) == expected


# --- asynchronous SQL Lab ---------------------------------------------------------------


@respx.mock
async def test_sqllab_execute_runs_async_and_returns_rows(mcp_server, monkeypatch):
    monkeypatch.setattr(queries, "_POLL_INTERVAL_SECONDS", 0)
    _csrf()
    respx.get(f"{BASE}/api/v1/database/3").mock(
        return_value=httpx.Response(200, json={"result": {"id": 3, "allow_run_async": True}})
    )
    execute = respx.post(f"{BASE}/api/v1/sqllab/execute/").mock(
        return_value=httpx.Response(202, json={"query": {"id": "c1", "queryId": 77, "state": "pending"}})
    )
    respx.get(f"{BASE}/api/v1/query/77").mock(
        side_effect=[
            httpx.Response(200, json={"result": {"id": 77, "status": "running"}}),
            httpx.Response(200, json={"result": {"id": 77, "status": "success", "results_key": "k1"}}),
        ]
    )
    results = respx.get(f"{BASE}/api/v1/sqllab/results/").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": [{"x": 1}]})
    )

    async with Client(mcp_server) as c:
        result = await c.call_tool("superset_sqllab_execute", {"database_id": 3, "sql": "SELECT 1 AS x"})

    assert json.loads(execute.calls.last.request.content)["runAsync"] is True
    assert results.calls.last.request.url.params["q"] == "(key:'k1')"
    assert json.loads(_text(result))["data"] == [{"x": 1}]


@respx.mock
async def test_sqllab_execute_stays_sync_where_async_is_off(mcp_server):
    _csrf()
    respx.get(f"{BASE}/api/v1/database/2").mock(
        return_value=httpx.Response(200, json={"result": {"id": 2, "allow_run_async": False}})
    )
    execute = respx.post(f"{BASE}/api/v1/sqllab/execute/").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": [{"x": 1}], "query": {"queryId": 5}})
    )

    async with Client(mcp_server) as c:
        result = await c.call_tool("superset_sqllab_execute", {"database_id": 2, "sql": "SELECT 1 AS x"})

    assert json.loads(execute.calls.last.request.content)["runAsync"] is False
    assert json.loads(_text(result))["data"] == [{"x": 1}]


@respx.mock
async def test_sqllab_execute_reports_a_query_still_running(mcp_server):
    _csrf()
    respx.post(f"{BASE}/api/v1/sqllab/execute/").mock(
        return_value=httpx.Response(202, json={"query": {"id": "c9", "queryId": 91, "state": "pending"}})
    )
    respx.get(f"{BASE}/api/v1/query/91").mock(
        return_value=httpx.Response(200, json={"result": {"id": 91, "status": "running"}})
    )

    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "superset_sqllab_execute",
            {"database_id": 3, "sql": "SELECT 1", "run_async": True, "wait_seconds": 0},
        )

    payload = json.loads(_text(result))
    assert payload["status"] == "running"
    assert payload["query_id"] == 91
    assert payload["client_id"] == "c9"


@respx.mock
async def test_sqllab_execute_reports_an_async_failure(mcp_server, monkeypatch):
    monkeypatch.setattr(queries, "_POLL_INTERVAL_SECONDS", 0)
    _csrf()
    respx.post(f"{BASE}/api/v1/sqllab/execute/").mock(
        return_value=httpx.Response(202, json={"query": {"id": "c2", "queryId": 92, "state": "pending"}})
    )
    respx.get(f"{BASE}/api/v1/query/92").mock(
        return_value=httpx.Response(200, json={"result": {"status": "failed", "error_message": "boom"}})
    )

    async with Client(mcp_server) as c:
        result = await c.call_tool("superset_sqllab_execute", {"database_id": 3, "sql": "SELECT 1", "run_async": True})

    assert json.loads(_text(result)) == {"status": "failed", "query_id": 92, "error": "boom"}


# --- dashboard copy with charts -----------------------------------------------------------

_ORIGINAL_POSITIONS = {
    "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
    "CHART-a": {"type": "CHART", "id": "CHART-a", "meta": {"chartId": 889, "width": 12}},
    "CHART-b": {"type": "CHART", "id": "CHART-b", "meta": {"chartId": 934, "width": 12}},
}
_ORIGINAL_METADATA = {
    "native_filter_configuration": [
        {"id": "NATIVE_FILTER-kind", "chartsInScope": [889], "scope": {"excluded": [934], "rootPath": ["ROOT_ID"]}},
    ],
    "chart_configuration": {
        "889": {"id": 889, "crossFilters": {"chartsInScope": [934], "scope": {"excluded": [889], "rootPath": []}}},
    },
    "global_chart_configuration": {"chartsInScope": [889, 934], "scope": {"excluded": [], "rootPath": ["ROOT_ID"]}},
    "timed_refresh_immune_slices": [934],
    "expanded_slices": {"889": True},
    "color_scheme": "supersetColors",
}


def test_remap_chart_ids_rewrites_filters_and_cross_filters():
    md = json.loads(json.dumps(_ORIGINAL_METADATA))
    changed = _remap_chart_ids(md, {889: 2001, 934: 2002})

    assert md["native_filter_configuration"][0]["chartsInScope"] == [2001]
    assert md["native_filter_configuration"][0]["scope"]["excluded"] == [2002]
    assert list(md["chart_configuration"]) == ["2001"]
    assert md["chart_configuration"]["2001"]["id"] == 2001
    assert md["chart_configuration"]["2001"]["crossFilters"]["chartsInScope"] == [2002]
    assert md["chart_configuration"]["2001"]["crossFilters"]["scope"]["excluded"] == [2001]
    assert md["global_chart_configuration"]["chartsInScope"] == [2001, 2002]
    assert md["timed_refresh_immune_slices"] == [2002]
    assert md["expanded_slices"] == {"2001": True}
    assert md["color_scheme"] == "supersetColors"
    assert changed == 9


@respx.mock
async def test_dashboard_copy_with_charts_leaves_the_original_alone(mcp_server):
    _csrf()
    respx.get(f"{BASE}/api/v1/dashboard/155").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "id": 155,
                    "position_json": json.dumps(_ORIGINAL_POSITIONS),
                    "json_metadata": json.dumps(_ORIGINAL_METADATA),
                    "css": ".x{}",
                }
            },
        )
    )
    copy = respx.post(f"{BASE}/api/v1/dashboard/155/copy/").mock(
        return_value=httpx.Response(200, json={"id": 500, "last_modified_time": 1})
    )
    new_positions = json.loads(json.dumps(_ORIGINAL_POSITIONS))
    new_positions["CHART-a"]["meta"]["chartId"] = 2001
    new_positions["CHART-b"]["meta"]["chartId"] = 2002
    respx.get(f"{BASE}/api/v1/dashboard/500").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "id": 500,
                    "position_json": json.dumps(new_positions),
                    "json_metadata": json.dumps(_ORIGINAL_METADATA),
                }
            },
        )
    )
    put_copy = respx.put(f"{BASE}/api/v1/dashboard/500").mock(return_value=httpx.Response(200, json={"result": {}}))
    respx.get(f"{BASE}/api/v1/chart/2001").mock(
        return_value=httpx.Response(200, json={"result": {"id": 2001, "slice_name": "РНП WB"}})
    )
    respx.get(f"{BASE}/api/v1/chart/2002").mock(
        return_value=httpx.Response(200, json={"result": {"id": 2002, "slice_name": "Сводка WB"}})
    )
    rename_a = respx.put(f"{BASE}/api/v1/chart/2001").mock(return_value=httpx.Response(200, json={"result": {}}))
    rename_b = respx.put(f"{BASE}/api/v1/chart/2002").mock(return_value=httpx.Response(200, json={"result": {}}))
    # Anything touching the original dashboard, its charts or any dataset/role must not happen.
    forbidden = [
        respx.put(f"{BASE}/api/v1/dashboard/155"),
        respx.put(url__regex=rf"{BASE}/api/v1/chart/(889|934)$"),
        respx.put(url__regex=rf"{BASE}/api/v1/dataset/.*"),
        respx.post(url__regex=rf"{BASE}/api/v1/security/roles/.*"),
    ]
    for route in forbidden:
        route.mock(return_value=httpx.Response(500))

    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "superset_dashboard_copy",
            {
                "dashboard_id": 155,
                "dashboard_title": "[TEST] РНП",
                "duplicate_charts": True,
                "chart_name_prefix": "[TEST] ",
            },
        )

    payload = json.loads(_text(result))
    assert payload["id"] == 500
    assert payload["chart_id_map"] == {"889": 2001, "934": 2002}

    sent = json.loads(copy.calls.last.request.content)
    assert sent["duplicate_slices"] is True
    assert sent["css"] == ".x{}"
    sent_md = json.loads(sent["json_metadata"])
    assert sent_md["positions"] == _ORIGINAL_POSITIONS
    assert sent_md["color_scheme"] == "supersetColors"

    fixed = json.loads(json.loads(put_copy.calls.last.request.content)["json_metadata"])
    assert fixed["native_filter_configuration"][0]["chartsInScope"] == [2001]
    assert fixed["native_filter_configuration"][0]["scope"]["excluded"] == [2002]

    assert json.loads(rename_a.calls.last.request.content) == {"slice_name": "[TEST] РНП WB"}
    assert json.loads(rename_b.calls.last.request.content) == {"slice_name": "[TEST] Сводка WB"}
    assert all(not route.called for route in forbidden)


@respx.mock
async def test_dashboard_copy_with_charts_needs_a_saved_layout(mcp_server):
    respx.get(f"{BASE}/api/v1/dashboard/7").mock(
        return_value=httpx.Response(200, json={"result": {"id": 7, "position_json": None, "json_metadata": "{}"}})
    )
    copy = respx.post(f"{BASE}/api/v1/dashboard/7/copy/")

    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "superset_dashboard_copy", {"dashboard_id": 7, "dashboard_title": "x", "duplicate_charts": True}
        )

    assert "no saved layout" in json.loads(_text(result))["error"]
    assert not copy.called


# --- switching a chart's dataset ---------------------------------------------------------


@respx.mock
async def test_chart_update_switches_datasource_and_keeps_params(mcp_server):
    _csrf()
    respx.get(f"{BASE}/api/v1/chart/2002").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "id": 2002,
                    "datasource_id": 197,
                    "params": json.dumps({"datasource": "197__table", "viz_type": "pivot_table_v2", "metrics": ["m1"]}),
                    "query_context": json.dumps(
                        {"datasource": {"id": 197, "type": "table"}, "form_data": {"datasource": "197__table"}}
                    ),
                    "dashboards": [{"id": 500, "dashboard_title": "[TEST] РНП"}],
                }
            },
        )
    )
    put = respx.put(f"{BASE}/api/v1/chart/2002").mock(return_value=httpx.Response(200, json={"result": {"id": 2002}}))

    async with Client(mcp_server) as c:
        result = await c.call_tool("superset_chart_update", {"chart_id": 2002, "datasource_id": 234})

    sent = json.loads(put.calls.last.request.content)
    assert sent["datasource_id"] == 234
    assert sent["datasource_type"] == "table"
    params = json.loads(sent["params"])
    assert params == {"datasource": "234__table", "viz_type": "pivot_table_v2", "metrics": ["m1"]}
    qc = json.loads(sent["query_context"])
    assert qc["datasource"] == {"id": 234, "type": "table"}
    assert qc["form_data"]["datasource"] == "234__table"

    changed = json.loads(_text(result))["_datasource_changed"]
    assert changed == {"from": 197, "to": 234, "dashboards": [{"id": 500, "title": "[TEST] РНП"}]}


@respx.mock
async def test_chart_update_switches_datasource_without_query_context(mcp_server):
    _csrf()
    respx.get(f"{BASE}/api/v1/chart/2001").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {"id": 2001, "datasource_id": 197, "params": "{}", "query_context": None, "dashboards": []}
            },
        )
    )
    put = respx.put(f"{BASE}/api/v1/chart/2001").mock(return_value=httpx.Response(200, json={"result": {"id": 2001}}))

    async with Client(mcp_server) as c:
        await c.call_tool("superset_chart_update", {"chart_id": 2001, "datasource_id": 234})

    sent = json.loads(put.calls.last.request.content)
    assert "query_context" not in sent
    assert json.loads(sent["params"]) == {"datasource": "234__table"}


def test_results_key_is_a_quoted_rison_string():
    # Unquoted, a key starting with a digit is parsed as a number and Superset rejects it.
    assert (
        queries._results_rison("18125d80-236e-4044-b89d-d65781dd47bd") == "(key:'18125d80-236e-4044-b89d-d65781dd47bd')"
    )
    assert queries._results_rison("a'b!c") == "(key:'a!'b!!c')"

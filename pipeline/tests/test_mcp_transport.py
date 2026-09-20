"""The two transports (PRD §E.3): streamable HTTP mounted on the API at /mcp, and stdio for a launched client.

Over HTTP the SDK's own client talks to the FastAPI app through an in-process ASGI transport, so the whole
path is exercised (the route, the session manager inside the app's lifespan, the bearer key or the loopback
rule) with no socket. Over stdio a child Python process serves the same server on its pipes and the SDK's
stdio client launches it, which is what Claude Code and Cursor do. Both run the real tools on the synthetic
store; nothing here touches the live one or calls a model."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from typer.testing import CliRunner

from legacy_reader import paths as LP
from legacy_reader.api import app as A
from legacy_reader.mcp import TOOL_VERSION
from legacy_reader.mcp import contract as C
from legacy_reader.mcp import server as SV
from legacy_reader.mcp import transport as TR
from legacy_reader.mcp.cli import mcp_app

from mcp_world import CELL, numbers_outside_vals, synthetic_store
from test_api import FakeBackend

BASE = "http://127.0.0.1:8787"


@pytest.fixture
def store(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    return synthetic_store(prospect_sandbox, monkeypatch)


@pytest.fixture
def app(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The API app with the MCP route mounted, its runs under the test's directory and no key register."""
    monkeypatch.delenv("LR_MCP_KEYS", raising=False)
    monkeypatch.delenv("LR_MCP_KEY", raising=False)
    monkeypatch.setattr(SV, "PATHS", LP.Paths(tmp_path))
    return A.create_app(lambda: FakeBackend(), model="fake-model", effort="low", backend_name="fake", db_path=tmp_path / "t.duckdb")


def over_http(app: Any, scenario: Callable[[Client], Awaitable[None]], headers: dict[str, str] | None = None) -> None:
    """A stock client over streamable HTTP against the app, its lifespan running as uvicorn would run it."""

    async def main() -> None:
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=headers or {}) as http:
                async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
                    await scenario(client)

    anyio.run(main)


async def gated_round_trip(client: Client) -> None:
    """What §E.5 asks for: a stock client asks about a cell and gets a gated answer."""
    assert client.server_info is not None and client.server_info.version == TOOL_VERSION
    assert [t.name for t in (await client.list_tools()).tools] == list(C.CATALOGUE)
    r = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "dashboard"})
    assert not r.is_error, r.content[0].text
    sid = r.structured_content["session_id"]
    feats = await client.call_tool("cell_features", {"session_id": sid})
    assert not feats.is_error, feats.content[0].text
    sc = feats.structured_content
    assert sc["rows"] and numbers_outside_vals(sc) == []
    cond = next(row for row in sc["rows"] if row["feature"] == "d_conductor_m")["value"]
    ok = await client.call_tool("check_claims", {"session_id": sid, "claims": [
        {"text": f"the nearest conductor is {cond['value']} m away", "value_ids": [cond["id"]]}]})
    assert ok.structured_content["ok"] is True
    bad = await client.call_tool("check_claims", {"session_id": sid, "claims": [
        {"text": "the nearest conductor is 999 m away", "value_ids": [cond["id"]]}]})
    assert bad.structured_content["ok"] is False and "999" in bad.structured_content["problems"][0]
    scored = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "scored", "fold": 2})
    assert not scored.is_error
    scores = await client.call_tool("cell_scores", {"session_id": scored.structured_content["session_id"]})
    assert not scores.is_error and all(row["out_of_fold"] is True for row in scores.structured_content["rows"])


def test_the_api_serves_the_contract_at_mcp_to_a_loopback_client(app: Any, tmp_path: Path) -> None:
    assert any(getattr(r, "path", None) == "/mcp" for r in app.router.routes), "one route, declared before any catch-all"
    with TestClient(app) as c:
        assert c.get("/api/health").json()["ok"] is True, "the API is unchanged by the mount"
    over_http(app, gated_round_trip)
    runs = list((tmp_path / "pipeline" / "data" / "runs").glob("*-mcp*"))
    assert len(runs) == 2 and all((r / "manifest.json").is_file() and (r / "spans.jsonl").is_file() for r in runs)
    assert all(json.loads((r / "manifest.json").read_text())["finished_at"] for r in runs), "closed with the app"


def test_with_a_register_the_route_needs_a_bearer_key(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LR_MCP_KEYS", "reader-key:read;runner-key:read,record,run")
    monkeypatch.setattr(SV, "PATHS", LP.Paths(tmp_path))
    app = A.create_app(lambda: FakeBackend(), model="fake-model", effort="low", backend_name="fake", db_path=tmp_path / "t.duckdb")

    async def refused(client: Client) -> None:
        with pytest.raises(MCPError) as err:
            await client.list_tools()
        assert "Bearer" in str(err.value) and "reader-key" not in str(err.value)
        r = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "dashboard"})
        assert r.is_error and "Bearer" in r.content[0].text

    async def as_reader(client: Client) -> None:
        names = [t.name for t in (await client.list_tools()).tools]
        assert "record_insight" not in names and "cell_features" in names
        r = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "dashboard"})
        assert not r.is_error and r.structured_content["cell"] == CELL

    over_http(app, refused)
    over_http(app, refused, headers={"Authorization": "Basic reader-key"})
    over_http(app, as_reader, headers={"Authorization": "Bearer reader-key"})
    over_http(app, gated_round_trip, headers={"Authorization": "Bearer runner-key"})


def test_the_standalone_http_app_serves_the_same_route(store: Path, tmp_path: Path) -> None:
    lr = SV.build(environ={}, runs_dir=tmp_path / "runs")
    app = TR.http_app(lr)
    over_http(app, gated_round_trip)


def test_stdio_serves_a_client_that_launched_the_process(store: Path, tmp_path: Path) -> None:
    child = textwrap.dedent("""
        import sys
        from pathlib import Path
        db, tmp = Path(sys.argv[1]), Path(sys.argv[2])
        from legacy_reader import store as ST
        from legacy_reader.store import snapshot as SN
        ST.db_path = lambda: db
        SN.db_path = lambda: db
        SN.snapshots_dir = lambda: tmp / "snapshots"
        from legacy_reader.mcp.server import build
        from legacy_reader.mcp.transport import serve_stdio
        print("a stray print before serving must not reach the wire either")
        serve_stdio(build(environ={}, runs_dir=tmp / "runs"))
    """)
    params = StdioServerParameters(command=sys.executable, args=["-c", child, str(store), str(tmp_path)],
                                   env={"LR_TRACING_MLFLOW": "0", "PATH": "/usr/bin:/bin"})

    async def main() -> None:
        async with Client(params) as client:
            await gated_round_trip(client)

    anyio.run(main)
    runs = list((tmp_path / "runs").glob("*-mcp*"))
    assert len(runs) == 2 and all((r / "spans.jsonl").is_file() for r in runs)


def test_the_cli_names_the_catalogue_and_mints_a_key_but_serves_one_transport_at_a_time() -> None:
    runner = CliRunner()
    out = runner.invoke(mcp_app, ["tools"])
    assert out.exit_code == 0 and "open_session" in out.output and "serverInfo.version prospect/tools/v2" in out.output
    lines = {ln.split()[0]: ln.split()[1:] for ln in out.output.splitlines() if ln.startswith("  ")}
    assert lines["record_insight"] == ["action", "scope", "record"] and lines["cell_features"] == ["read-only", "scope", "read"]
    assert lines["run_analyst"] == ["task", "scope", "run"] and list(lines) == list(C.CATALOGUE)
    listed = json.loads(runner.invoke(mcp_app, ["tools", "--json"]).output)
    assert [t["name"] for t in listed] == list(C.CATALOGUE) and listed[1]["annotations"]["readOnlyHint"] is True
    key = runner.invoke(mcp_app, ["key", "--scopes", "read,record"])
    assert key.exit_code == 0 and "LR_MCP_KEYS=" in key.output and ":read,record" in key.output
    assert runner.invoke(mcp_app, ["key", "--scopes", "admin"]).exit_code != 0
    both = runner.invoke(mcp_app, ["serve", "--stdio", "--http"])
    assert both.exit_code != 0 and "one transport" in both.output
    neither = runner.invoke(mcp_app, ["serve"])
    assert neither.exit_code != 0

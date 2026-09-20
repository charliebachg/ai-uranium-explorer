"""The MCP server through the SDK's in-memory client (PRD §E.3, §E.5): a stock client gets a gated answer.

Every test here is a client round trip with no network and no model: the server runs in-process over the
synthetic store and the session tests' fakes, whose leakage keywords are ignored on purpose, so a rule that
holds here holds because the session dropped the row and not because the tool was polite. The client
validates every non-error result against the tool's outputSchema, so a bare number anywhere would fail the
call before a test could look at it."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
import duckdb
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from legacy_reader import store as ST
from legacy_reader.mcp import TOOL_VERSION
from legacy_reader.mcp import contract as C
from legacy_reader.mcp import server as SV
from legacy_reader.mcp.handlers import NOT_AVAILABLE, WITHHELD
from legacy_reader.runtime.manifest import Manifest
from legacy_reader.runtime.tracing import read_spans

from fake_session_world import BLIND_FILE, FAR_FILE, SERVED_SCORE
from mcp_world import (CELL, CX_FILE, CX_HOLE, FOLD, OTHER, FakeWorld, crosscheck_fixture, make_server,
                       numbers_outside_vals, synthetic_store)

Scenario = Callable[[Client], Awaitable[None]]


@pytest.fixture
def store(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    return synthetic_store(prospect_sandbox, monkeypatch)


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


def run(lr: SV.LrServer, scenario: Scenario) -> None:
    """One client session against the server, then the server closed so every manifest is finished."""

    async def main() -> None:
        try:
            async with Client(lr.server) as client:
                await scenario(client)
        finally:
            lr.close()

    anyio.run(main)


async def opened(client: Client, purpose: str = "scored", **over: Any) -> tuple[str, dict[str, Any]]:
    args = {"cell_id": CELL, "purpose": purpose, **({"fold": FOLD} if purpose != "dashboard" else {}), **over}
    r = await client.call_tool("open_session", args)
    assert not r.is_error, r.content[0].text
    return r.structured_content["session_id"], r.structured_content


# ---------------------------------------------------------------- the list and the shape


def test_server_info_and_the_tool_list_are_the_contract(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        assert client.server_info is not None and client.server_info.version == TOOL_VERSION
        first = await client.list_tools(cache_mode="bypass")
        second = await client.list_tools(cache_mode="bypass")
        assert [t.name for t in first.tools] == list(C.CATALOGUE) == [t.name for t in second.tools]
        assert first.ttl_ms == SV.LIST_TTL_MS and first.cache_scope == "private"
        for tool in first.tools:
            spec = C.CATALOGUE[tool.name]
            assert tool.annotations.open_world_hint is False
            assert tool.annotations.read_only_hint is (spec.kind == "read"), tool.name
            assert tool.output_schema["$defs"]["Unknown"] != tool.output_schema["$defs"]["Absent"]

    run(make_server(tmp_path, world), scenario)


def test_every_number_in_every_tool_result_carries_an_id(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    calls = [("cell_features", {}), ("cell_scores", {}), ("criteria_breakdown", {}), ("label_context", {}),
             ("nearby", {"layer": "em_conductors"}), ("coverage", {}), ("crosscheck", {}),
             ("retrieve", {"query": "conductor"})]

    async def scenario(client: Client) -> None:
        world.served_scores_allowed = True
        sid, opened_doc = await opened(client, "dashboard")
        assert numbers_outside_vals(opened_doc) == [] and opened_doc["fold"]["state"] == "absent"
        seen: set[str] = set()
        for name, args in calls:
            r = await client.call_tool(name, {"session_id": sid, **args})
            assert not r.is_error, (name, r.content[0].text)
            sc = r.structured_content
            assert sc["tool"] == name and sc["cell"] == CELL and sc["rows"], name
            assert numbers_outside_vals(sc) == [], (name, numbers_outside_vals(sc))
            for vid in sc["values"]:
                assert f"[{vid}]" in r.content[0].text, f"{name}: the prose puts {vid} beside its number"
            seen |= set(sc["values"])
        live = next(iter(tmp_path.glob("runs/*")))
        manifest = json.loads((live / "manifest.json").read_text())
        assert manifest["config"]["n_calls"] == len(calls)
        # the gate middleware: every id served is in the session's registry, minted ones included
        r = await client.call_tool("check_claims", {"session_id": sid, "claims": [
            {"text": f"{len(seen)} ids", "value_ids": sorted(seen)}]})
        assert r.structured_content["unresolved"] == [] and len(r.structured_content["resolved"]) == len(seen)

    run(make_server(tmp_path, world), scenario)


def test_unknown_and_absent_are_distinct_types_in_a_served_result(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "dashboard")
        feats = (await client.call_tool("cell_features", {"session_id": sid})).structured_content
        sed = next(r for r in feats["rows"] if r["feature"] == "sed_u_max_ppm")
        assert sed["value"]["state"] == "unknown" and sed["value"]["nearest_observation"]["value"] == 4200.0
        assert all(v is not None for v in sed.values()) and "unit" not in sed, "no null where a type is due; a null unit is not served"
        x = (await client.call_tool("crosscheck", {"session_id": sid})).structured_content
        pair = {r["pair"]: r for r in x["rows"]}
        assert pair["conductor_fault"]["state"] == "absent" and pair["conductor_fault"]["absent"]["state"] == "absent"
        assert pair["conductor_fault"]["d_fault_m"]["state"] == "unknown", "one side unknown, the pair absent: both said"
        assert pair["sediment_sampling"]["sed_u_max_ppm"]["state"] == "unknown"
        assert pair["sediment_sampling"]["thin_sampling"]["state"] == "unknown", "a rule that cannot be applied is unknown"
        assert pair["sediment_sampling"]["min_samples"]["value"] == 3, "the rule's threshold is a value with an id"
        text = (await client.call_tool("crosscheck", {"session_id": sid})).content[0].text
        assert "absent (no mapped fault within the radius)" in text and "unknown (" in text

    run(make_server(tmp_path, world), scenario)


# ---------------------------------------------------------------- the leakage rules through the wire


def test_a_scored_session_refuses_a_blind_listed_file_and_serves_only_out_of_fold_scores(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, doc = await opened(client, "scored")
        assert doc["fold"]["value"] == FOLD and len(doc["blind_list_hash"]) == 64
        r = await client.call_tool("retrieve", {"session_id": sid, "query": "conductor"})
        assert not r.is_error
        rows = r.structured_content["rows"]
        assert world.received["retrieve"][-1]["exclude_files"] == [BLIND_FILE], "the blind-list reached the tool"
        assert BLIND_FILE not in json.dumps(r.structured_content) and BLIND_FILE not in r.content[0].text
        assert all("file" not in row for row in rows), "a scored session scrubs file numbers"
        assert any(row.get("tier") == "expert" for row in rows), "the far passage and the expert note survive"
        assert "c:pass:0:page" not in r.structured_content["values"], "and the dropped row's values go with it"
        s = await client.call_tool("cell_scores", {"session_id": sid})
        assert not s.is_error, s.content[0].text
        srows = s.structured_content["rows"]
        assert srows and all(row["out_of_fold"] is True and row["fold"]["value"] == FOLD for row in srows)
        assert {row["model"] for row in srows} == {"learned", "effort"}, "the fold-1 criteria row is another fold's model"
        assert SERVED_SCORE not in [v.get("value") for v in s.structured_content["values"].values()]
        assert world.received["cell_scores"] == [], "the served table was never asked for (B18)"
        other = await client.call_tool("cell_features", {"session_id": sid, "cell_id": OTHER})
        assert other.is_error and "own cell only" in other.content[0].text
    lr = make_server(tmp_path, world)
    run(lr, scenario)
    manifest = Manifest.read(next(iter(tmp_path.glob("runs/*"))))
    assert manifest.finished_at and manifest.blind_list_sha256 and manifest.config["session"]["refusals"]["B17"] >= 1
    assert manifest.config["session"]["refusals"]["B18"] >= 1 and manifest.scores_seen
    assert all(s["fold_kind"] == "spatial" and s["fold"] == FOLD for s in manifest.scores_seen)


def test_the_label_mask_holds(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "scored")
        r = await client.call_tool("label_context", {"session_id": sid})
        assert world.received["label_context"][-1]["mask_cell"] == CELL, "the mask reached the tool"
        rows = r.structured_content["rows"]
        assert rows and all(row["distance_km"]["value"] > 0 for row in rows), "the 0.0 km row is gone regardless (B30)"
        assert "Cigar Lake" not in json.dumps(r.structured_content) and "Rabbit Lake" not in r.content[0].text
        world.served_scores_allowed = True
        dsid, _ = await opened(client, "dashboard")
        d = await client.call_tool("label_context", {"session_id": dsid})
        assert any(row["distance_km"]["value"] == 0.0 and row["name"] == "Cigar Lake" for row in d.structured_content["rows"]), \
            "the dashboard keeps the cell's own label: it is the first thing a skeptic asks"

    run(make_server(tmp_path, world), scenario)


def test_a_benchmark_session_shows_a_bench_id_and_refuses_a_real_cell_id(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, doc = await opened(client, "benchmark", bench_id="b-0007")
        assert doc["cell"] == "b-0007" and doc["fold"]["id"] == "b:b-0007:session:fold"
        r = await client.call_tool("cell_features", {"session_id": sid})
        assert not r.is_error and r.structured_content["cell"] == "b-0007"
        assert CELL not in json.dumps(r.structured_content) and all(k.startswith("b:") for k in r.structured_content["values"])
        r = await client.call_tool("cell_features", {"session_id": sid, "cell_id": CELL})
        assert r.is_error and "own cell only" in r.content[0].text

    run(make_server(tmp_path, world), scenario)


# ---------------------------------------------------------------- the gate as a callable, abstain, run_analyst


def test_a_claim_with_an_unbacked_number_fails_check_claims(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "scored")
        feats = (await client.call_tool("cell_features", {"session_id": sid})).structured_content
        cond = next(r for r in feats["rows"] if r["feature"] == "d_conductor_m")["value"]
        good = {"text": f"the nearest conductor is {cond['value']:g} m away", "value_ids": [cond["id"]]}
        bad = {"text": "the nearest conductor is 640 m away", "value_ids": [cond["id"]]}
        made_up = {"text": "there are 17 holes", "value_ids": ["c:cell:0000_0000:no_such"]}
        r = await client.call_tool("check_claims", {"session_id": sid, "claims": [good, bad, made_up]})
        sc = r.structured_content
        assert sc["ok"] is False and sc["resolved"] == [cond["id"]] and sc["unresolved"] == ["c:cell:0000_0000:no_such"]
        assert any("640" in p for p in sc["problems"]) and any("no_such" in p for p in sc["problems"])
        assert "claim 0" not in " ".join(sc["problems"]), "the backed claim passes"
        r = await client.call_tool("check_claims", {"session_id": sid, "claims": [good]})
        assert r.structured_content["ok"] is True and "every number backed" in r.content[0].text
        km = {"text": "the conductor is 0.82 km away", "value_ids": [cond["id"]]}
        assert (await client.call_tool("check_claims", {"session_id": sid, "claims": [km]})).structured_content["ok"], \
            "a unit the gate can verify itself is not a fabrication"

    run(make_server(tmp_path, world), scenario)


def test_abstain_is_recorded_on_the_session_and_its_manifest(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "scored")
        r = await client.call_tool("abstain", {"session_id": sid, "reason": "not_measured", "detail": "no boulder count here"})
        assert not r.is_error and r.structured_content["abstain_id"].startswith("a:") and r.structured_content["reason"] == "not_measured"
        r2 = await client.call_tool("abstain", {"session_id": sid, "reason": "out_of_scope"})
        assert r2.structured_content["abstain_id"] != r.structured_content["abstain_id"]
        bad = await client.call_tool("abstain", {"session_id": sid, "reason": "dunno"})
        assert bad.is_error and "not_measured" in bad.content[0].text and "dunno" in bad.content[0].text

    run(make_server(tmp_path, world), scenario)
    manifest = Manifest.read(next(iter(tmp_path.glob("runs/*"))))
    assert [a["reason"] for a in manifest.config["abstentions"]] == ["not_measured", "out_of_scope"]
    assert manifest.config["abstentions"][0]["detail"] == "no boulder count here"


def test_run_analyst_is_a_stub_that_says_so(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "scored")
        r = await client.call_tool("run_analyst", {"session_id": sid, "budget_usd": 1.0})
        assert r.is_error and r.content[0].text == NOT_AVAILABLE and "not available" in NOT_AVAILABLE

    run(make_server(tmp_path, world), scenario)


# ---------------------------------------------------------------- handles, keys and scopes


def test_a_handle_is_opaque_bound_to_its_key_and_expires(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    now = [dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)]
    env_a = {"LR_MCP_KEYS": "alpha-key:read;beta-key:read,record", "LR_MCP_KEY": "alpha-key"}
    lr = make_server(tmp_path, world, environ=env_a, clock=lambda: now[0])
    handle: list[str] = []

    async def as_alpha(client: Client) -> None:
        sid, doc = await opened(client, "scored")
        handle.append(sid)
        assert len(sid) == 32 and CELL not in sid and doc["expires_at"] == "2026-09-21T16:00:00+00:00"
        r = await client.call_tool("cell_features", {"session_id": sid})
        assert not r.is_error
        r = await client.call_tool("cell_features", {})
        assert r.is_error and "session_id" in r.content[0].text

    async def as_beta(client: Client) -> None:
        r = await client.call_tool("cell_features", {"session_id": handle[0]})
        assert r.is_error and "another key" in r.content[0].text
        r = await client.call_tool("check_claims", {"session_id": handle[0], "claims": []})
        assert r.is_error, "the gate is bound to the key too"

    async def as_alpha_later(client: Client) -> None:
        now[0] += dt.timedelta(hours=5)
        r = await client.call_tool("cell_features", {"session_id": handle[0]})
        assert r.is_error and "expired" in r.content[0].text
        assert len(lr.store) == 0, "an expired handle is swept and its manifest finished"

    async def main() -> None:
        async with Client(lr.server) as client:
            await as_alpha(client)
        lr.environ = {**env_a, "LR_MCP_KEY": "beta-key"}
        async with Client(lr.server) as client:
            await as_beta(client)
        lr.environ = env_a
        async with Client(lr.server) as client:
            await as_alpha_later(client)

    anyio.run(main)
    manifest = Manifest.read(next(iter(tmp_path.glob("runs/*"))))
    assert manifest.config["principal"].startswith("key:") and "alpha-key" not in json.dumps(manifest.as_dict()), \
        "a key never appears in a manifest: only its label"


def test_scopes_filter_tools_list_and_refuse_a_call_outside_them(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    env = {"LR_MCP_KEYS": "reader:read;writer:read,record;runner:read,record,run"}

    async def as_reader(client: Client) -> None:
        names = [t.name for t in (await client.list_tools(cache_mode="bypass")).tools]
        assert "record_insight" not in names and "run_analyst" not in names and "check_claims" in names
        sid, _ = await opened(client, "dashboard")
        r = await client.call_tool("record_insight", {"session_id": sid, "text": "x", "author": "a"})
        assert r.is_error and "`record` scope" in r.content[0].text

    async def as_runner(client: Client) -> None:
        names = [t.name for t in (await client.list_tools(cache_mode="bypass")).tools]
        assert names == list(C.CATALOGUE), "every scope: the whole catalogue, in order"

    async def as_nobody(client: Client) -> None:
        with pytest.raises(MCPError) as err:
            await client.list_tools(cache_mode="bypass")
        assert "LR_MCP_KEY" in str(err.value) and "reader" not in str(err.value)
        r = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "dashboard"})
        assert r.is_error and "LR_MCP_KEY" in r.content[0].text, "a tool call gets a result, not a protocol error"

    for key, scenario in (("reader", as_reader), ("runner", as_runner), ("no-such-key", as_nobody)):
        run(make_server(tmp_path, world, environ={**env, "LR_MCP_KEY": key}), scenario)


def test_without_a_register_a_local_caller_has_every_scope(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        assert [t.name for t in (await client.list_tools()).tools] == list(C.CATALOGUE)

    run(make_server(tmp_path, world, environ={}), scenario)


# ---------------------------------------------------------------- record_insight and hole_crosscheck


def test_record_insight_writes_the_expert_tier_and_mints_expert_ids(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    lr = make_server(tmp_path, world, insight_store=lambda: ST.connect(store))

    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "dashboard")
        r = await client.call_tool("record_insight", {
            "session_id": sid, "author": "C. Geologist",
            "text": "The conductor probably continues 1.5 km north-east of hole Q6-1, offset 3 times along strike."})
        assert not r.is_error, r.content[0].text
        sc = r.structured_content
        assert sc["expert_id"].startswith("e:") and sc["cell"] == CELL and sc["author"] == "C. Geologist"
        assert len(sc["value_ids"]) == 2 and numbers_outside_vals(sc) == []
        vals = sc["values"]
        assert {v["value"] for v in vals.values()} == {1.5, 3} and all(v["tier"] == "expert" for v in vals.values())
        assert all(vid.startswith(f"c:insight:{CELL}:") for vid in vals)
        claim = {"text": "the geologist puts the continuation 1.5 km away", "value_ids": [next(k for k, v in vals.items() if v["value"] == 1.5)]}
        g = await client.call_tool("check_claims", {"session_id": sid, "claims": [claim]})
        assert g.structured_content["ok"] is True, "an expert-tier id is citable like any other"
        on_other = await client.call_tool("record_insight", {"session_id": sid, "cell_id": OTHER, "text": "x", "author": "a"})
        assert on_other.is_error and "own cell" in on_other.content[0].text

    run(lr, scenario)
    con = duckdb.connect(str(store), read_only=True)
    try:
        rows = con.execute("select expert_id, cell_id, author, value_ids_json, values_json, session_id, run_id, tier "
                           "from expert.insight").fetchall()
    finally:
        con.close()
    assert len(rows) == 1 and rows[0][1] == CELL and rows[0][2] == "C. Geologist" and rows[0][7] == "expert"
    assert len(json.loads(rows[0][3])) == 2 and set(json.loads(rows[0][4])) == set(json.loads(rows[0][3]))
    manifest = Manifest.read(tmp_path / "runs" / rows[0][6])
    assert manifest.config["insights"][0]["expert_id"] == rows[0][0] and manifest.config["session"]["n_expert_ids"] == 2


def test_hole_crosscheck_serves_the_crosscheck_files_of_a_cell(store: Path, world: FakeWorld, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    con = duckdb.connect(str(store), read_only=True)
    try:
        lon, lat = con.execute("select lon, lat from derived.cell where cell_id = ?", [CELL]).fetchone()
    finally:
        con.close()
    crosscheck_fixture(tmp_path / "crosscheck", monkeypatch, (lon + 0.005, lat))

    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "dashboard")
        r = await client.call_tool("hole_crosscheck", {"session_id": sid})
        assert not r.is_error, r.content[0].text
        sc = r.structured_content
        assert numbers_outside_vals(sc) == []
        kinds = [row["kind"] for row in sc["rows"]]
        assert kinds == ["summary", "match", "match"]
        summary, matched, unplaced = sc["rows"]
        assert summary["file"] == CX_FILE and summary["matches_n"]["value"] == 2 and summary["queue_n"]["value"] == 1
        assert summary["signatures_n"]["value"] == 1
        assert matched["hole_id"] == CX_HOLE and matched["offset_m"]["id"] == f"d:{CX_FILE}:off_ab12cd34"
        assert matched["offset_m"]["value"] == 36.2 and matched["bearing_deg"]["value"] == 289.0
        assert matched["datum_shift_signature"] is True and matched["adjudication"] == "needed"
        assert matched["differences"]["total_depth_m"]["value"] == -1.5 and matched["differences"]["dip_deg"]["state"] == "unknown"
        assert unplaced["offset_m"]["state"] == "absent" and unplaced["bearing_deg"]["state"] == "absent"
        assert f"[d:{CX_FILE}:off_ab12cd34]" in r.content[0].text
        one = await client.call_tool("hole_crosscheck", {"session_id": sid, "hole_id": "KL-101"})
        assert [row["kind"] for row in one.structured_content["rows"]] == ["summary", "match"], "a hole by its printed name"
        far = await client.call_tool("hole_crosscheck", {"session_id": sid, "radius_km": 0.5, "cell_id": OTHER})
        assert not far.is_error and far.structured_content["rows"] == [] and "No crosschecked file" in far.content[0].text
        none = await client.call_tool("hole_crosscheck", {"session_id": sid, "file_num": "99Z99-0000"})
        assert none.is_error and "lr crosscheck" in none.content[0].text
        cite = {"text": "the collar sits 36.2 m from the geods record", "value_ids": [f"d:{CX_FILE}:off_ab12cd34"]}
        assert (await client.call_tool("check_claims", {"session_id": sid, "claims": [cite]})).structured_content["ok"]
        bsid, _ = await opened(client, "scored")
        blinded = await client.call_tool("hole_crosscheck", {"session_id": bsid})
        assert blinded.is_error and "dashboard sessions only" in blinded.content[0].text

    run(make_server(tmp_path, world), scenario)


# ---------------------------------------------------------------- spans, the public-safe build, resources, prompts


def test_every_call_is_a_span_with_an_arguments_hash_result_ids_and_latency(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "benchmark", bench_id="b-0009")
        await client.call_tool("cell_features", {"session_id": sid})
        await client.call_tool("retrieve", {"session_id": sid, "query": "graphitic conductor"})
        await client.call_tool("cell_features", {"session_id": "nope"})

    run(make_server(tmp_path, world), scenario)
    run_dir = next(iter(tmp_path.glob("runs/*")))
    spans = read_spans(run_dir)
    mine = [s for s in spans if s["name"].startswith("mcp:")]
    assert [s["name"] for s in mine] == ["mcp:open_session", "mcp:cell_features", "mcp:retrieve"], "a bad handle has no session to trace under"
    for s in mine:
        a = s["attrs"]
        assert a["session_id"] and a["run_id"] == run_dir.name and len(a["args_sha256"]) == 64 and s["duration_ms"] >= 0
        assert a["principal"] == "local" and isinstance(a["result_ids"], list) and a["is_error"] is False
    assert mine[1]["attrs"]["result_ids"] and all(v.startswith("b:") for v in mine[1]["attrs"]["result_ids"])
    dumped = json.dumps(spans)
    assert CELL not in dumped and "graphitic conductor" not in dumped, "argument values never reach a span: the hash does"
    inner = [s for s in spans if s["name"] == "tool:retrieve"]
    assert inner and inner[0]["parent_id"] == mine[2]["span_id"], "the session's own tool span nests under the call's"


def test_the_public_safe_build_withholds_report_text_and_the_hole_crosscheck(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        sid, _ = await opened(client, "dashboard")
        r = await client.call_tool("retrieve", {"session_id": sid, "query": "conductor"})
        rows = r.structured_content["rows"]
        pages = [row for row in rows if row["tier"] == "page"]
        assert pages and all(row["text"] == WITHHELD for row in pages), "a report's words are not redistributable"
        assert all(row["file"] in (BLIND_FILE, FAR_FILE) and row["page"]["value"] for row in pages), "the citation and its ids stay"
        assert next(row for row in rows if row["tier"] == "expert")["text"].startswith("A geologist"), "an expert note is ours"
        assert "Cluff Lake" not in r.content[0].text
        h = await client.call_tool("hole_crosscheck", {"session_id": sid})
        assert h.is_error and "public-safe" in h.content[0].text
        inv = json.loads((await client.read_resource("lr://reading/inventory")).contents[0].text)
        assert {s["key"]: s["licence"]["redistributable"] for s in inv["sources"]}["geods_holes"] is False

    run(make_server(tmp_path, world, public_safe=True), scenario)


def test_resources_and_prompts_are_served_and_a_prompt_needs_its_handle(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        names = [r.name for r in (await client.list_resources()).resources]
        assert names == ["handbook", "criteria", "readiness_gate", "reading_inventory"]
        templates = [t.uri_template for t in (await client.list_resource_templates()).resource_templates]
        assert templates == ["lr://cell/{id}/evidence", "lr://cell/{id}/chains", "lr://run/{id}/manifest"]
        handbook = (await client.read_resource("lr://handbook")).contents[0]
        assert handbook.mime_type == "text/markdown" and len(handbook.text) > 1000
        criteria = (await client.read_resource("lr://criteria")).contents[0]
        assert criteria.mime_type == "application/toml" and "[[criteria]]" in criteria.text or "criteria" in criteria.text
        chains = json.loads((await client.read_resource(f"lr://cell/{CELL}/chains")).contents[0].text)
        assert chains["cell_id"] == CELL and chains["chains"] == []
        with pytest.raises(MCPError) as err:
            await client.read_resource("lr://cell/not-a-cell/chains")
        assert "0123_0045" in str(err.value)
        with pytest.raises(MCPError):
            await client.read_resource("lr://run/no-such-run/manifest")
        sid, doc = await opened(client, "scored")
        manifest = json.loads((await client.read_resource(f"lr://run/{doc['run_id']}/manifest")).contents[0].text)
        assert manifest["config"]["session_id"] == sid and manifest["config"]["tool_contract"] == TOOL_VERSION
        prompts = [p.name for p in (await client.list_prompts()).prompts]
        assert prompts == ["proponent", "skeptic", "adjudicator", "analyst.executor", "analyst.verifier", "interface.router"]
        for name in prompts:
            got = await client.get_prompt(name, {"cell_id": CELL, "session_id": sid})
            text = got.messages[0].content.text
            assert sid in text and "check_claims" in text and "abstain" in text and "never compute a number" in text
        with pytest.raises(MCPError) as err:
            await client.get_prompt("skeptic", {"cell_id": CELL})
        assert "session_id" in str(err.value)

    run(make_server(tmp_path, world), scenario)


def test_arguments_outside_the_schema_are_refused_with_the_field_named(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    async def scenario(client: Client) -> None:
        r = await client.call_tool("open_session", {"cell_id": "not-a-cell", "purpose": "dashboard"})
        assert r.is_error and "cell_id" in r.content[0].text
        r = await client.call_tool("open_session", {"cell_id": "9999_9999", "purpose": "dashboard"})
        assert r.is_error and "outside the grid" in r.content[0].text
        r = await client.call_tool("open_session", {"cell_id": CELL, "purpose": "audit"})
        assert r.is_error and "purpose" in r.content[0].text
        sid, _ = await opened(client, "dashboard")
        r = await client.call_tool("nearby", {"session_id": sid, "layer": "smdi_uranium"})
        assert r.is_error and "layer" in r.content[0].text
        r = await client.call_tool("nearby", {"session_id": sid, "layer": "em_conductors", "radius_m": 100})
        assert r.is_error and "radius_m" in r.content[0].text
        r = await client.call_tool("no_such_tool", {"session_id": sid})
        assert r.is_error and "no tool named" in r.content[0].text
        r = await client.call_tool("cell_features", {"session_id": sid, "extra": 1})
        assert r.is_error and "extra" in r.content[0].text

    run(make_server(tmp_path, world), scenario)

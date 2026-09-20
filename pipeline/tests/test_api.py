"""The FastAPI service: the same contract as the stdlib server it replaced, plus persisted conversations.

The chat backend is a scripted fake and the tools are stubbed, so nothing here calls a model or reads the real
store; conversations land in a temporary DuckDB with the real schema applied."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uranium_explorer.api import app as A
from uranium_explorer.api import persist
from uranium_explorer.prospect import serve as S
from uranium_explorer.prospect import tools as T
from uranium_explorer.values import stat

LOOPBACK = ("127.0.0.1", 50000)


class FakeBackend:
    """Answers straight away, without a number, so the gate has nothing to refuse."""

    def __init__(self, text: str = "The conductor sits close by; nothing here has been drilled to basement.") -> None:
        self.text = text
        self.calls = 0

    def call(self, req, on_delta=None):
        self.calls += 1

        class R:
            structured = {"action": "answer", "reasoning": "the staged record answers it",
                          "answer": {"text": self.text, "claims": []}}
            cost_usd = 0.01

        return R()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(
        tool, args, rows=[{"feature": "d_conductor_m", "value": 820.0}],
        values={"c:cell:0001_0001:d_conductor_m": stat("c:cell:0001_0001:d_conductor_m", 820.0, fmt="m1", unit="m")}))
    monkeypatch.setattr(S, "candidates", lambda limit=40, model="criteria": [
        {"cell_id": "0001_0001", "score": 0.9, "known_share": 0.8, "lon": -105.1, "lat": 57.9, "in_basin": True,
         "label_tier": "unlabelled", "label_name": "", "km_to_label": 12.3}][:limit])
    monkeypatch.setattr(S, "evidence", lambda cell_id: {
        "cell_id": cell_id, "lon": -105.1, "lat": 57.9, "in_basin": True,
        "parts": {"cell_scores": {"tool": "cell_scores", "args": {"cell_id": cell_id}, "note": "", "rows": [{"model": "criteria", "score": 0.5}],
                                  "values": {"c:score:x": {"id": "c:score:x", "kind": "stat", "value": 0.5, "fmt": "ratio3"}}}},
        "values": {"c:score:x": {"id": "c:score:x", "kind": "stat", "value": 0.5, "fmt": "ratio3"}},
        "memos": [{"memo_id": "m1", "role": "skeptic", "verdict": "insufficient", "published": True, "created_at": "t",
                   "claims": [{"claim_no": 1, "text": "score 0.5", "value_ids": ["c:score:x"]}]}]})
    backend = FakeBackend()
    app = A.create_app(lambda: backend, model="fake-model", effort="low", backend_name="fake", db_path=tmp_path / "t.duckdb")
    # from a loopback address, as a browser on the same machine is: with no key register that is the `local`
    # principal with every role (Starlette's test client would otherwise call itself "testclient")
    c = TestClient(app, client=LOOPBACK)
    c.backend = backend  # type: ignore[attr-defined]
    c.db_path = tmp_path / "t.duckdb"  # type: ignore[attr-defined]
    return c


def test_health_names_the_model_and_backend(client: TestClient) -> None:
    r = client.get("/api/health")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["model"] == "fake-model" and body["backend"] == "fake"
    assert body["reads"] in ("duckdb", "postgis") and set(body["evidence_cache"]) == {"hits", "misses"}


def test_cells_and_evidence_keep_the_old_contract(client: TestClient) -> None:
    cells = client.get("/api/cells?limit=5").json()["cells"]
    assert [c["cell_id"] for c in cells] == ["0001_0001"] and cells[0]["km_to_label"] == 12.3
    ev = client.get("/api/cell/0001_0001").json()
    assert ev["cell_id"] == "0001_0001" and ev["memos"][0]["claims"][0]["value_ids"] == ["c:score:x"]
    assert "chains" in ev, "the response model passes the analyst chains through (a strict model once stripped them)"
    assert client.get("/api/cell/not-a-cell").status_code == 400
    assert client.get("/api/cells?limit=0").status_code == 422


def test_a_chat_turn_is_gated_persisted_and_continued(client: TestClient) -> None:
    r = client.post("/api/chat", json={"cell_id": "0001_0001", "question": "Has the conductor been tested?"})
    assert r.status_code == 200, r.text
    body = r.json()
    cid = body["conversation_id"]
    assert body["turn"]["published"] is True and body["turn"]["text"].startswith("The conductor")
    assert body["cell_id"] == "0001_0001" and body["cost_usd"] >= 0

    stored = persist.load_conversation(cid, client.db_path)
    assert stored["cell_id"] == "0001_0001" and stored["model"] == "fake-model" and stored["backend"] == "fake"
    assert stored["requested_by"] == "local" and stored["turns"][0]["requested_by"] == "local", "who asked is on the row"
    assert len(stored["turns"]) == 1 and stored["turns"][0]["published"] and stored["turns"][0]["problems"] == []
    assert [c["tool"] for c in stored["turns"][0]["tool_calls"]] == ["cell_scores", "cell_features", "criteria_breakdown", "label_context"], \
        "the first turn records the four opening tool calls; the fake then answered without asking for more"
    assert stored["turns"][0]["duration_s"] >= 0

    r2 = client.post("/api/chat", json={"cell_id": "0001_0001", "question": "And the depth?", "conversation_id": cid})
    assert r2.json()["conversation_id"] == cid
    turns = persist.load_conversation(cid, client.db_path)["turns"]
    assert len(turns) == 2 and turns[1]["tool_calls"] == [], "the second turn made no new tool call"
    assert client.get(f"/api/conversation/{cid}").json()["turns"][1]["question"] == "And the depth?"
    listed = client.get("/api/cell/0001_0001/conversations").json()["conversations"]
    assert listed[0]["conversation_id"] == cid and listed[0]["turns"] == 2


def test_a_stored_conversation_is_rehydrated_after_a_restart(client: TestClient) -> None:
    cid = client.post("/api/chat", json={"cell_id": "0001_0001", "question": "first"}).json()["conversation_id"]
    client.app.state.registry.live.clear()  # the process restarted; the store did not
    r = client.post("/api/chat", json={"cell_id": "0001_0001", "question": "second", "conversation_id": cid})
    assert r.json()["conversation_id"] == cid
    conv = client.app.state.registry.live[cid]
    assert [t["question"] for t in conv.turns] == ["first", "second"], "the transcript carried over"


def test_a_conversation_id_for_another_cell_starts_a_new_one(client: TestClient) -> None:
    cid = client.post("/api/chat", json={"cell_id": "0001_0001", "question": "q"}).json()["conversation_id"]
    r = client.post("/api/chat", json={"cell_id": "0002_0002", "question": "q", "conversation_id": cid})
    assert r.json()["conversation_id"] != cid


def test_bad_requests_are_refused_before_any_model_call(client: TestClient) -> None:
    assert client.post("/api/chat", json={"cell_id": "nope", "question": "q"}).status_code == 422
    assert client.post("/api/chat", json={"cell_id": "0001_0001", "question": ""}).status_code == 422
    assert client.backend.calls == 0


def test_the_stream_emits_the_loop_events_and_ends_with_done(client: TestClient) -> None:
    with client.stream("POST", "/api/chat/stream", json={"cell_id": "0001_0001", "question": "stream it"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in r.iter_lines() if line]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "opened" and "thinking" in kinds and "checking" in kinds and kinds[-1] == "done"
    done = events[-1]
    assert done["turn"]["published"] is True and done["conversation_id"]
    assert persist.load_conversation(done["conversation_id"], client.db_path)["turns"][0]["question"] == "stream it"


def test_a_backend_failure_is_reported_on_the_stream_not_swallowed(client: TestClient) -> None:
    def boom(req, on_delta=None):
        raise RuntimeError("upstream fell over")

    client.backend.call = boom
    with client.stream("POST", "/api/chat/stream", json={"cell_id": "0001_0001", "question": "x"}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line]
    assert events[-1]["type"] == "error" and "upstream fell over" in events[-1]["error"]
    assert client.post("/api/chat", json={"cell_id": "0001_0001", "question": "x"}).status_code == 500


def test_reads_work_on_a_fresh_store_before_any_conversation_exists(client: TestClient) -> None:
    """A read-only connection never creates tables; the app applies the schema at startup instead."""
    assert client.get("/api/cell/0001_0001/conversations").json() == {"cell_id": "0001_0001", "conversations": []}
    assert client.get("/api/conversation/nope").status_code == 404


def test_the_openapi_document_types_every_response(client: TestClient) -> None:
    spec = client.app.openapi()
    schemas = spec["components"]["schemas"]
    for name in ("Cells", "Candidate", "Evidence", "ToolResultOut", "Val", "ChatResponse", "Turn", "ConversationRecord", "CellConversations",
                 "Job", "CellJobs", "JobRequest", "Whoami"):
        assert name in schemas, name
    ok = spec["paths"]["/api/cells"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/Cells"}, "the cells response is a named schema, not additionalProperties"



def test_evidence_assembles_memos_and_claims_from_the_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercises serve.evidence itself, which the stubbed routes above bypass: it lost its json import once."""
    from uranium_explorer.store import connect

    db = tmp_path / "s.duckdb"
    con = connect(db)
    con.execute("insert into derived.cell (cell_id, grid_id, col, row, cx, cy, lon, lat, geom_wkb, in_basin) "
                "values ('0001_0001', 'g', 1, 1, 0.0, 0.0, -105.1, 57.9, '\\x00'::blob, true)")
    con.execute("insert into agent.memo (memo_id, cell_id, role, verdict, model, prompt_version, run_id, created_at, published) "
                "values ('m1', '0001_0001', 'skeptic', 'insufficient', 'fake', 'v', 'r', 't', true)")
    con.execute("insert into agent.memo_claim (memo_id, claim_no, text, value_ids) values ('m1', 1, 'score 0.5', '[\"c:score:x\"]')")
    con.close()
    monkeypatch.setattr(S, "connect", lambda read_only=False: connect(db, read_only=read_only))
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(tool, args, rows=[], values={}))
    ev = S.evidence("0001_0001")
    assert ev["lon"] == -105.1 and ev["memos"][0]["claims"] == [{"claim_no": 1, "text": "score 0.5", "value_ids": ["c:score:x"]}]


def test_the_built_site_is_served_from_the_same_process_behind_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>AI Uranium Explorer</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(S, "candidates", lambda limit=40, model="criteria": [])
    app = A.create_app(lambda: None, model="m", db_path=tmp_path / "t.duckdb", web_dist=dist)
    c = TestClient(app, client=LOOPBACK)
    assert c.get("/api/health").json()["ok"] is True, "the API is not shadowed by the site"
    assert c.get("/").text.startswith("<!doctype html>")
    assert c.get("/eval").text.startswith("<!doctype html>"), "the app's own routes fall back to index.html"
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert c.get("/../etc/passwd").status_code in (200, 404) and "root:" not in c.get("/../etc/passwd").text


# ---------------------------------------------------------------- serving reads


def test_the_evidence_cache_serves_a_record_once_per_store_version() -> None:
    from uranium_explorer.api.reads import EvidenceCache

    calls = []
    stamp = {"v": 1.0}
    cache = EvidenceCache(compute=lambda cid: calls.append(cid) or {"cell_id": cid}, stamp=lambda: stamp["v"])
    assert cache.get("a")["cell_id"] == "a" and cache.get("a")["cell_id"] == "a"
    assert calls == ["a"] and cache.hits == 1 and cache.misses == 1
    stamp["v"] = 2.0  # the store was written: every cached record is stale at once
    cache.get("a")
    assert calls == ["a", "a"]


def test_candidates_fall_back_to_duckdb_when_postgis_fails_and_retry_later(monkeypatch: pytest.MonkeyPatch) -> None:
    from uranium_explorer.api import reads

    def pg_down(limit, model, dsn):
        raise ConnectionError("refused")

    c = reads.Candidates(dsn="postgresql://x", duck=lambda limit, model: [{"cell_id": "duck"}], pg=pg_down)
    assert c.get(5, "criteria") == [{"cell_id": "duck"}] and c.source == "duckdb"
    c.pg = lambda limit, model, dsn: [{"cell_id": "pg"}]
    assert c.get(5, "criteria") == [{"cell_id": "duck"}], "a failed serving database is not retried on every request"
    monkeypatch.setattr(reads.time, "monotonic", lambda: c.failed_at + reads.PG_RETRY_S + 1)
    assert c.get(5, "criteria") == [{"cell_id": "pg"}] and c.source == "postgis"
    assert reads.Candidates(dsn="", duck=lambda limit, model: [], pg=pg_down).get(1, "criteria") == []


def test_ten_readers_of_one_cold_cell_cost_one_computation() -> None:
    import threading

    from uranium_explorer.api.reads import EvidenceCache

    calls = []
    started = threading.Event()

    def slow(cid):
        calls.append(cid)
        started.wait(0.2)
        return {"cell_id": cid}

    cache = EvidenceCache(compute=slow, stamp=lambda: 1.0)
    threads = [threading.Thread(target=cache.get, args=("z",)) for _ in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert calls == ["z"] and cache.misses == 1 and cache.hits == 9


def test_in_one_mode_a_read_only_request_gets_a_writable_connection_and_the_schema_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from uranium_explorer import store

    db = tmp_path / "one.duckdb"
    monkeypatch.setenv("UE_STORE_RW", "1")
    applied = []
    real = store.apply_schema
    monkeypatch.setattr(store, "apply_schema", lambda con: applied.append(1) or real(con))
    a = store.connect(db, read_only=True)
    a.execute("create table t (x integer)")   # would raise on a read-only handle
    a.close()
    b = store.connect(db, read_only=True)
    b.execute("insert into t values (1)")
    b.close()
    assert len(applied) == 1, "the schema is applied once per path per process in one mode"
    monkeypatch.delenv("UE_STORE_RW")
    c = store.connect(db, read_only=True)
    with pytest.raises(Exception):
        c.execute("insert into t values (2)")
    c.close()

"""The FastAPI service: the same contract as the stdlib server it replaced, plus persisted conversations.

The chat backend is a scripted fake and the tools are stubbed, so nothing here calls a model or reads the real
store; conversations land in a temporary DuckDB with the real schema applied."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from legacy_reader.api import app as A
from legacy_reader.api import persist
from legacy_reader.prospect import serve as S
from legacy_reader.prospect import tools as T
from legacy_reader.values import stat


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
    monkeypatch.setattr(S, "candidates", lambda limit=40, model="criteria": [{"cell_id": "0001_0001", "score": 0.9}][:limit])
    monkeypatch.setattr(S, "evidence", lambda cell_id: {"cell_id": cell_id, "parts": [], "values": {}, "memos": []})
    backend = FakeBackend()
    app = A.create_app(lambda: backend, model="fake-model", effort="low", backend_name="fake", db_path=tmp_path / "t.duckdb")
    c = TestClient(app)
    c.backend = backend  # type: ignore[attr-defined]
    c.db_path = tmp_path / "t.duckdb"  # type: ignore[attr-defined]
    return c


def test_health_names_the_model_and_backend(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True, "model": "fake-model", "effort": "low", "backend": "fake"}


def test_cells_and_evidence_keep_the_old_contract(client: TestClient) -> None:
    assert client.get("/api/cells?limit=5").json() == {"cells": [{"cell_id": "0001_0001", "score": 0.9}]}
    assert client.get("/api/cell/0001_0001").json()["cell_id"] == "0001_0001"
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

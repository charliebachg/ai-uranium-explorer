"""The review queue over HTTP: listed for anyone, resolved only by a geologist, and never rewriting a reading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from legacy_reader.api import app as A
from legacy_reader.extractor import agree as AG
from legacy_reader.extractor import queue as Q
from legacy_reader.prospect import serve as S
from legacy_reader.store import connect


def cand(field: str, value: str, vid: str | None = None, bbox=None) -> AG.Candidate:
    return AG.Candidate(field=field, scope="cell", as_printed=value, value_id=vid, bbox=bbox, row_index=1,
                        model="claude-opus-5" if vid else "z-ai/glm-5.3-flash")


def seed(db: Path) -> list[dict]:
    pairs = [AG.Pair("disagreed", cand("to_depth", "42.0", "x:F1:aaaa", [0.1, 0.1, 0.2, 0.12]), cand("to_depth", "42.5"), "row", "0.5 apart"),
             AG.Pair("only_a", cand("grade", "0.8", "x:F1:bbbb", [0.5, 0.1, 0.6, 0.12]), None, "none"),
             AG.Pair("only_b", None, cand("sample_id", "219"), "none"),
             AG.Pair("agreed", cand("from_depth", "37.0", "x:F1:cccc"), cand("from_depth", "37.0"), "box")]
    common = dict(run_id="run-1", file_num="F1", page_id="pg:x:0004", page_no=4, model_a="claude-opus-5", model_b="z-ai/glm-5.3-flash")
    agreement = [Q.agreement_row(p, prompt_version="pv", **common) for p in pairs]
    queue = [Q.queue_row(p, **common) for p in pairs if p.status != "agreed"]
    other = Q.queue_row(AG.Pair("only_a", cand("grade", "1.1", "x:F2:dddd"), None, "none"),
                        run_id="run-1", file_num="F2", page_id="pg:y:0002", page_no=2, model_a="m", model_b="n")
    con = connect(db)
    try:
        Q.file_rows(con, agreement, queue + [other])
    finally:
        con.close()
    return queue


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(S, "candidates", lambda limit=40, model="criteria": [])
    monkeypatch.setenv("LR_MCP_KEYS", "geo-key:read,record;viewer-key:read")
    db = tmp_path / "t.duckdb"
    app = A.create_app(lambda: None, model="fake", effort="low", backend_name="fake", db_path=db)
    c = TestClient(app)
    c.queue = seed(db)  # type: ignore[attr-defined]
    c.db = db  # type: ignore[attr-defined]
    return c


def test_open_items_are_listed_paged_and_filtered_by_file(client: TestClient) -> None:
    body = client.get("/api/review").json()
    assert body["total"] == 4 and body["status"] == "open" and body["counts"] == {"open": 4}
    assert {i["reason"] for i in body["items"]} == {"disagreed", "only_a", "only_b"}
    d = next(i for i in body["items"] if i["reason"] == "disagreed")
    assert d["reading_a"]["as_printed"] == "42.0" and d["reading_b"]["as_printed"] == "42.5"
    assert d["reading_a"]["bbox"] == [0.1, 0.1, 0.2, 0.12] and d["value_id"] == "x:F1:aaaa" and d["field_type"] == "depth"
    only = client.get("/api/review?file=F1&limit=2").json()
    assert only["total"] == 3 and len(only["items"]) == 2 and only["file_num"] == "F1"
    rest = client.get("/api/review?file=F1&limit=2&offset=2").json()
    assert len(rest["items"]) == 1
    assert client.get("/api/review?status=nonsense").status_code == 422


def test_resolving_needs_the_geologist_role_and_records_who_decided(client: TestClient) -> None:
    qid = next(i["queue_id"] for i in client.get("/api/review?file=F1").json()["items"] if i["reason"] == "disagreed")
    assert client.post(f"/api/review/{qid}", json={"decision": "accepted_b"}).status_code == 401
    r = client.post(f"/api/review/{qid}", json={"decision": "accepted_b"}, headers={"X-Api-Key": "viewer-key"})
    assert r.status_code == 403 and "geologist" in r.json()["detail"]
    r = client.post(f"/api/review/{qid}", json={"decision": "accepted_b", "note": "the scan reads 42.5"},
                    headers={"X-Api-Key": "geo-key"})
    assert r.status_code == 200
    item = r.json()
    assert item["status"] == "accepted_b" and item["resolved_by"].startswith("key:") and "geo-key" not in json.dumps(item)
    assert item["resolution"]["reading"]["as_printed"] == "42.5" and item["resolution"]["note"] == "the scan reads 42.5"
    assert item["reading_a"]["as_printed"] == "42.0" and item["reading_b"]["as_printed"] == "42.5", "the readings stay"
    # resolved once: a second decision is refused, and the open list no longer carries it
    again = client.post(f"/api/review/{qid}", json={"decision": "rejected"}, headers={"X-Api-Key": "geo-key"})
    assert again.status_code == 409
    assert qid not in {i["queue_id"] for i in client.get("/api/review").json()["items"]}
    assert client.get("/api/review?status=accepted_b").json()["total"] == 1
    assert client.post("/api/review/nope", json={"decision": "rejected"}, headers={"X-Api-Key": "geo-key"}).status_code == 404


def test_an_edit_needs_a_value_and_an_accept_needs_that_reading(client: TestClient) -> None:
    items = client.get("/api/review?file=F1").json()["items"]
    only_b = next(i["queue_id"] for i in items if i["reason"] == "only_b")
    headers = {"X-Api-Key": "geo-key"}
    assert client.post(f"/api/review/{only_b}", json={"decision": "accepted_a"}, headers=headers).status_code == 422
    assert client.post(f"/api/review/{only_b}", json={"decision": "edited"}, headers=headers).status_code == 422
    r = client.post(f"/api/review/{only_b}", json={"decision": "edited", "value": {"as_printed": "218", "field": "sample_id"}},
                    headers=headers)
    assert r.status_code == 200 and r.json()["resolution"]["reading"]["as_printed"] == "218"


def test_the_original_rows_are_never_rewritten(client: TestClient) -> None:
    con = connect(client.db, read_only=True)  # type: ignore[attr-defined]
    try:
        before = con.execute("select agreement_id, status, reading_a_json, reading_b_json from read.agreement order by 1").fetchall()
    finally:
        con.close()
    qid = client.get("/api/review?file=F1").json()["items"][0]["queue_id"]
    assert client.post(f"/api/review/{qid}", json={"decision": "rejected"}, headers={"X-Api-Key": "geo-key"}).status_code == 200
    con = connect(client.db, read_only=True)  # type: ignore[attr-defined]
    try:
        after = con.execute("select agreement_id, status, reading_a_json, reading_b_json from read.agreement order by 1").fetchall()
        row = con.execute("select reading_a_json, reading_b_json, status from read.review_item where queue_id = ?", [qid]).fetchone()
    finally:
        con.close()
    assert before == after, "a resolution is recorded on the queue row alone"
    assert row[2] == "rejected" and row[0] is not None or row[1] is not None


def test_with_no_register_a_local_caller_is_the_local_principal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "candidates", lambda limit=40, model="criteria": [])
    monkeypatch.delenv("LR_MCP_KEYS", raising=False)
    db = tmp_path / "t.duckdb"
    app = A.create_app(lambda: None, model="fake", effort="low", backend_name="fake", db_path=db)
    c = TestClient(app, client=("127.0.0.1", 50000))   # a browser on the same machine, not "testclient"
    seed(db)
    qid = c.get("/api/review").json()["items"][0]["queue_id"]
    r = c.post(f"/api/review/{qid}", json={"decision": "rejected"})
    assert r.status_code == 200 and r.json()["resolved_by"] == "local"

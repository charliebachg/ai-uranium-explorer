"""Auth and roles on the API (PRD §A.2), and the job routes over HTTP.

One key register for the API and the MCP server: a fake `LR_MCP_KEYS` here, never a real key. The chat backend
is the scripted fake of `test_api`, the tools are stubbed, and the job kind is a fake that finishes at once,
so nothing here calls a model or touches the live store."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from legacy_reader.api import app as A
from legacy_reader.api import auth as AUTH
from legacy_reader.api import jobs as J
from legacy_reader.api import persist
from legacy_reader.mcp.auth import Keyring, Principal, parse_keys
from legacy_reader.prospect import serve as S
from legacy_reader.prospect import tools as T
from legacy_reader.values import stat

from test_api import LOOPBACK, FakeBackend

REGISTER = "viewer-key:read;geo-key:read,record;admin-key:read,record,run;odd-key:run"
CELL = "0201_0072"


GATES: dict[int, threading.Event] = {}


def fake_kind(role: str = "admin") -> J.JobKind:
    gate = threading.Event()

    def run(job: dict[str, Any], progress: J.Progress) -> dict[str, Any]:
        progress.event("stage:plan")
        gate.wait(2.0)
        return {"chain_id": f"run-1:{job['cell_id']}", "verdict": "insufficient", "cost_usd": 0.05}

    def prepare(cell_id: str | None, args: dict[str, Any]) -> dict[str, Any]:
        if cell_id != CELL:
            raise J.JobRefused(f"the analyst runs only on the enabled cells (PRD §9.4), and {cell_id} is not one of them")
        return {"budget_usd": float(args.get("budget_usd", 0.5)), "reason": str(args.get("reason") or "")}

    kind = J.JobKind("analyst", role, run, prepare, lambda args: args["budget_usd"])
    GATES[id(kind)] = gate   # the kind is frozen; the test finds its gate by the kind
    return kind


def let_go(kind: J.JobKind) -> None:
    GATES[id(kind)].set()


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(
        tool, args, rows=[{"feature": "d_conductor_m", "value": 820.0}],
        values={f"c:cell:{CELL}:d_conductor_m": stat(f"c:cell:{CELL}:d_conductor_m", 820.0, fmt="m1", unit="m")}))
    monkeypatch.setattr(S, "candidates", lambda limit=40, model="criteria": [])
    monkeypatch.setattr(S, "evidence", lambda cell_id: {"cell_id": cell_id, "parts": {}, "values": {}, "memos": []})
    # the serving process runs in one connection mode; a job worker writes while a request reads
    monkeypatch.setenv("LR_STORE_RW", "1")


def make_client(tmp_path: Path, register: str | None, *, client: tuple[str, int] = LOOPBACK,
                kind: J.JobKind | None = None) -> TestClient:
    keyring = Keyring(parse_keys(register)) if register else Keyring()
    db = tmp_path / "t.duckdb"
    runner = J.Runner(db, kinds={"analyst": kind or fake_kind()}, workers=1, session_budget_usd=1.0)
    app = A.create_app(lambda: FakeBackend(), model="fake-model", effort="low", backend_name="fake", db_path=db,
                       keyring=keyring, jobs=runner)
    c = TestClient(app, client=client)
    c.db_path = db  # type: ignore[attr-defined]
    c.runner = runner  # type: ignore[attr-defined]
    return c


def as_key(key: str, header: str = "bearer") -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if header == "bearer" else {"X-Api-Key": key}


# ---------------------------------------------------------------- roles over scopes


def test_roles_are_sets_of_the_mcp_scopes() -> None:
    assert AUTH.ROLES == {"viewer": {"read"}, "geologist": {"read", "record"}, "admin": {"read", "record", "run"}}
    assert AUTH.roles_of(Principal("k", frozenset({"read"}))) == ["viewer"]
    assert AUTH.roles_of(Principal("k", frozenset({"read", "record"}))) == ["viewer", "geologist"]
    assert AUTH.roles_of(Principal("k", frozenset({"read", "record", "run"}))) == ["viewer", "geologist", "admin"]
    assert AUTH.roles_of(Principal("k", frozenset({"run"}))) == [], "a role is all of its scopes or none of it"
    with pytest.raises(ValueError, match="no role"):
        AUTH.has_role(Principal("k", frozenset()), "owner")


def test_without_a_register_a_loopback_client_is_local_with_every_role_and_another_address_is_refused(
        tmp_path: Path, stubs: None) -> None:
    local = make_client(tmp_path, None)
    me = local.get("/api/whoami").json()
    assert me == {"name": "local", "scopes": ["read", "record", "run"], "roles": ["viewer", "geologist", "admin"]}
    r = local.post("/api/chat", json={"cell_id": CELL, "question": "q"})
    assert r.status_code == 200 and persist.load_conversation(r.json()["conversation_id"], local.db_path)["requested_by"] == "local"
    remote = make_client(tmp_path / "remote", None, client=("10.0.0.7", 4000))
    r = remote.get("/api/whoami")
    assert r.status_code == 401 and "LR_MCP_KEYS is not configured" in r.json()["detail"] and "10.0.0.7" in r.json()["detail"]
    assert remote.post("/api/chat", json={"cell_id": CELL, "question": "q"}).status_code == 401
    assert remote.get("/api/health").status_code == 200, "health and the reads of the record need no principal"
    assert remote.get(f"/api/cell/{CELL}").status_code == 200


def test_with_a_register_every_caller_presents_a_key_by_either_header(tmp_path: Path, stubs: None) -> None:
    c = make_client(tmp_path, REGISTER)
    r = c.get("/api/whoami")
    assert r.status_code == 401 and "Authorization: Bearer" in r.json()["detail"] and "X-Api-Key" in r.json()["detail"]
    assert "viewer-key" not in r.text, "the register's contents never leave the process"
    assert c.get("/api/whoami", headers=as_key("no-such-key")).status_code == 401
    assert c.get("/api/whoami", headers={"Authorization": "Basic viewer-key"}).status_code == 401
    bearer = c.get("/api/whoami", headers=as_key("geo-key")).json()
    header = c.get("/api/whoami", headers=as_key("geo-key", "x-api-key")).json()
    assert bearer == header and bearer["roles"] == ["viewer", "geologist"] and bearer["name"].startswith("key:")
    assert "geo-key" not in bearer["name"], "a principal is named by its label, never its key"
    assert c.get("/api/whoami", headers=as_key("odd-key")).json()["roles"] == []


def test_chat_needs_the_geologist_role_and_records_who_asked(tmp_path: Path, stubs: None) -> None:
    c = make_client(tmp_path, REGISTER)
    refused = c.post("/api/chat", json={"cell_id": CELL, "question": "q"}, headers=as_key("viewer-key"))
    assert refused.status_code == 403
    assert "needs the geologist role" in refused.json()["detail"] and "holds viewer" in refused.json()["detail"]
    assert "viewer-key" not in refused.text
    with c.stream("POST", "/api/chat/stream", json={"cell_id": CELL, "question": "q"}, headers=as_key("viewer-key")) as s:
        assert s.status_code == 403
    ok = c.post("/api/chat", json={"cell_id": CELL, "question": "q"}, headers=as_key("geo-key"))
    assert ok.status_code == 200, ok.text
    label = c.get("/api/whoami", headers=as_key("geo-key")).json()["name"]
    stored = persist.load_conversation(ok.json()["conversation_id"], c.db_path)
    assert stored["requested_by"] == label and stored["turns"][0]["requested_by"] == label
    assert c.get(f"/api/conversation/{stored['conversation_id']}").json()["turns"][0]["requested_by"] == label
    # a second turn on the same conversation by another key is that key's turn
    again = c.post("/api/chat", json={"cell_id": CELL, "question": "more", "conversation_id": stored["conversation_id"]},
                   headers=as_key("admin-key"))
    turns = persist.load_conversation(again.json()["conversation_id"], c.db_path)["turns"]
    assert turns[1]["requested_by"] == c.get("/api/whoami", headers=as_key("admin-key")).json()["name"] != label


# ---------------------------------------------------------------- the job routes


def test_a_job_is_submitted_polled_listed_and_its_requester_recorded(tmp_path: Path, stubs: None) -> None:
    kind = fake_kind()
    c = make_client(tmp_path, REGISTER, kind=kind)
    body = {"kind": "analyst", "cell_id": CELL, "args": {"reason": "the scores disagree"}}
    assert c.post("/api/jobs", json=body).status_code == 401
    assert c.post("/api/jobs", json=body, headers=as_key("geo-key")).status_code == 403, "analyst jobs need admin"
    r = c.post("/api/jobs", json=body, headers=as_key("admin-key"))
    assert r.status_code == 202, r.text
    job = r.json()
    admin = c.get("/api/whoami", headers=as_key("admin-key")).json()["name"]
    assert job["kind"] == "analyst" and job["cell_id"] == CELL and job["requested_by"] == admin
    assert job["status"] in ("queued", "running") and job["args"] == {"budget_usd": 0.5, "reason": "the scores disagree"}
    assert c.get(f"/api/jobs/{job['job_id']}").status_code == 401, "reading a job needs a principal"
    seen = c.get(f"/api/jobs/{job['job_id']}", headers=as_key("viewer-key")).json()
    assert seen["job_id"] == job["job_id"], "a viewer may watch a job"
    listed = c.get(f"/api/cell/{CELL}/jobs", headers=as_key("viewer-key")).json()
    assert listed["cell_id"] == CELL and [j["job_id"] for j in listed["jobs"]] == [job["job_id"]]
    let_go(kind)
    done = c.runner.wait(job["job_id"])
    final = c.get(f"/api/jobs/{job['job_id']}", headers=as_key("viewer-key")).json()
    assert done["status"] == final["status"] == "done"
    assert final["result"] == {"chain_id": f"run-1:{CELL}", "verdict": "insufficient", "cost_usd": 0.05}
    assert [e["event"] for e in final["progress"]] == ["queued", "started", "stage:plan", "done"]
    assert c.get("/api/jobs/nope", headers=as_key("viewer-key")).status_code == 404
    assert c.get("/api/cell/nope/jobs", headers=as_key("viewer-key")).status_code == 400


def test_a_refused_submission_is_a_400_with_the_reason(tmp_path: Path, stubs: None) -> None:
    c = make_client(tmp_path, None)
    r = c.post("/api/jobs", json={"kind": "analyst", "cell_id": "0000_0001"})
    assert r.status_code == 400 and "only on the enabled cells (PRD §9.4)" in r.json()["detail"]
    assert c.post("/api/jobs", json={"kind": "corpus", "cell_id": CELL}).status_code == 404
    assert c.post("/api/jobs", json={"kind": "analyst", "cell_id": "not-a-cell"}).status_code == 422
    over = c.post("/api/jobs", json={"kind": "analyst", "cell_id": CELL, "args": {"budget_usd": 1.5}})
    assert over.status_code == 400 and "session budget of $1.00" in over.json()["detail"]
    assert c.get(f"/api/cell/{CELL}/jobs").json()["jobs"] == []


def test_cancel_needs_the_kinds_role_and_stops_the_job(tmp_path: Path, stubs: None) -> None:
    kind = fake_kind()
    c = make_client(tmp_path, REGISTER, kind=kind)
    first = c.post("/api/jobs", json={"kind": "analyst", "cell_id": CELL, "args": {"budget_usd": 0.4}},
                   headers=as_key("admin-key")).json()
    second = c.post("/api/jobs", json={"kind": "analyst", "cell_id": CELL, "args": {"budget_usd": 0.4}},
                    headers=as_key("admin-key")).json()
    assert c.post(f"/api/jobs/{second['job_id']}/cancel", headers=as_key("viewer-key")).status_code == 403
    cancelled = c.post(f"/api/jobs/{second['job_id']}/cancel", headers=as_key("admin-key")).json()
    assert cancelled["status"] == "cancelled" and cancelled["error"] == "cancelled before it started"
    asked = c.post(f"/api/jobs/{first['job_id']}/cancel", headers=as_key("admin-key")).json()
    assert asked["status"] == "running" and asked["progress"][-1]["event"] == "cancel_requested"
    let_go(kind)
    c.runner.wait(first["job_id"])
    assert c.get(f"/api/jobs/{first['job_id']}", headers=as_key("viewer-key")).json()["status"] == "done", \
        "a kind that never checks the flag runs to its end; the analyst kind checks at every model call"
    assert c.post("/api/jobs/nope/cancel", headers=as_key("admin-key")).status_code == 404


def test_the_openapi_document_names_the_job_routes_and_the_api_spec_needs_no_live_store(tmp_path: Path, stubs: None) -> None:
    c = make_client(tmp_path, None)
    paths = c.app.openapi()["paths"]
    for path, method in (("/api/jobs", "post"), ("/api/jobs/{job_id}", "get"), ("/api/cell/{cell_id}/jobs", "get"),
                         ("/api/jobs/{job_id}/cancel", "post"), ("/api/whoami", "get")):
        assert method in paths[path], (path, method)
    ok = paths["/api/jobs"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/Job"}

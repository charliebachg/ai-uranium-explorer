"""The job runner (PRD §A.2, background jobs): durable rows in `agent.job` on a temporary store built from
`schema.sql`, a bounded pool inside the process, and the analyst kind over the fake loop world.

No model is called anywhere here: the fake kind is a function that waits on an event, and the analyst kind
runs on the scripted `LoopBackend` over the fake tool world exactly as the chain-run tests do."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from legacy_reader.analyst import arms as A
from legacy_reader.analyst import chains as CH
from legacy_reader.analyst.session import Session
from legacy_reader.api import jobs as J
from legacy_reader.store import connect, tier_audit

from fake_bench import install_runtime
from fake_loop_world import CELL as WORLD_CELL, LoopBackend, LoopWorld, loop_registry
from test_analyst_chain_run import fake_cards

ENABLED = "0201_0072"


# ---------------------------------------------------------------- a fake kind


class Gate:
    """A kind whose work waits until the test lets it go, so queued, running and cancelled are observable."""

    def __init__(self) -> None:
        self.go = threading.Event()
        self.started = threading.Event()
        self.seen: list[dict[str, Any]] = []

    def prepare(self, cell_id: str | None, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("bad"):
            raise J.JobRefused("bad is not allowed")
        return {"n": int(args.get("n", 1)), "budget_usd": float(args.get("budget_usd", 0.0))}

    def run(self, job: dict[str, Any], progress: J.Progress) -> dict[str, Any]:
        self.seen.append(job)
        progress.event("stage:one")
        self.started.set()
        self.go.wait(5.0)
        progress.check()
        progress.event("stage:two")
        if job["args"]["n"] < 0:
            raise ValueError("n must not be negative")
        return {"n": job["args"]["n"], "cost_usd": job["args"]["budget_usd"] / 2}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "jobs.duckdb"


def make_runner(db: Path, gate: Gate, **over: Any) -> J.Runner:
    kind = J.JobKind("fake", "geologist", gate.run, gate.prepare, lambda args: args["budget_usd"])
    kw: dict[str, Any] = dict(kinds={"fake": kind}, workers=1, session_budget_usd=1.0)
    kw.update(over)
    return J.Runner(db, **kw)


def test_a_job_is_queued_run_and_done_with_its_progress_and_result_in_the_store(db: Path) -> None:
    gate = Gate()
    r = make_runner(db, gate)
    job_id = r.submit("fake", ENABLED, {"n": 3, "budget_usd": 0.2}, requested_by="key:abc12345")
    row = r.get(job_id)
    assert row["status"] in ("queued", "running") and row["requested_by"] == "key:abc12345" and row["cell_id"] == ENABLED
    assert row["args"] == {"n": 3, "budget_usd": 0.2} and row["progress"][0]["event"] == "queued"
    assert gate.started.wait(5.0)
    running = r.get(job_id)
    assert running["status"] == "running" and running["started_at"] and running["finished_at"] is None
    assert [e["event"] for e in running["progress"]] == ["queued", "started", "stage:one"]
    gate.go.set()
    done = r.wait(job_id)
    assert done["status"] == "done" and done["result"] == {"n": 3, "cost_usd": 0.1} and done["finished_at"]
    assert [e["event"] for e in done["progress"]] == ["queued", "started", "stage:one", "stage:two", "done"]
    assert r.for_cell(ENABLED)[0]["job_id"] == job_id and r.for_cell("0000_0000") == []
    assert r.committed_usd() == pytest.approx(0.1), "a finished job counts what it spent, not what it reserved"
    con = connect(db)
    try:
        stored = con.execute("select status, requested_by, progress_json, result_json from agent.job where job_id = ?",
                             [job_id]).fetchone()
        assert stored[0] == "done" and stored[1] == "key:abc12345" and json.loads(stored[3])["n"] == 3
        assert tier_audit(con) == []
    finally:
        con.close()


def test_a_failing_job_records_the_error_and_a_bad_submission_is_refused_before_queueing(db: Path) -> None:
    gate = Gate()
    gate.go.set()
    r = make_runner(db, gate)
    job_id = r.submit("fake", ENABLED, {"n": -1}, requested_by="local")
    failed = r.wait(job_id)
    assert failed["status"] == "failed" and failed["error"] == "ValueError: n must not be negative"
    assert failed["result"] is None and failed["progress"][-1]["event"] == "failed"
    with pytest.raises(J.JobRefused, match="bad is not allowed"):
        r.submit("fake", ENABLED, {"bad": True}, requested_by="local")
    with pytest.raises(J.JobRefused, match="no job kind"):
        r.submit("nope", ENABLED, {}, requested_by="local")
    with pytest.raises(J.JobRefused, match="looks like"):
        r.submit("fake", "not-a-cell", {}, requested_by="local")
    with pytest.raises(J.JobNotFound):
        r.get("no-such-job")
    assert len(r.for_cell(ENABLED)) == 1, "a refused submission wrote no row"


def test_cancel_stops_a_queued_job_before_it_starts_and_a_running_one_at_its_next_check(db: Path) -> None:
    gate = Gate()
    r = make_runner(db, gate)
    first = r.submit("fake", ENABLED, {"n": 1}, requested_by="local")
    second = r.submit("fake", ENABLED, {"n": 2}, requested_by="local")   # one worker: this one waits its turn
    assert gate.started.wait(5.0)
    assert r.get(second)["status"] == "queued"
    cancelled = r.cancel(second)
    assert cancelled["status"] == "cancelled" and cancelled["error"] == "cancelled before it started"
    asked = r.cancel(first)
    assert asked["status"] == "running" and asked["progress"][-1]["event"] == "cancel_requested"
    gate.go.set()
    stopped = r.wait(first)
    assert stopped["status"] == "cancelled" and stopped["result"] is None
    assert r.wait(second)["status"] == "cancelled" and len(gate.seen) == 1, "the queued one never ran"
    assert r.cancel(first)["status"] == "cancelled", "cancelling a finished job changes nothing"


def test_the_session_budget_refuses_a_job_the_process_cannot_afford(db: Path) -> None:
    gate = Gate()
    r = make_runner(db, gate, session_budget_usd=1.0)
    a = r.submit("fake", ENABLED, {"budget_usd": 0.6}, requested_by="local")
    with pytest.raises(J.JobRefused, match=r"session budget of \$1.00 .* leaves \$0.40; this job asks for \$0.50"):
        r.submit("fake", ENABLED, {"budget_usd": 0.5}, requested_by="local")
    gate.go.set()
    r.wait(a)
    assert r.committed_usd() == pytest.approx(0.3), "what it reserved is released; what it cost stays"
    b = r.submit("fake", ENABLED, {"budget_usd": 0.5}, requested_by="local")
    assert r.wait(b)["status"] == "done"


def test_a_restart_marks_what_was_running_or_queued_as_failed_with_the_reason(db: Path) -> None:
    gate = Gate()
    r = make_runner(db, gate)
    running = r.submit("fake", ENABLED, {"n": 1}, requested_by="local")
    queued = r.submit("fake", ENABLED, {"n": 2}, requested_by="local")
    assert gate.started.wait(5.0)
    r.close()
    # the process restarted: a new runner over the same store, before the old worker is let go
    again = make_runner(db, Gate())
    assert again.recovered == 2
    assert again.get(running)["status"] == "failed" and again.get(running)["error"] == J.RESTARTED
    assert again.get(queued)["status"] == "failed" and again.get(queued)["error"].startswith(J.RESTARTED)
    gate.go.set()
    r.wait(running)  # the old worker finishing late cannot overwrite the word the new process gave
    assert again.get(running)["status"] == "failed" and again.get(running)["finished_at"]


def test_the_module_hook_goes_through_the_installed_runner(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = Gate()
    gate.go.set()
    r = make_runner(db, gate, kinds={"analyst": J.JobKind("analyst", "admin", gate.run, gate.prepare)})
    J.install(r)
    job_id = J.submit_analyst(ENABLED, reason="a look", expert_ids=["e:1"], budget_usd=0.25, requested_by="key:x")
    assert J.get(job_id)["requested_by"] == "key:x" and J.get(job_id)["job_id"] == job_id
    assert r.wait(job_id)["status"] == "done"
    with pytest.raises(J.JobNotFound):
        J.get("missing")
    monkeypatch.setattr(J, "_default", None)


# ---------------------------------------------------------------- the analyst kind on the fake world


def dashboard_factory(world: LoopWorld):
    def factory(cell_id: str, arm, stage: Path, shared):
        return Session.open(WORLD_CELL, "dashboard", fold=None, switches=arm.switches, stage=stage,
                            tools=loop_registry(world))
    return factory


@pytest.fixture
def analyst_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A runner whose only kind is the analyst over the fake loop world, on a temporary store in the serving
    process's one-connection mode (the chain run writes while the row is rewritten)."""
    rt = install_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("LR_STORE_RW", "1")
    db = tmp_path / "chains.duckdb"
    connect(db).close()
    # a cent a call: the default arm's per-call ceiling ($0.30) plus thirteen calls fit the default budget
    backend = LoopBackend(cost=0.01)
    kind = J.analyst_kind(db_path=db, enabled=lambda: {ENABLED}, backend_factory=lambda _arm: backend,
                          session_factory=dashboard_factory(LoopWorld()), cards=fake_cards, cache_root=rt.cache)
    r = J.Runner(db, kinds={"analyst": kind}, workers=1, session_budget_usd=5.0)
    yield r, db, rt, backend
    r.close()


def test_the_analyst_kind_refuses_any_cell_but_an_enabled_one_and_an_out_of_range_budget(analyst_runner) -> None:
    r, _db, _rt, _backend = analyst_runner
    with pytest.raises(J.JobRefused, match=r"only on the enabled cells \(PRD §9.4\), and 0000_0001 is not one"):
        r.submit("analyst", "0000_0001", {}, requested_by="local")
    with pytest.raises(J.JobRefused, match="at most 2.00"):
        r.submit("analyst", ENABLED, {"budget_usd": 2.5}, requested_by="local")
    with pytest.raises(J.JobRefused, match="over 0"):
        r.submit("analyst", ENABLED, {"budget_usd": 0}, requested_by="local")
    with pytest.raises(J.JobRefused, match="not a staged"):
        r.submit("analyst", ENABLED, {"arm": "v0"}, requested_by="local")
    with pytest.raises(J.JobRefused, match="no usable arm"):
        r.submit("analyst", ENABLED, {"arm": "no-such-arm"}, requested_by="local")
    with pytest.raises(J.JobRefused, match=r"under arm 'v1'\'s per-call ceiling of \$1.50"):
        r.submit("analyst", ENABLED, {"arm": "v1", "budget_usd": 1.0}, requested_by="local")
    with pytest.raises(J.JobRefused, match="one cell"):
        r.submit("analyst", None, {}, requested_by="local")
    assert r.for_cell(ENABLED) == []


def test_an_analyst_job_publishes_the_chain_the_way_lr_arm_chain_does_and_reports_it(analyst_runner) -> None:
    r, db, rt, backend = analyst_runner
    job_id = r.submit("analyst", ENABLED, {"reason": "the scores disagree", "expert_ids": ["e:abc"]},
                      requested_by="key:deadbeef")
    row = r.get(job_id)
    assert row["args"] == {"arm": J.DEFAULT_ARM, "backend": "auto", "budget_usd": J.DEFAULT_BUDGET_USD,
                           "reason": "the scores disagree", "expert_ids": ["e:abc"]}
    done = r.wait(job_id, timeout=120.0)
    assert done["status"] == "done", done["error"]
    res = done["result"]
    assert res["chain_id"] == f"{done['run_id']}:{ENABLED}" and res["verdict"] == "supports_closer_look"
    assert res["published"] is True and res["problems"] == [] and res["cost_usd"] > 0 and res["arm"] == J.DEFAULT_ARM
    assert res["reason"] == "the scores disagree" and res["expert_ids"] == ["e:abc"]
    events = [e["event"] for e in done["progress"]]
    assert events[:2] == ["queued", "started"] and "run" in events and events[-1] == "done"
    stages = [e for e in events if e.startswith("stage:")]
    assert stages[0] == "stage:plan" and stages[-1] == "stage:publish" and "stage:execute" in stages \
        and "stage:verify" in stages and "stage:decide" in stages, stages
    assert "stage:gate" not in stages, "a gate attempt is not a stage a reader waits for"
    assert any(e["event"] == "log" and f"chains: arm {J.DEFAULT_ARM}" in e["text"] for e in done["progress"])
    # the chain is in the agent tier under the job's run, exactly as `lr arm chain` would have put it
    stored = CH.chains_for_cell(connect(db, read_only=True), WORLD_CELL)
    assert [c["chain_id"] for c in stored] == [res["chain_id"]] and stored[0]["published"] is True
    assert stored[0]["arm"] == J.DEFAULT_ARM and stored[0]["purpose"] == "dashboard"
    assert (rt.runs / done["run_id"] / "spans.jsonl").is_file() and (rt.runs / done["run_id"] / "cells.jsonl").is_file()
    assert r.committed_usd() == pytest.approx(res["cost_usd"])


def test_cancelling_a_running_analyst_job_stops_it_at_the_next_model_call(analyst_runner) -> None:
    r, db, _rt, backend = analyst_runner
    first_call = threading.Event()
    release = threading.Event()
    inner = backend.call

    def slow(req):
        first_call.set()
        release.wait(10.0)
        return inner(req)

    backend.call = slow  # type: ignore[method-assign]
    job_id = r.submit("analyst", ENABLED, {}, requested_by="local")
    assert first_call.wait(60.0)
    assert r.cancel(job_id)["progress"][-1]["event"] == "cancel_requested"
    release.set()
    stopped = r.wait(job_id, timeout=120.0)
    assert stopped["status"] == "cancelled" and "next model call" in stopped["error"] and stopped["run_id"]
    assert CH.chains_for_cell(connect(db, read_only=True), WORLD_CELL) == [], "no chain was published"


def test_the_registered_analyst_kind_needs_the_admin_role_and_the_default_arm_is_the_openrouter_stack() -> None:
    kind = J.KINDS["analyst"]
    assert kind.role == "admin" and J.DEFAULT_ARM == "v1-openrouter" and J.DEFAULT_BACKEND == "auto"
    assert A.load_arm(J.DEFAULT_ARM).agent == "v1"
    assert ENABLED in J.enabled_cells(), "the enabled set is read from knowledge/enabled_cells.toml"
    assert replace(A.load_arm("v1"), name="x").name == "x"


# ---------------------------------------------------------------- migration 0006

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0006_agent_job.py"


def _job_columns(sql: str) -> list[tuple[str, str]]:
    """(column, first type word) of `agent.job` from a create-table body, the way the chain migration test reads one."""
    import re

    m = re.search(r"create table if not exists\s+agent\.job\s*\((.*?)\n\);", sql, re.S)
    assert m, "agent.job is declared"
    cols = []
    for line in m.group(1).splitlines():
        code = line.partition("--")[0].strip()
        if code and not code.startswith("primary key"):
            name, kind = code.split()[:2]
            cols.append((name, kind))
    return cols


def test_migration_0006_declares_agent_job_and_requested_by_as_schema_sql_does(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    from legacy_reader.store import SCHEMA_SQL
    from legacy_reader.store import pg as PG

    spec = importlib.util.spec_from_file_location("lr_migration_0006", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0006" and mod.down_revision == "0005"
    executed: list[str] = []
    monkeypatch.setattr(mod.op, "execute", executed.append)
    mod.upgrade()
    sql = "\n".join(executed)
    assert _job_columns(sql) == _job_columns(PG.postgres_ddl(SCHEMA_SQL.read_text()))
    assert [c for c, _ in _job_columns(sql)] == [*J.COLUMNS, "tier"], "the runner's columns are the table's, in order"
    assert "job_cell_idx" in sql
    assert "alter table agent.conversation add column if not exists requested_by text" in sql
    assert "alter table agent.conversation_turn add column if not exists requested_by text" in sql
    mod.downgrade()
    assert "drop table if exists agent.job" in executed[-1]

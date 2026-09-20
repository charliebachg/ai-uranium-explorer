"""The interface agent (PRD §8.3): the router's kinds and their plans, the three actions, and the gate.

A scripted backend answers by task (`interface_route`, `interface_answer`, `prospect_chat`), and the tools are
a fake world, so nothing here calls a model or reads the real store. An insight lands in a temporary DuckDB
with the real schema; the analyst job runner is stubbed at the two names the agent imports."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from legacy_reader.interface import actions as A
from legacy_reader.interface import agent as G
from legacy_reader.interface import router as R
from legacy_reader.interface.conversation import Conversation
from legacy_reader.prospect import tools as T
from legacy_reader.store import connect
from legacy_reader.values import stat

CELL = "0201_0072"          # enabled
OTHER = "0001_0001"         # not enabled
CONDUCTOR = f"c:cell:{CELL}:d_conductor_m"


class Scripted:
    """Replies by task, each a queue; a task with nothing queued replies with the last thing it was given."""

    def __init__(self, **by_task: list[dict[str, Any]]) -> None:
        self.queues = {k: list(v) for k, v in by_task.items()}
        self.requests: list[Any] = []

    def call(self, req: Any) -> Any:
        self.requests.append(req)
        q = self.queues.setdefault(req.task, [{"kind": "other"}] if req.task == "interface_route" else [])
        if not q:
            raise AssertionError(f"no scripted reply for task {req.task}")
        step = q.pop(0) if len(q) > 1 else q[0]

        class Reply:
            structured = step
            cost_usd = 0.001

        return Reply()


def route(kind: str, **more: Any) -> dict[str, Any]:
    return {"kind": kind, **more}


def answer(text: str, claims: list[dict[str, Any]] | None = None, **more: Any) -> dict[str, Any]:
    return {"action": "answer", "answer": {"text": text, "claims": claims or [], **more}}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Every tool returns one conductor distance under the cell's id; `criteria_breakdown` returns a small
    criteria table so the sensitivity has something to rank. The calls are recorded."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake(tool: str, args: dict[str, Any]) -> T.ToolResult:
        calls.append((tool, dict(args)))
        cell = args.get("cell_id") or CELL
        if tool == "criteria_breakdown":
            return T.ToolResult(tool, args, rows=[
                {"criterion": "conductor", "weight": 2.0, "membership": 1.0, "state": "met", "status": "published"},
                {"criterion": "fault", "weight": 1.0, "membership": None, "state": "unknown", "status": "published"},
                {"criterion": "host", "weight": 1.0, "membership": 0.0, "state": "not met", "status": "published"},
            ], values={f"c:crit:{cell}:conductor": stat(f"c:crit:{cell}:conductor", 1.0, fmt="ratio3")})
        vid = f"c:cell:{cell}:d_conductor_m"
        return T.ToolResult(tool, args, rows=[{"feature": "d_conductor_m", "value": 820.0, "value_id": vid}],
                            values={vid: stat(vid, 820.0, fmt="m1", unit="m")})

    monkeypatch.setattr(T, "call", fake)
    return {"calls": calls}


def conversation(tmp_path: Path, cell: str = CELL) -> Conversation:
    return Conversation(cell_id=cell, insight_store=partial(connect, tmp_path / "insight.duckdb", False))


# ---------------------------------------------------------------- the router and its plans


def test_every_kind_has_a_deterministic_plan_and_nothing_else_is_a_kind() -> None:
    assert R.KINDS == ("lookup", "compare", "explain_score", "what_is_unknown", "what_would_change",
                       "record_insight", "run_analyst", "other")
    q = "how far is the conductor?"
    by_kind = {
        "lookup": [("cell_features", {"cell_id": CELL})],
        "explain_score": [("cell_scores", {"cell_id": CELL}), ("criteria_breakdown", {"cell_id": CELL})],
        "what_is_unknown": [("criteria_breakdown", {"cell_id": CELL}), ("coverage", {"feature_key": None})],
        "what_would_change": [("sensitivity", {"cell_id": CELL})],
        "record_insight": [], "run_analyst": [], "other": [],
    }
    for kind, expected in by_kind.items():
        steps = R.plan(R.parse(route(kind), q, CELL), q)
        assert [(s.tool, s.args) for s in steps] == expected, kind
    # the same route and question give the same steps, twice
    r = R.parse(route("compare"), f"compare with {OTHER}", CELL)
    assert R.plan(r, "x") == R.plan(r, "x")
    assert [(s.tool, s.args["cell_id"]) for s in R.plan(r, "x")] == [
        ("cell_scores", CELL), ("criteria_breakdown", CELL), ("cell_scores", OTHER), ("criteria_breakdown", OTHER)]


def test_a_lookup_goes_to_the_tool_that_holds_the_topic() -> None:
    q = "what do the reports say about the conductor?"
    assert R.plan(R.parse(route("lookup", topic="passages"), q, CELL), q)[0].tool == "retrieve"
    assert R.plan(R.parse(route("lookup", topic="labels"), q, CELL), q)[0].tool == "label_context"
    assert R.plan(R.parse(route("lookup", topic="scores"), q, CELL), q)[0].tool == "cell_scores"
    near = R.plan(R.parse(route("lookup", topic="nearby", entities=["em_conductors"]), q, CELL), q)[0]
    assert near.tool == "nearby" and near.args["layer"] == "em_conductors"
    # a nearby question that names no layer reads the handbook's two pairs instead of guessing a layer
    assert R.plan(R.parse(route("lookup", topic="nearby"), q, CELL), q)[0].tool == "crosscheck"
    cov = R.plan(R.parse(route("lookup", topic="coverage", entities=["sed_u_max_ppm"]), q, CELL), q)[0]
    assert cov.args == {"feature_key": "sed_u_max_ppm"}


def test_cell_ids_come_from_the_question_not_the_model() -> None:
    r = R.parse(route("compare", cell_ids=["9999_9999"]), f"how does {OTHER} compare?", CELL)
    assert r.cell_ids == [CELL, OTHER, "9999_9999"], "the conversation's cell first, then the question's, then the model's"
    assert R.parse(route("bogus"), "q", CELL).kind == "other"
    assert R.parse({}, "q", CELL).fallback, "an unreadable reply is routed as other and says so"


def test_a_routed_turn_runs_the_plan_then_one_answer_call(tmp_path: Path, world: dict) -> None:
    conv = conversation(tmp_path)
    backend = Scripted(
        interface_route=[route("explain_score", reason="asks why the score is what it is")],
        interface_answer=[answer("The conductor sits 820.0 m away.",
                                 [{"text": "820.0 m to the conductor", "value_ids": [CONDUCTOR]}])],
    )
    events: list[dict[str, Any]] = []
    turn = G.ask(conv, "why is the score what it is?", backend, model="fake", on_event=events.append)
    assert turn["published"] is True and "820.0" in turn["text"]
    assert turn["route"]["kind"] == "explain_score"
    assert [s["tool"] for s in turn["route"]["plan"]] == ["cell_scores", "criteria_breakdown"]
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["opened", "route"] and "checking" in kinds
    # the plan's two reads were staged by the opening already: reused, not fetched twice
    assert [e for e in events if e["type"] == "tool" and e.get("reused")], "an already-staged read is reused"
    assert [r.task for r in backend.requests] == ["interface_route", "interface_answer"]
    assert backend.requests[0].stage_files == (), "the router is sent no evidence files"
    assert backend.requests[1].stage_files, "the answer call is sent the staged evidence"


def test_an_unrouted_question_falls_back_to_the_tool_loop(tmp_path: Path, world: dict) -> None:
    conv = conversation(tmp_path)
    backend = Scripted(
        interface_route=[route("other")],
        prospect_chat=[{"action": "call_tool", "tool": "retrieve", "args": {"query": "conductor", "cell_id": CELL}},
                       answer("Nothing numeric here.")],
    )
    turn = G.ask(conv, "tell me something", backend, model="fake")
    assert turn["published"] is True and turn["route"]["kind"] == "other"
    assert "retrieve" in turn["tools_used"]
    assert [r.task for r in backend.requests] == ["interface_route", "prospect_chat", "prospect_chat"]


def test_a_backend_that_cannot_route_still_answers(tmp_path: Path, world: dict) -> None:
    """The API's old fake replies with an answer to every task; that lands in the loop and is answered."""
    conv = conversation(tmp_path)
    backend = Scripted(interface_route=[answer("x")], prospect_chat=[answer("The record is thin here.")])
    turn = G.ask(conv, "well?", backend, model="fake")
    assert turn["published"] is True and turn["route"]["kind"] == "other" and turn["route"]["fallback"]


# ---------------------------------------------------------------- abstain


def test_an_out_of_scope_question_goes_straight_to_abstain(tmp_path: Path, world: dict) -> None:
    conv = conversation(tmp_path)
    backend = Scripted(interface_route=[route("lookup", out_of_scope=True, detail="asks for a grade")])
    events: list[dict[str, Any]] = []
    turn = G.ask(conv, "what grade was intersected?", backend, model="fake", on_event=events.append)
    assert [r.task for r in backend.requests] == ["interface_route"], "no answer call, no tools"
    assert turn["published"] is True and turn["cannot_answer"] is True
    assert turn["abstention"]["reason"] == "out_of_scope" and turn["abstention"]["abstain_id"].startswith("a:")
    assert turn["abstention"]["detail"] == "asks for a grade"
    assert conv.abstentions[0]["abstain_id"] == turn["abstention"]["abstain_id"], "recorded on the conversation"
    assert "abstain" in turn["tools_used"], "the abstention is listed like a tool call, so the persisted turn names it"
    assert [e for e in events if e["type"] == "abstain"][0]["reason"] == "out_of_scope"


def test_the_model_may_abstain_from_the_answer_call_and_its_detail_is_gated(tmp_path: Path, world: dict) -> None:
    conv = conversation(tmp_path)
    backend = Scripted(
        interface_route=[route("lookup", topic="features")],
        interface_answer=[{"action": "abstain", "abstain": {"reason": "not_measured",
                                                             "detail": "no depth here; the nearest is 4300 m away"}}],
    )
    turn = G.ask(conv, "how deep is the unconformity?", backend, model="fake")
    assert turn["abstention"]["reason"] == "not_measured"
    assert turn["abstention"]["detail"] == "", "a number the model wrote into its excuse is dropped with the excuse"
    assert turn["text"].startswith("No answer: nobody measured that here")


def test_a_compare_without_a_second_cell_is_refused_with_no_value(tmp_path: Path, world: dict) -> None:
    conv = conversation(tmp_path)
    turn = G.ask(conv, "compare this with the camp", Scripted(interface_route=[route("compare")]), model="fake")
    assert turn["abstention"]["reason"] == "no_value"


# ---------------------------------------------------------------- the gate


def test_the_gate_refuses_an_unbacked_number_then_retries_once(tmp_path: Path, world: dict) -> None:
    conv = conversation(tmp_path)
    backend = Scripted(
        interface_route=[route("lookup", topic="features")],
        interface_answer=[answer("The conductor is 820.0 m away and the basement lies at 512.7 m."),
                          answer("The conductor is 820.0 m away.",
                                 [{"text": "820.0 m", "value_ids": [CONDUCTOR]}])],
    )
    events: list[dict[str, Any]] = []
    turn = G.ask(conv, "how far?", backend, model="fake", on_event=events.append)
    assert turn["published"] is True and turn["retried"] is True
    refused = [e for e in events if e["type"] == "refused"]
    assert refused and any("512.7" in p for p in refused[0]["problems"])
    assert "512.7" in backend.requests[-1].user_prompt, "the retry carries the gate's objection"
    assert backend.requests[-1].context_hash != backend.requests[-2].context_hash, "the retry is never a cache hit"


def test_a_reply_with_no_answer_object_is_asked_again_not_published_empty(tmp_path: Path, world: dict) -> None:
    """Seen on the smoke run: the cheap model wrote its whole answer into `reasoning` and sent no `answer`."""
    conv = conversation(tmp_path)
    backend = Scripted(
        interface_route=[route("explain_score")],
        interface_answer=[{"action": "answer", "reasoning": "The conductor is 820.0 m away, which is close."},
                          answer("The conductor is 820.0 m away.", [{"text": "820.0 m", "value_ids": [CONDUCTOR]}])],
    )
    turn = G.ask(conv, "why?", backend, model="fake")
    assert turn["published"] is True and turn["retried"] is True and "820.0" in turn["text"]
    assert G.NO_ANSWER in backend.requests[-1].user_prompt
    assert "reasoning" not in G.ANSWER_SCHEMA["properties"], "the answer call has no field to write the answer into by mistake"
    empty = Scripted(interface_route=[route("explain_score")],
                     interface_answer=[{"action": "answer", "reasoning": "x"}, {"action": "answer", "reasoning": "y"}])
    turn2 = G.ask(conversation(tmp_path), "why?", empty, model="fake")
    assert turn2["published"] is False and turn2["problems"] == [G.NO_ANSWER]


def test_a_second_refusal_withholds_the_answer(tmp_path: Path, world: dict) -> None:
    conv = conversation(tmp_path)
    bad = answer("Grades of 4.7% were intersected.")
    backend = Scripted(interface_route=[route("lookup", topic="features")], interface_answer=[bad, bad])
    turn = G.ask(conv, "any grades?", backend, model="fake")
    assert turn["published"] is False and turn["text"] is None
    assert any("4.7" in p for p in turn["problems"])


# ---------------------------------------------------------------- record_insight


def test_an_insight_is_written_to_the_expert_tier_and_citable_afterwards(tmp_path: Path, world: dict) -> None:
    conv = conversation(tmp_path)
    statement = "the conductor probably continues north-east of hole HR-014 for another 600 m"
    backend = Scripted(interface_route=[route("record_insight", insight_text=statement)])
    events: list[dict[str, Any]] = []
    turn = G.ask(conv, f"record this: {statement}", backend, model="fake", on_event=events.append)
    assert turn["published"] is True and turn["insight"]["expert_id"].startswith("e:")
    assert turn["insight"]["author"] == "local", "author defaults to the caller's label, local"
    eid = turn["insight"]["expert_id"]
    minted = turn["insight"]["value_ids"]
    assert minted and all(v.startswith(f"c:insight:{CELL}:") for v in minted)
    assert set(minted) <= conv.expert_ids and set(minted) <= set(conv.values)
    assert "record_insight" in turn["tools_used"]
    assert [e for e in events if e["type"] == "insight"][0]["expert_id"] == eid

    con = connect(tmp_path / "insight.duckdb", read_only=True)
    try:
        row = con.execute("select cell_id, author, text, principal, tier from expert.insight where expert_id = ?",
                          [eid]).fetchone()
    finally:
        con.close()
    assert row == (CELL, "local", statement, "local", "expert")

    # a later answer may cite the expert value, and the claim is labelled for it (B19)
    backend = Scripted(
        interface_route=[route("lookup", topic="features")],
        interface_answer=[answer("You said it runs another 600 m.", [{"text": "600 m, your statement",
                                                                       "value_ids": [minted[0]]}])],
    )
    turn2 = G.ask(conv, "what did I say?", backend, model="fake")
    assert turn2["published"] is True and turn2["claims"][0]["expert"] is True
    assert turn2["expert_ids"] == [minted[0]]


def test_an_insight_with_no_writable_store_is_refused_not_lost(tmp_path: Path, world: dict) -> None:
    conv = Conversation(cell_id=CELL, insight_store=None)
    turn = G.ask(conv, "record: nothing", Scripted(interface_route=[route("record_insight")]), model="fake")
    assert turn["published"] is False and "no writable store" in turn["problems"][0]


def test_the_author_is_the_callers_label(tmp_path: Path, world: dict) -> None:
    conv = Conversation(cell_id=CELL, requested_by="key:ab12cd34",
                        insight_store=partial(connect, tmp_path / "i.duckdb", False))
    rec = A.record_insight(conv, "the fault is older than the conductor")
    assert rec["author"] == "key:ab12cd34"


# ---------------------------------------------------------------- invoke the analyst


def test_invoking_on_a_cell_that_is_not_enabled_is_refused_with_the_reason(tmp_path: Path, world: dict,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from legacy_reader.api import jobs as J

    submitted: list[Any] = []
    monkeypatch.setattr(J, "submit_analyst", lambda *a, **k: submitted.append((a, k)) or "j1")
    conv = conversation(tmp_path, cell=OTHER)
    turn = G.ask(conv, "run the analyst", Scripted(interface_route=[route("run_analyst")]), model="fake")
    assert submitted == [], "nothing was submitted"
    assert turn["job"] is None and turn["cannot_answer"] is True
    assert "enabled cells" in turn["text"] and turn["published"] is True


def test_invoking_on_an_enabled_cell_submits_with_the_experts_and_reports_when_done(
        tmp_path: Path, world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from legacy_reader.api import jobs as J

    submitted: list[Any] = []
    state = {"status": "running"}

    def submit(cell_id: str, *, reason: str, expert_ids: list[str], budget_usd: float, requested_by: str) -> str:
        submitted.append({"cell_id": cell_id, "reason": reason, "expert_ids": expert_ids, "budget_usd": budget_usd,
                          "requested_by": requested_by})
        return "job-abc"

    monkeypatch.setattr(J, "submit_analyst", submit)
    monkeypatch.setattr(J, "get", lambda job_id: {"job_id": job_id, **state})
    monkeypatch.setattr(A, "oof_score_ids", lambda cell: [f"c:score:{cell}:learned"])
    fake_diff = {"verdict": {"before": "insufficient", "after": "supports_closer_look", "changed": True},
                 "nodes": [{"node_id": "n01", "before": "unknown", "after": "met", "changed": True}], "n_changed": 1}
    monkeypatch.setattr(G.D, "assess", lambda cell, chain_id: {**fake_diff, "chain_id": chain_id})

    conv = conversation(tmp_path)
    A.record_insight(conv, "the conductor continues north-east")
    events: list[dict[str, Any]] = []
    turn = G.ask(conv, "run the analyst with that", Scripted(interface_route=[route("run_analyst")]), model="fake",
                 on_event=events.append)
    assert turn["published"] is True and turn["job"]["job_id"] == "job-abc"
    assert submitted[0]["cell_id"] == CELL and submitted[0]["requested_by"] == "local"
    assert submitted[0]["expert_ids"] == sorted(conv.expert_ids) and submitted[0]["reason"] == "run the analyst with that"
    assert turn["job"]["score_ids"] == [f"c:score:{CELL}:learned"], "the out-of-fold score ids ride on the handover"
    assert [e for e in events if e["type"] == "job"][0]["status"] == "submitted"
    assert "run_analyst" in turn["tools_used"]

    # still running: the next turn reports nothing
    turn2 = G.ask(conv, "and?", Scripted(interface_route=[route("other")], prospect_chat=[answer("Still running.")]),
                  model="fake")
    assert turn2["jobs_done"] == []

    # done: the next turn carries the verdict and the diff against the stored chain without the insight
    state.update({"status": "done", "result": {"chain_id": "run:cell", "verdict": "supports_closer_look",
                                               "cost_usd": 0.04}})
    events = []
    turn3 = G.ask(conv, "now?", Scripted(interface_route=[route("other")], prospect_chat=[answer("Done.")]),
                  model="fake", on_event=events.append)
    done = turn3["jobs_done"]
    assert len(done) == 1 and done[0]["verdict"] == "supports_closer_look"
    assert done[0]["assessment"]["verdict"]["changed"] is True and done[0]["assessment"]["n_changed"] == 1
    assert [e for e in events if e["type"] == "job"][0]["status"] == "done"
    # reported once
    turn4 = G.ask(conv, "again?", Scripted(interface_route=[route("other")], prospect_chat=[answer("Nothing new.")]),
                  model="fake")
    assert turn4["jobs_done"] == []


def test_a_session_budget_bounds_the_invocations(tmp_path: Path, world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from legacy_reader.api import jobs as J

    monkeypatch.setattr(J, "submit_analyst", lambda *a, **k: "j")
    monkeypatch.setattr(A, "oof_score_ids", lambda cell: [])
    monkeypatch.setenv(A.SESSION_BUDGET_VAR, "0.60")
    monkeypatch.setenv(A.JOB_BUDGET_VAR, "0.50")
    conv = conversation(tmp_path)
    assert "job_id" in A.invoke_analyst(conv, "first")
    second = A.invoke_analyst(conv, "second")
    assert "error" in second and "per-session analyst budget" in second["error"]


def test_the_runners_own_refusal_is_the_reason(tmp_path: Path, world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from legacy_reader.api import jobs as J

    def refuse(*a: Any, **k: Any) -> str:
        raise J.JobRefused("the session budget leaves nothing")

    monkeypatch.setattr(J, "submit_analyst", refuse)
    rec = A.invoke_analyst(conversation(tmp_path), "go")
    assert rec["error"] == "the session budget leaves nothing"


# ---------------------------------------------------------------- the shim


def test_prospect_chat_is_a_shim_over_the_agent(tmp_path: Path, world: dict) -> None:
    from legacy_reader.prospect import chat as C

    assert C.Conversation is Conversation and C.MAX_STEPS == 5
    conv = C.Conversation(cell_id=CELL)
    turn = C.ask(conv, "q", Scripted(interface_route=[route("other")], prospect_chat=[answer("Plain.")]))
    assert turn["published"] is True and turn["route"]["kind"] == "other"
    assert json.dumps(turn), "the turn is JSON, as the API streams it"

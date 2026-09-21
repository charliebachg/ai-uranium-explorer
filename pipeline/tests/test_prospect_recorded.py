"""The recorded session the walkthrough replays, and the switch that picks which backend answers.

A recording is shown to an audience, so the things worth pinning are that it keeps what the gate refused
(a withheld answer is the most informative thing in the demo, not an embarrassment to drop), that it carries
the interface agent's own record of each turn (the route, a refusal with its reason, the analyst job with its
progress and result, the diff the follow-up reports), that it never writes an insight, and that it carries
only the values its published claims cite, since anything else is dead weight in a file the browser loads.

A scripted backend answers the router and the loop; the tools are stubbed; the job runner is stubbed at the
two names the agent imports, `submit_analyst` and `get`, so the job turn runs without a model or a store."""

from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest

from uranium_explorer.interface import actions as A
from uranium_explorer.interface import agent as G
from uranium_explorer.prospect import recorded as R
from uranium_explorer.prospect import serve as S
from uranium_explorer.prospect import tools as T

CELL = "0201_0072"          # enabled: the analyst may be invoked on it
OTHER = "0001_0001"         # not enabled


def val(vid: str, value, fmt: str = "m1", unit: str | None = None) -> dict:
    return {"id": vid, "kind": "stat", "as_printed": None, "value": value, "unit_as_printed": None,
            "fmt": fmt, "unit": unit}


VALUES = {
    "c:cell:x:d_conductor_m": val("c:cell:x:d_conductor_m", 820.0, unit="m"),
    "c:cell:x:holes_n": val("c:cell:x:holes_n", 493, fmt="int"),
}


class FakeBackend:
    """Answers each call with the next scripted step, so a recording can be made without a model. Every turn
    is routed first; the router's call takes the next scripted route, or `other` when none was scripted, so
    the loop's steps stay the loop's, as they were before the interface agent existed."""

    def __init__(self, steps, routes=None):
        self.steps = list(steps)
        self.routes = list(routes or [])
        self.tasks: list[str] = []

    def call(self, req):
        self.tasks.append(req.task)
        if req.task == "interface_route":
            step = self.routes.pop(0) if self.routes else {"kind": "other"}
        else:
            step = self.steps.pop(0)

        class Response:
            structured = step
            cost_usd = 0.011

        return Response()


@pytest.fixture
def stub_tools(monkeypatch):
    def fake(tool, args):
        return T.ToolResult(tool, args, rows=[{"feature": "d_conductor_m", "value": 820.0}], values=VALUES)

    monkeypatch.setattr(T, "call", fake)
    # the cell's centre comes from the store; a test never opens it
    monkeypatch.setattr(R, "_centre", lambda cell: (-103.7, 58.2))


def answer(text: str, claims: list[dict]) -> dict:
    return {"action": "answer", "answer": {"text": text, "claims": claims}}


def good() -> dict:
    return answer(
        "The nearest mapped conductor is 820.0 m away.",
        [{"text": "The nearest mapped conductor is 820.0 m away.",
          "value_ids": ["c:cell:x:d_conductor_m"]}],
    )


def fabricated() -> dict:
    return answer(
        "The nearest mapped conductor is 512.7 m away.",
        [{"text": "The nearest mapped conductor is 512.7 m away.",
          "value_ids": ["c:cell:x:d_conductor_m"]}],
    )


# ---------------------------------------------------------------- the recording


def test_a_recording_keeps_the_answer_the_gate_refused(stub_tools) -> None:
    """The withheld turn is the point of showing this at all: it is the check firing, in public. The gate
    hands the model its objections once before withholding, so a turn that stays fabricated takes two
    scripted replies."""
    payload = R.record(OTHER, FakeBackend([fabricated(), fabricated(), good()]),
                       questions=("how far?", "how far again?"), follow_up=None)
    assert [t["published"] for t in payload["turns"]] == [False, True]
    withheld = payload["turns"][0]
    assert withheld["text"] is None, "a refused answer is never carried in the file"
    assert any("512.7" in p for p in withheld["problems"]), "the objection travels with it"
    assert withheld["retried"] is True, "the file says the gate gave the model a second ask"


def test_every_turn_carries_the_route_it_took(stub_tools) -> None:
    payload = R.record(OTHER, FakeBackend([good()]), questions=("how far?",), follow_up=None)
    turn = payload["turns"][0]
    assert turn["route"]["kind"] == "other" and turn["route"]["plan"] == []
    assert turn["abstention"] is None and turn["insight"] is None and turn["job"] is None
    assert turn["jobs_done"] == [] and turn["expert_ids"] == []


def test_a_refused_question_is_kept_with_its_reason(stub_tools) -> None:
    """The question the system must refuse is asked on purpose; the file carries the refusal, its reason and
    its id rather than an answer that never came."""
    backend = FakeBackend([], routes=[{"kind": "lookup", "out_of_scope": True, "detail": "asks for a grade"}])
    payload = R.record(OTHER, backend, questions=("what grade would a hole here intersect?",), follow_up=None)
    turn = payload["turns"][0]
    assert backend.tasks == ["interface_route"], "an out-of-scope question makes no answer call"
    assert turn["route"]["out_of_scope"] is True
    assert turn["abstention"]["reason"] == "out_of_scope" and turn["abstention"]["abstain_id"].startswith("a:")
    assert turn["published"] is True and turn["cannot_answer"] is True
    assert "abstain" in turn["tools_used"]


def test_a_recording_never_writes_an_insight(stub_tools) -> None:
    """A recording asks, it never testifies: the conversation has no insight store, so the router's
    `record_insight` is refused and nothing reaches the expert tier."""
    statement = "the conductor probably continues north-east for another 600 m"
    backend = FakeBackend([], routes=[{"kind": "record_insight", "insight_text": statement}])
    payload = R.record(OTHER, backend, questions=(f"record this: {statement}",), follow_up=None)
    turn = payload["turns"][0]
    assert turn["published"] is False and turn["insight"] is None
    assert "no writable store" in turn["problems"][0]


def test_only_the_values_the_published_claims_cite_are_written(stub_tools) -> None:
    payload = R.record(OTHER, FakeBackend([good()]), questions=("how far?",), follow_up=None)
    assert set(payload["values"]) == {"c:cell:x:d_conductor_m"}
    assert "c:cell:x:holes_n" not in payload["values"], "a value nothing cites is dead weight in the browser"


def test_the_recording_says_when_and_by_what_it_was_made(stub_tools) -> None:
    payload = R.record(OTHER, FakeBackend([good()]), model="gpt-5-mini", effort="low", questions=("how far?",),
                       follow_up=None)
    assert payload["model"] == "gpt-5-mini" and payload["effort"] == "low"
    assert payload["cell_id"] == OTHER and payload["lon"] == -103.7 and payload["lat"] == 58.2
    assert payload["recorded_at"].endswith("+00:00") or "T" in payload["recorded_at"]
    assert payload["cost_usd"] > 0
    assert payload["job"] is None, "no analyst was asked for, so there is no job to carry"


def test_it_is_written_as_json_the_web_contract_can_read(stub_tools, tmp_path) -> None:
    payload = R.record(OTHER, FakeBackend([good()]), questions=("how far?",), follow_up=None)
    path = R.write(payload, out=tmp_path / "recorded_chat.json")
    back = json.loads(path.read_text())
    assert back["turns"][0]["claims"][0]["value_ids"] == ["c:cell:x:d_conductor_m"]
    assert back["turns"][0]["route"]["kind"] == "other"


# ---------------------------------------------------------------- the analyst job


@pytest.fixture
def stub_runner(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The job runner at the two names the agent imports. The row is queued when submitted, running with a
    stage on the first poll, and done with a result on the second, so the wait sees each state once."""
    from uranium_explorer.api import jobs as J

    submitted: list[dict[str, Any]] = []
    row: dict[str, Any] = {
        "job_id": "job-tour", "kind": "analyst", "cell_id": CELL, "status": "queued", "requested_by": "tour",
        "args": {}, "created_at": "2026-09-21T00:00:00+00:00", "started_at": None, "finished_at": None,
        "progress": [{"at": "2026-09-21T00:00:00+00:00", "event": "queued"}], "result": None, "error": None,
        "run_id": None,
    }
    polls = {"n": 0}

    def submit(cell_id: str, *, reason: str, expert_ids: list[str], budget_usd: float, requested_by: str) -> str:
        submitted.append({"cell_id": cell_id, "reason": reason, "expert_ids": expert_ids, "budget_usd": budget_usd,
                          "requested_by": requested_by})
        return row["job_id"]

    def get(job_id: str) -> dict[str, Any]:
        assert job_id == row["job_id"]
        polls["n"] += 1
        if polls["n"] == 1:
            row.update(status="running", started_at="t1", run_id="run",
                       progress=[*row["progress"], {"at": "t1", "event": "started"},
                                 {"at": "t1", "event": "stage:plan", "ms": 12}])
        elif polls["n"] == 2:
            row.update(status="done", finished_at="t2",
                       progress=[*row["progress"], {"at": "t2", "event": "stage:decide", "ms": 40},
                                 {"at": "t2", "event": "done"}],
                       result={"chain_id": f"run:{CELL}", "run_id": "run", "arm": "v1-openrouter",
                               "verdict": "insufficient", "probability": 0.8, "published": False,
                               "problems": ["the verifier refused node n02 twice"], "cost_usd": 0.04})
        return dict(row)

    monkeypatch.setattr(J, "submit_analyst", submit)
    monkeypatch.setattr(J, "get", get)
    monkeypatch.setattr(A, "oof_score_ids", lambda cell: [f"c:score:{cell}:learned"])
    diff = {"verdict": {"before": "supports_closer_look", "after": "insufficient", "changed": True},
            "nodes": [{"node_id": "n02", "criterion": "fault", "kind": "criterion", "before": "met",
                       "after": "unknown", "expert_ids": [], "changed": True}],
            "n_changed": 1, "n_leaning_on_expert": 0, "expert_ids": [], "baseline_chain_id": "stored:cell",
            "note": "node statuses and the verdict of the run beside the stored run"}
    monkeypatch.setattr(G.D, "assess", lambda cell, chain_id: {**diff, "chain_id": chain_id})
    return SimpleNamespace(submitted=submitted, row=row, polls=polls)


def test_the_analyst_turn_runs_the_job_to_the_end_and_the_follow_up_reports_it(stub_tools, stub_runner) -> None:
    backend = FakeBackend([good()], routes=[{"kind": "run_analyst"}, {"kind": "other"}])
    payload = R.record(CELL, backend, questions=("run the analyst on this cell",), follow_up="what did it decide?",
                       job_budget_usd=0.40, wait=partial(R.wait_for_job, poll_s=0.0))
    assert [t["question"] for t in payload["turns"]] == ["run the analyst on this cell", "what did it decide?"]
    first, second = payload["turns"]

    # the invocation: the handover the agent made, under the recording's own label and budget
    assert first["route"]["kind"] == "run_analyst" and first["published"] is True
    assert stub_runner.submitted == [{"cell_id": CELL, "reason": "run the analyst on this cell", "expert_ids": [],
                                      "budget_usd": 0.40, "requested_by": "tour"}]
    assert first["job"]["job_id"] == "job-tour" and first["job"]["score_ids"] == [f"c:score:{CELL}:learned"]
    # the recorded card carries the row as it ended, where the live card would have polled it
    assert first["job"]["status"] == "done"
    assert [e["event"] for e in first["job"]["progress"]] == ["queued", "started", "stage:plan", "stage:decide", "done"]
    assert first["job"]["result"]["chain_id"] == f"run:{CELL}" and first["job"]["result"]["published"] is False
    assert stub_runner.polls["n"] >= 2, "the wait polled the row until it was final"

    # the follow-up: the finished job with its verdict and the diff against the stored chain
    done = second["jobs_done"]
    assert len(done) == 1 and done[0]["verdict"] == "insufficient" and done[0]["status"] == "done"
    assert done[0]["assessment"]["chain_id"] == f"run:{CELL}" and done[0]["assessment"]["n_changed"] == 1
    assert done[0]["assessment"]["verdict"]["changed"] is True
    assert second["published"] is True and "820.0" in second["text"]

    # the file's own summary of the job, and the withheld chain's objection kept rather than dropped
    assert payload["job"]["job_id"] == "job-tour" and payload["job"]["status"] == "done"
    assert payload["job"]["result"]["problems"] == ["the verifier refused node n02 twice"]
    assert set(payload["values"]) == {"c:cell:x:d_conductor_m"}
    assert json.dumps(payload), "the whole recording is JSON"


def test_the_job_budget_is_set_for_the_session_and_put_back(stub_tools, stub_runner, monkeypatch) -> None:
    monkeypatch.setenv(A.JOB_BUDGET_VAR, "0.10")
    backend = FakeBackend([good()], routes=[{"kind": "run_analyst"}])
    R.record(CELL, backend, questions=("run it",), follow_up=None, job_budget_usd=0.35,
             wait=partial(R.wait_for_job, poll_s=0.0))
    assert stub_runner.submitted[0]["budget_usd"] == 0.35
    import os

    assert os.environ[A.JOB_BUDGET_VAR] == "0.10", "the environment is as it was once the session is over"


def test_a_cell_the_analyst_refuses_is_recorded_as_refused(stub_tools, stub_runner) -> None:
    """Not an enabled cell: the agent's refusal is the answer, no job is submitted and no follow-up is asked."""
    backend = FakeBackend([], routes=[{"kind": "run_analyst"}])
    payload = R.record(OTHER, backend, questions=("run the analyst",), follow_up="and?")
    assert len(payload["turns"]) == 1 and stub_runner.submitted == []
    turn = payload["turns"][0]
    assert turn["job"] is None and turn["cannot_answer"] is True and "enabled cells" in turn["text"]
    assert payload["job"] is None


def test_the_wait_gives_up_after_its_timeout(monkeypatch) -> None:
    from uranium_explorer.api import jobs as J

    monkeypatch.setattr(J, "get", lambda job_id: {"job_id": job_id, "status": "running", "progress": []})
    with pytest.raises(TimeoutError, match="still running"):
        R.wait_for_job("j", timeout_s=0.0, poll_s=0.0)


def test_the_latest_stage_reads_like_the_strip() -> None:
    assert R.latest_stage([{"event": "queued"}, {"event": "stage:plan"}, {"event": "stage:gate"}]) == "gate"
    assert R.latest_stage([{"event": "queued"}]) is None


# ---------------------------------------------------------------- which backend answers


def test_the_chat_backend_can_be_chosen_and_the_two_never_share_a_cache() -> None:
    openai, openai_model = S.make_backend("openai", "")
    claude, claude_model = S.make_backend("claude", "")
    assert openai_model != "claude-sonnet-5"
    assert claude_model == "claude-sonnet-5"
    # the family is what keeps one backend's answer from being served for the other
    assert openai.inner.family != claude.inner.family


def test_an_unknown_backend_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(ValueError, match="openai, claude or auto"):
        S.make_backend("gemini", "")


def test_an_explicit_model_wins_over_the_default() -> None:
    _backend, model = S.make_backend("openai", "gpt-4.1-mini")
    assert model == "gpt-4.1-mini"

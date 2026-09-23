"""A real session with the interface agent, recorded, so the walkthrough does not depend on a server being up.

The chat in the dashboard is live: every turn is routed, answered from the tools and gated before it is
shown, and an analyst it invokes runs as a job the panel polls. A guided tour cannot be live, because a demo
that stops to wait on a model call, or fails because nobody started `ue prospect serve`, is a demo that does
not get watched.

So this records one. The questions are asked for real, in one conversation on one enabled cell, through the
same `interface.agent.ask` the API runs; the analyst job is submitted through the same `api.jobs` runner the
API installs, only here the recording waits for it in-process and then asks one more question, so the
transcript carries the verdict and the diff the way a live session's next turn would. Everything the panel
draws live is kept: the route and its plan, an abstention with its reason, the job with its progress events
and its result, and the chain diff. A turn the gate withheld is kept as withheld, objections in its place.

Two things a recording never does. It never writes an insight: `record_insight` is refused for want of a
store, because an invented geologist's statement has no business in the expert tier. And it never hides a
result it did not like: a chain the verifier withheld is recorded with what the verifier objected to. The
page labels the exchange as recorded, because a replayed answer is evidence of what the agent said once, not
proof of what it would say now.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ..interface import actions as A
from ..interface import agent as G
from ..interface import default_model
from ..interface.conversation import Conversation
from ..paths import PATHS

#: The tour's questions, in order, the way a geologist asks them: what is here, why the scores are what they
#: are, what would settle it, one the system must refuse, and the analyst itself. The refusal is asked on
#: purpose: a declined question with its reason is the most informative turn in the demo.
QUESTIONS: tuple[str, ...] = (
    "What is actually measured in this cell, and what is only assumed?",
    "What did the evidence readers conclude about this cell?",
    "Why is the criteria score what it is, and is the learned score explained by the drilling history here?",
    "What grade would a hole drilled here intersect?",
    "Run the analyst on this cell and tell me what it decides.",
)
#: asked once the job has finished, so the transcript carries the verdict and the diff on a turn of its own
FOLLOW_UP = "The analyst has finished. What did it decide, and what changed against the stored chain?"

#: what an interface turn carries into the file: the API's shape, plus the route and the actions the agent adds
TURN_KEYS: tuple[str, ...] = (
    "question", "text", "claims", "caveats", "cannot_answer", "published", "problems", "tools_used", "cost_usd",
    "model", "route", "abstention", "insight", "job", "jobs_done", "expert_ids", "retried",
)
#: what the runner's row contributes to a recorded job: where it went and what it produced
ROW_KEYS: tuple[str, ...] = ("status", "progress", "result", "error", "run_id", "started_at", "finished_at")
#: the author label a recording asks under: it is a job's `requested_by` in the store, and never a key
REQUESTED_BY = "tour"
POLL_S = 1.0

Log = Callable[[str], None]
Wait = Callable[..., dict[str, Any]]


def _centre(cell_id: str) -> tuple[float, float] | None:
    """The cell's centre, read-only, so the walkthrough can fly to it."""
    from ..store import connect

    con = connect(read_only=True)
    try:
        row = con.execute("select lon, lat from derived.cell where cell_id = ?", [cell_id]).fetchone()
    finally:
        con.close()
    return (float(row[0]), float(row[1])) if row else None


def latest_stage(progress: list[dict[str, Any]]) -> str | None:
    """The last stage the runner's row reports, as the strip in the dashboard reads it."""
    for event in reversed(progress):
        name = str(event.get("event") or "")
        if name.startswith("stage:"):
            return name[len("stage:"):]
    return None


def wait_for_job(job_id: str, timeout_s: float = 1800.0, poll_s: float = POLL_S,
                 log: Log = lambda _m: None) -> dict[str, Any]:
    """Poll the runner's row until the job is final, logging each stage as it closes.

    The interface agent reports a job on a later turn only once it has finished; a recording that asked its
    follow-up while the chain was still running would have to replay "still running", which is nothing a
    room needs to see. So the recording waits here, through `api.jobs.get` like any other poller."""
    from ..api import jobs as J

    deadline = time.monotonic() + timeout_s
    seen: str | None = None
    while True:
        row = J.get(job_id)
        stage = latest_stage(list(row.get("progress") or []))
        if stage and stage != seen:
            log(f"     stage {stage}")
            seen = stage
        if row.get("status") in J.FINAL:
            return row
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} was still {row.get('status')} after {timeout_s:.0f}s")
        time.sleep(poll_s)


@contextmanager
def _job_budget(usd: float | None) -> Iterator[None]:
    """`invoke_analyst` reads what a job may spend from the environment by name; the recording sets it for
    its own process for the length of the session and puts the old value back."""
    if usd is None:
        yield
        return
    before = os.environ.get(A.JOB_BUDGET_VAR)
    os.environ[A.JOB_BUDGET_VAR] = f"{usd:.4f}"
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(A.JOB_BUDGET_VAR, None)
        else:
            os.environ[A.JOB_BUDGET_VAR] = before


def _kept(turn: dict[str, Any]) -> dict[str, Any]:
    """The turn as the file carries it: the keys the panel reads, with the list-valued ones never null."""
    out = {k: turn.get(k) for k in TURN_KEYS}
    for k in ("claims", "caveats", "problems", "tools_used", "jobs_done", "expert_ids"):
        out[k] = list(out.get(k) or [])
    out["published"] = bool(out.get("published"))
    out["cannot_answer"] = bool(out.get("cannot_answer"))
    out["retried"] = bool(out.get("retried"))
    return out


def _say(turn: dict[str, Any], log: Log) -> None:
    route = turn.get("route") or {}
    plan = " · ".join(s.get("tool", "") for s in route.get("plan") or []) or "no reads"
    log(f"     route {route.get('kind')}" + (" (out of scope)" if route.get("out_of_scope") else "") + f", {plan}")
    if turn.get("abstention"):
        log(f"     declined: {turn['abstention'].get('reason')} {turn['abstention'].get('abstain_id')}")
    elif turn.get("published"):
        log(f"     passed the gate{' on the second ask' if turn.get('retried') else ''}, "
            f"{len(turn.get('claims') or [])} claim(s)")
    else:
        log(f"     withheld: {'; '.join(turn.get('problems') or [])}")
    if turn.get("job"):
        log(f"     analyst job {turn['job'].get('job_id')} submitted")
    for done in turn.get("jobs_done") or []:
        diff = done.get("assessment") or {}
        verdict = (diff.get("verdict") or {})
        log(f"     job {done.get('job_id')} {done.get('status')}: verdict {done.get('verdict')}"
            + (f"; against {diff.get('baseline_chain_id')}: {diff.get('n_changed')} node(s) changed, verdict "
               f"{'changed' if verdict.get('changed') else 'unchanged'}" if diff else "")
            + (f"; assessment: {done['assessment_error']}" if done.get("assessment_error") else ""))


def _job_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """The runner's row as the file keeps it: the id, where the job went, what it produced, and nothing of
    the arguments the row repeats from the turn."""
    if row is None:
        return None
    out = {"job_id": str(row.get("job_id") or ""), "cell_id": row.get("cell_id"), "kind": row.get("kind"),
           "requested_by": row.get("requested_by"), "created_at": row.get("created_at")}
    out.update({k: row.get(k) for k in ROW_KEYS})
    out["progress"] = list(out.get("progress") or [])
    return out


def record(cell_id: str, backend: Any, model: str | None = None, effort: str = "low",
           questions: tuple[str, ...] = QUESTIONS, follow_up: str | None = FOLLOW_UP,
           log: Log = lambda _m: None, job_budget_usd: float | None = None,
           wait: Wait = wait_for_job, requested_by: str = REQUESTED_BY) -> dict[str, Any]:
    """Ask each question in one conversation through the interface agent, run the analyst job it submits to
    the end, ask the follow-up, and return the exchange with the values its claims cite.

    The conversation has no insight store on purpose: a recording asks, it never testifies. `wait` is how a
    submitted job is waited for (the runner's row, polled); a test hands in its own."""
    model = model or default_model()
    conv = Conversation(cell_id=cell_id, requested_by=requested_by, insight_store=None)
    turns: list[dict[str, Any]] = []
    job_row: dict[str, Any] | None = None
    with _job_budget(job_budget_usd):
        for i, question in enumerate(questions, 1):
            log(f"  {i}. {question}")
            turn = G.ask(conv, question, backend, model=model, effort=effort, log=log)
            _say(turn, log)
            turns.append(_kept(turn))
            job = turn.get("job") or {}
            if not job.get("job_id"):
                continue
            row = wait(str(job["job_id"]), log=log)
            job_row = row
            # The live card polls the row until it is final and prints what it finds there; a replay cannot
            # poll, so the recorded card carries the row as it ended, where the live one would have fetched it.
            turns[-1]["job"] = {**turns[-1]["job"], **{k: row.get(k) for k in ROW_KEYS if k in row}}
            log(f"     job {row.get('job_id')} {row.get('status')}"
                + (f": {row['error']}" if row.get("error") else ""))
            if follow_up:
                log(f"  {i + 1}. {follow_up}")
                turn = G.ask(conv, follow_up, backend, model=model, effort=effort, log=log)
                _say(turn, log)
                turns.append(_kept(turn))

    cited = {v for t in turns for c in t["claims"] for v in c.get("value_ids", [])}
    centre = _centre(cell_id)
    return {
        "cell_id": cell_id,
        "lon": centre[0] if centre else None,
        "lat": centre[1] if centre else None,
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "model": model,
        "effort": effort,
        "requested_by": requested_by,
        # the turns' own spend; the job's is in its result, and the ledger has the sum of both
        "cost_usd": round(conv.cost_usd, 4),
        "tools_available": sorted({c["tool"] for c in conv.calls}),
        "turns": turns,
        "job": _job_row(job_row),
        "values": {k: conv.values[k] for k in sorted(cited) if k in conv.values},
    }


def write(payload: dict[str, Any], out: Path | None = None) -> Path:
    path = out or (PATHS.web_data / "prospect" / "recorded_chat.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=str) + "\n")
    return path


__all__ = ["FOLLOW_UP", "QUESTIONS", "REQUESTED_BY", "ROW_KEYS", "TURN_KEYS", "latest_stage", "record",
           "wait_for_job", "write"]

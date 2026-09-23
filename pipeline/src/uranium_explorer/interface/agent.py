"""One turn of the interface agent: route, plan or loop, answer, gate, report.

The shape of a turn is the one the API and the panel already read (`question`, `text`, `claims`, `published`,
`problems`, `tools_used`, `cost_usd`); the agent adds the route it took, the abstention it recorded, the
insight it wrote, the job it submitted, and any job that finished since the last turn with its assessment. A
finished job is also staged as a tool result of its own, so the answer call on that turn can read the verdict
and cite the probability, the cost and the diff's counts by id like any other number.

The gate is the same `check_claims` a published memo passes: every number in the answer, prose included,
resolves to a value id a tool returned in this conversation. An answer that fails is sent back once with the
objections and asked again; one that fails twice is withheld, objections in its place. A refusal is an
answer too, recorded under an `abstain_id` with its reason, and a detail the model wrote is shown only if
it passes the same gate. Nothing the model says ever reaches the screen without passing through here.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

from ..backends.base import UsageLimitReached
from ..prospect import tools as T
from ..prospect.memo import check_claims
from ..values import stat
from . import actions as A
from . import default_model
from . import diff as D
from . import loop as L
from . import model as M
from . import router as R
from .conversation import Conversation

ANSWER_PROMPT_VERSION = "interface/answer/v2"
#: the router runs at low effort: a classification, not a judgement
ROUTER_EFFORT = "low"
#: the kinds whose plan is a list of reads; the other three are an action or the loop
PLANNED: frozenset[str] = frozenset({"lookup", "compare", "explain_score", "what_is_unknown", "what_would_change"})
#: the statuses a job runner may report; anything else is still running
DONE, FAILED = "done", ("failed", "error", "refused", "cancelled")
REASON_SAID: dict[str, str] = {
    "not_measured": "nobody measured that here",
    "outside_grid": "that is not on this grid",
    "no_value": "the store holds no value that would answer it",
    "out_of_scope": "this record never says that",
}
GUIDANCE: dict[str, str] = {
    "lookup": "State the value with its id. If the tool has no value for it, abstain with not_measured: an "
              "unknown is not a low reading, and say how far the nearest observation is if the row gives it.",
    "compare": "Put each cell's numbers in their own claims with their own ids; say what differs and what is "
               "unknown on either side. Never subtract one cell's number from the other's.",
    "explain_score": "Cite the criteria, learned and effort scores by id, and the criteria that carried the "
                     "criteria score by their membership ids. Say how much of the learned score is exploration "
                     "history by pointing at the effort score, not by working anything out.",
    "what_is_unknown": "Name the criteria whose state is unknown here, apart from those not met, and for each say "
                       "what the coverage row says about how thin its feature is across the grid.",
    "what_would_change": "Read the sensitivity rows: they rank the unknown criteria by how far the criteria score "
                         "would move if each were measured and met. Every row already carries its rank, "
                         "delta_if_met, score_if_met and score_if_not_met, each beside its *_id, and the first "
                         "row carries score_now: report the rank 1 row (and the next if its delta is the same) by "
                         "those ids. Never compute a delta or a share yourself.",
}

#: the one answer call's schema: the action and its object, and no `reasoning` field, because the cheap model
#: wrote its whole answer into that field and left `answer` out (the loop keeps its reasoning: there it says
#: why a tool is being called, which is what the panel shows)
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["answer", "abstain"]},
        **L.ANSWER_PROPERTIES,
    },
}

Log = Callable[[str], None]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- the gate


#: what the model is told when its reply had the action but no answer in it
NO_ANSWER = ("the reply carried no answer: put the prose in answer.text and every number in a claim under "
             "answer.claims with its value id (the reasoning field is not the answer)")


#: what the model is told when it abstained for want of a value while this turn's plan had staged values
ABSTAINED_WITH_EVIDENCE = ("you abstained with no_value, but this question's plan staged {n} value(s) in {files}: "
                           "read them again and answer from them; abstain again only if none of them bears on "
                           "the question")
VERDICT_WORDS: dict[str, str] = {"supports_closer_look": "supports a closer look",
                                 "insufficient": "insufficient evidence", "evidence_against": "evidence against"}


def check_answer(conv: Conversation, answer: dict[str, Any]) -> list[str]:
    """The gate, after one check the gate cannot make: that there is an answer at all. The smoke run on the
    cheap model twice returned `action: answer` with the whole answer written into `reasoning` and no
    `answer` object; that is refused with a reason the retry can act on, not published as an empty text."""
    if not (answer.get("text") or answer.get("claims")):
        return [NO_ANSWER]
    return gate(conv, answer)


def gate(conv: Conversation, answer: dict[str, Any]) -> list[str]:
    """Every number in the claims and in the prose resolves to an id returned in this conversation."""
    claims = list(answer.get("claims") or [])
    problems = check_claims(claims, conv.values, context=conv.context)
    # the prose is held to the same rule as the claims: a number in the summary must also be backed
    problems += check_claims([{"text": str(answer.get("text") or ""),
                               "value_ids": [v for c in claims for v in (c.get("value_ids") or [])]}],
                             conv.values, context=conv.context)
    return list(dict.fromkeys(problems))


def _label_expert(conv: Conversation, claims: list[dict[str, Any]]) -> list[str]:
    """B19 on the answer: a claim that cites an expert-tier id is marked, and the ids are listed on the turn."""
    cited: set[str] = set()
    for c in claims:
        ids = {str(v) for v in (c.get("value_ids") or [])} & conv.expert_ids
        if ids:
            c["expert"] = True
            cited |= ids
    return sorted(cited)


# ---------------------------------------------------------------- the routed answer


def _answer_prompt(conv: Conversation, question: str, route: R.Route, files: list[str],
                   feedback: list[str] | None, jobs: list[str] | None = None) -> str:
    listing = "\n".join(f"  {c['file']}  <- {c['tool']}({json.dumps(c['args'])})" for c in conv.calls)
    fetched = "\n".join(f"  {f}" for f in files) or "  (nothing new: the opening evidence already holds it)"
    finished = ""
    if jobs:
        # the job the conversation invoked has finished since the last turn: its result is staged like a tool
        # result, and the model is told so, or it looks for the verdict in the cell's rows and abstains
        finished = ("\n\nAn analyst job this conversation invoked has finished. Its result is staged in the file(s) "
                    "below: the verdict as text, and the probability, the cost and the node counts of the diff "
                    "against the stored chain each with a value id. Read it before answering a question about "
                    "what the analyst decided or what changed:\n" + "\n".join(f"  {f}" for f in jobs))
    retry = ""
    if feedback:
        retry = ("\n\nYour previous answer was refused by the check:\n" + "\n".join(f"  - {p}" for p in feedback)
                 + "\nRewrite it so that every number cites, in value_ids, the id of the value it came from, or "
                   "leave the number out, or abstain. Do not repeat the refused answer.")
    return f"""Cell {conv.cell_id}.

The question: {question}

Routed as {route.kind}: {R.MEANING.get(route.kind, '')}.

Earlier in this conversation:
{conv.transcript()}

Read these first if you have not:
  {{STAGE_DIR}}/handbook.md    what the record supports and what it does not
  {{STAGE_DIR}}/criteria.toml  the criteria, thresholds, status and caveats
The evidence for this question, fetched by the plan (read these before anything else):
{fetched}
Everything staged in this conversation:
{listing}{finished}

{GUIDANCE.get(route.kind, '')}

Reply with action "answer" and the answer object (text, and claims each carrying the value ids of its numbers),
or action "abstain" with a reason and what would answer it when the staged evidence cannot answer the question.
A reply with no answer object is refused. Every number needs the value id it came from.{retry}"""


def _answer_once(conv: Conversation, question: str, route: R.Route, files: list[str], backend: Any, model: str,
                 effort: str, on_event: M.Event, feedback: list[str] | None,
                 jobs: list[str] | None = None) -> L.Outcome:
    assert conv.stage is not None
    out = L.Outcome()
    step = 2 if feedback else 1
    on_event({"type": "thinking", "step": step, "of": 2})
    req = M.request(task="interface_answer", stage=conv.stage, system=L.SYSTEM,
                    prompt=_answer_prompt(conv, question, route, files, feedback, jobs), schema=ANSWER_SCHEMA,
                    prompt_version=ANSWER_PROMPT_VERSION, model=model, effort=effort,
                    salt="retry" if feedback else None, first=[*(jobs or []), *files])
    try:
        reply, out.spent = M.call(backend, req, on_event, step)
    except UsageLimitReached as limit:
        out.problems.append(f"usage limit: {limit}")
        out.usage_limited = True
        return out
    if reply.get("reasoning"):
        on_event({"type": "reasoning", "step": step, "text": str(reply["reasoning"])})
    if reply.get("action") == "abstain":
        out.abstain = dict(reply.get("abstain") or {})
    else:
        out.answer = dict(reply.get("answer") or {})
    return out


def _execute(conv: Conversation, steps: list[R.Step], on_event: M.Event, log: Log) -> list[str]:
    """The plan's reads, staged; a read already staged with the same arguments is reused. Returns the files
    the answer should read first."""
    assert conv.stage is not None
    files: list[str] = []
    for step in steps:
        existing = conv.staged(step.tool, step.args)
        if existing is not None:
            files.append(existing["file"])
            on_event({"type": "tool", "tool": step.tool, "args": step.args, "reasoning": "already staged",
                      "rows": existing["rows"], "values": existing["values"], "reused": True})
            continue
        try:
            result = conv.record(L.call_tool(step.tool, step.args))
        except T.ToolError as err:
            log(f"    tool error: {err}")
            on_event({"type": "tool_error", "tool": step.tool, "args": step.args, "error": str(err)})
            continue
        log(f"    tool: {step.tool}({json.dumps(step.args)})")
        files.append(conv.calls[-1]["file"])
        on_event({"type": "tool", "tool": step.tool, "args": step.args, "reasoning": "the plan for this kind",
                  "rows": len(result.rows), "values": len(result.values)})
    return files


# ---------------------------------------------------------------- jobs that finished since the last turn


#: the tool name a finished job is staged under, so the persisted turn and the stage listing name it
JOB_RESULT = "job_result"


def _job_values(conv: Conversation, job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The numbers a finished job carries, each under an id the gate can resolve: the adjudicator's
    probability, what the job spent, and the node counts of the diff. The verdict is a word and stays text.
    The ids follow the MCP server's `job_status`, which mints `c:job:<job>:cost_usd` for the same figure."""
    job_id = str(job["job_id"])
    result = dict(job.get("result") or {})
    diff = job.get("assessment") or {}
    base = f"c:job:{job_id}"
    values: dict[str, dict[str, Any]] = {}

    def add(key: str, value: Any, fmt: str, note: str, unit: str | None = None) -> None:
        if value is None:
            return
        vid = f"{base}:{key}"
        values[vid] = stat(vid, value, fmt=fmt, note=note, unit=unit)

    if result.get("probability") is not None:
        add("probability", float(result["probability"]), "ratio3",
            f"the adjudicator's probability for analyst job {job_id} on cell {conv.cell_id}: that the public "
            "record labels this cell a known deposit or occurrence, a calibration figure and never a "
            "probability that ore is present")
    if result.get("cost_usd") is not None:
        add("cost_usd", float(result["cost_usd"]), "m2", f"what analyst job {job_id} spent on live model calls",
            unit="USD")
    if diff:
        add("n_nodes", len(diff.get("nodes") or []), "int",
            f"nodes compared in the diff of analyst job {job_id}'s chain against the stored chain")
        add("n_changed", int(diff.get("n_changed") or 0), "int",
            f"nodes whose status differs between analyst job {job_id}'s chain and the stored chain")
        add("n_leaning_on_expert", int(diff.get("n_leaning_on_expert") or 0), "int",
            f"nodes of analyst job {job_id}'s chain that cite an expert-tier value")
    return values


def _stage_result(conv: Conversation, job: dict[str, Any]) -> str:
    """The finished job staged as a tool result of its own, `tool_NN_job_result.json`, in the shape every
    staged read has (`tool`, `rows`, `values`): one row with the verdict as text and each number beside its
    id under a `*_id` key, and the values in the registry so a claim may cite them and pass the gate. The
    handover file (`tool_NN_run_analyst.json`) is rewritten to say the job is reported and where, because a
    model that reads `reported: false` there concludes nothing has come back and abstains."""
    job_id = str(job["job_id"])
    values = _job_values(conv, job)
    result = dict(job.get("result") or {})
    diff = job.get("assessment") or {}
    row: dict[str, Any] = {"job_id": job_id, "cell_id": conv.cell_id, "status": job.get("status"),
                           "verdict": result.get("verdict") or job.get("verdict") or "",
                           "chain_id": result.get("chain_id") or "", "run_id": result.get("run_id") or "",
                           "arm": result.get("arm") or "", "published": bool(result.get("published")),
                           "problems": list(result.get("problems") or [])}
    if job.get("error"):
        row["error"] = str(job["error"])
    for key in ("probability", "cost_usd", "n_nodes", "n_changed", "n_leaning_on_expert"):
        vid = f"c:job:{job_id}:{key}"
        if vid in values:
            row[key], row[f"{key}_id"] = values[vid]["value"], vid
    if diff:
        verdict = dict(diff.get("verdict") or {})
        row["diff"] = {
            "baseline_chain_id": diff.get("baseline_chain_id"),
            "verdict_before": verdict.get("before"), "verdict_after": verdict.get("after"),
            "verdict_changed": bool(verdict.get("changed")),
            "changed_nodes": [{k: n.get(k) for k in ("node_id", "criterion", "kind", "before", "after")}
                              for n in (diff.get("nodes") or []) if n.get("changed")],
            "expert_ids": list(diff.get("expert_ids") or []),
            "note": diff.get("note") or "",
        }
    elif job.get("assessment_error"):
        row["diff"] = {"note": f"no diff: {job['assessment_error']}"}
    name = conv.record_action(JOB_RESULT, {"tool": JOB_RESULT, "job_id": job_id, "rows": [row], "values": values})
    for call in conv.calls:
        if call["tool"] == "run_analyst" and call["args"].get("job_id") == job_id:
            path = conv.staging() / call["file"]
            try:
                handover = json.loads(path.read_text())
            except (OSError, ValueError):
                handover = {}
            handover.update({"status": job.get("status"), "reported": True, "result_file": name})
            path.write_text(json.dumps(handover, indent=1, default=str))
    return name


def job_report(conv: Conversation, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """The finished jobs said from their own records, for a turn whose model reply failed the check: the
    verdict as a word, whether the chain validated, and the probability, cost and changed nodes cited by the
    ids the job minted. Nothing in it is the model's, and it is put to the same gate before it is shown."""
    texts: list[str] = []
    claims: list[dict[str, Any]] = []
    for job in jobs:
        base = f"c:job:{job['job_id']}"
        if job.get("status") != DONE:
            texts.append(f"The analyst job did not finish: {job.get('error') or job.get('status')}.")
            continue
        result = dict(job.get("result") or {})
        verdict = VERDICT_WORDS.get(str(result.get("verdict") or ""), "no verdict")
        kept = "" if result.get("published") else "; its chain was not validated, so it is stored unpublished"
        texts.append(f"The analyst finished on this cell: {verdict}{kept}.")
        for key, said in (("probability", "the analyst's probability for this cell"),
                          ("n_changed", "nodes whose status differs from the stored chain"),
                          ("cost_usd", "what the analyst job spent")):
            if f"{base}:{key}" in conv.values:
                claims.append({"text": said, "value_ids": [f"{base}:{key}"]})
    return {"text": " ".join(texts), "claims": claims}


def _staged_values(conv: Conversation, files: list[str]) -> int:
    """How many values the calls staged under these files returned."""
    return sum(int(c.get("values") or 0) for c in conv.calls if c.get("file") in files)


def report_jobs(conv: Conversation, on_event: M.Event) -> list[dict[str, Any]]:
    """Every job this conversation submitted that has since finished: its verdict and the diff against the
    cell's stored chain without the insight (deterministic), each reported once and staged once, under
    `result_file`, with the ids a claim may cite under `value_ids`."""
    done: list[dict[str, Any]] = []
    for job in conv.jobs:
        if job.get("reported") or "job_id" not in job:
            continue
        status = A.job_status(str(job["job_id"]))
        if not status:
            continue
        job["status"] = str(status.get("status") or job.get("status") or "")
        if "progress" in status:
            job["progress"] = status["progress"]
        if job["status"] == DONE:
            result = dict(status.get("result") or {})
            job["result"] = result
            chain_id = result.get("chain_id")
            job["verdict"] = result.get("verdict")
            try:
                job["assessment"] = D.assess(conv.cell_id, str(chain_id)) if chain_id else None
            except Exception as err:  # noqa: BLE001 - a chain the store does not hold yet is reported, not raised
                job["assessment"] = None
                job["assessment_error"] = f"{type(err).__name__}: {err}"
        elif job["status"] in FAILED:
            job["error"] = str(status.get("error") or status.get("detail") or job["status"])
        else:
            continue
        job["reported"] = True
        job["result_file"] = _stage_result(conv, job)
        job["value_ids"] = sorted(f"c:job:{job['job_id']}:{k}" for k in ("probability", "cost_usd", "n_nodes",
                                                                        "n_changed", "n_leaning_on_expert")
                                  if f"c:job:{job['job_id']}:{k}" in conv.values)
        done.append(job)
        on_event({"type": "job", **job})
    return done


# ---------------------------------------------------------------- the turn


def ask(
    conv: Conversation, question: str, backend: Any, model: str | None = None, effort: str = "medium",
    log: Log = lambda _m: None, on_event: M.Event = lambda _e: None,
) -> dict[str, Any]:
    """One question, routed, answered from the evidence record and gated before it is returned.

    `on_event` receives the turn's own steps as they happen: the route and its plan, each tool call, the
    gate, and the actions (an abstention, an insight, a job), so a caller can show the work rather than a
    spinner."""
    model = model or default_model()
    conv.open()
    on_event({"type": "opened", "tools": [c["tool"] for c in conv.calls]})
    assert conv.stage is not None
    calls_before = len(conv.calls)
    jobs_done = report_jobs(conv, on_event)
    # the files the finished jobs were staged under: read first by this turn's answer call
    reported = [str(j["result_file"]) for j in jobs_done if j.get("result_file")]

    route, spent = R.route(conv, question, backend, model, ROUTER_EFFORT, on_event)
    steps = R.plan(route, question) if route.kind in PLANNED and not route.out_of_scope else []
    route_line = R.describe(route, steps)
    on_event({"type": "route", **route_line})
    log(f"    route: {route.kind}" + (f" ({route.fallback})" if route.fallback else ""))

    turn: dict[str, Any] = {
        "question": question, "text": None, "claims": [], "caveats": [], "cannot_answer": False,
        "published": False, "problems": [], "tools_used": [], "cost_usd": 0.0, "asked_at": _now(),
        "model": model, "route": route_line, "abstention": None, "insight": None, "job": None,
        "jobs_done": jobs_done, "expert_ids": [], "retried": False,
    }

    def finish(out: L.Outcome | None = None, *, text: str | None = None, claims: list[dict[str, Any]] | None = None,
               published: bool | None = None, cannot_answer: bool = False, problems: list[str] | None = None,
               caveats: list[str] | None = None) -> dict[str, Any]:
        turn["text"] = text
        turn["claims"] = claims or []
        turn["caveats"] = caveats or []
        turn["cannot_answer"] = cannot_answer
        turn["problems"] = problems or []
        turn["published"] = bool(published) if published is not None else not turn["problems"]
        if out is not None and out.usage_limited:
            turn["usage_limited"] = True
        turn["expert_ids"] = _label_expert(conv, turn["claims"])
        turn["tools_used"] = [c["tool"] for c in conv.calls[calls_before:]]
        turn["cost_usd"] = round(spent, 4)
        conv.cost_usd += spent
        conv.turns.append(turn)
        return turn

    def refuse(reason: str, detail: str) -> dict[str, Any]:
        """An abstention: recorded, then shown with its reason; the model's detail only if it passes the gate."""
        shown = detail.strip()
        if shown and check_claims([{"text": shown, "value_ids": []}], conv.values, context=conv.context):
            shown = ""   # a number the model wrote into its excuse is still a number it did not get from a tool
        rec = A.abstain(conv, reason, shown)
        turn["abstention"] = {**rec, "said": REASON_SAID.get(rec.get("reason", ""), "")}
        on_event({"type": "abstain", **turn["abstention"]})
        said = REASON_SAID.get(rec.get("reason", ""), "the tools cannot answer this")
        return finish(text=f"No answer: {said}." + (f" {shown}" if shown else ""), published=True, cannot_answer=True)

    # out of scope: straight to the abstain tool, no answer call, no tools
    if route.out_of_scope:
        return refuse("out_of_scope", route.detail)

    if route.kind == "record_insight":
        text = route.insight_text or question.strip()
        rec = A.record_insight(conv, text)
        if "error" in rec:
            return finish(text=None, problems=[rec["error"]])
        turn["insight"] = {"expert_id": rec["expert_id"], "author": rec["author"], "text": text,
                           "value_ids": list(rec.get("value_ids") or []), "recorded_at": rec.get("recorded_at")}
        on_event({"type": "insight", **turn["insight"]})
        claims = ([{"text": "the numbers in the insight, as written, now carry expert-tier ids",
                    "value_ids": list(rec.get("value_ids") or [])}] if rec.get("value_ids") else [])
        return finish(text=f"Recorded in the expert tier as {rec['expert_id']}, author {rec['author']}. From here "
                           "on any agent in this conversation may cite it, labelled as your statement rather than "
                           "as something the data shows.", claims=claims, published=True)

    if route.kind == "run_analyst":
        rec = A.invoke_analyst(conv, reason=question.strip())
        if "error" in rec:
            return finish(text=f"Not invoked: {rec['error']}.", published=True, cannot_answer=True)
        turn["job"] = {k: v for k, v in rec.items() if k != "tool"}
        on_event({"type": "job", **turn["job"]})
        return finish(text=f"Analyst invoked on cell {conv.cell_id}. It reads the evidence pack, the out-of-fold "
                           "scores and the expert-tier values recorded in this conversation, gated like any other "
                           "run; the verdict and the diff against the stored chain without the insight are "
                           "reported here when it finishes.", published=True)

    if route.kind == "compare":
        if len(route.cell_ids) < 2:
            return refuse("no_value", "a comparison needs a second cell id, written like 0123_0045")
        from ..mcp.sessions import cell_exists

        missing = [c for c in route.cell_ids[1:2] if not cell_exists(c)]
        if missing:
            return refuse("outside_grid", f"cell {missing[0]} is not on this grid")

    files: list[str] = []
    if route.kind in PLANNED:
        files = reported + _execute(conv, steps, on_event, log)
        out = _answer_once(conv, question, route, files, backend, model, effort, on_event, feedback=None,
                           jobs=reported)
    else:
        out = L.run(conv, question, backend, model, effort, on_event, log)
    spent += out.spent

    # "no value" while the plan had just staged values for this very question is a misreading, not an answer:
    # the cheap model once abstained on a sensitivity table that held every delta it said was missing. It is
    # asked once more, told which files hold them; a second abstention stands. This is the turn's one retry.
    n_staged = _staged_values(conv, files)
    if out.abstain is not None and str(out.abstain.get("reason") or "") == "no_value" and n_staged:
        turn["retried"] = True
        objection = [ABSTAINED_WITH_EVIDENCE.format(n=n_staged, files=", ".join(files))]
        on_event({"type": "refused", "problems": objection})
        out = _answer_once(conv, question, route, files, backend, model, effort, on_event, feedback=objection,
                           jobs=reported)
        spent += out.spent

    if out.usage_limited or (out.answer is None and out.abstain is None):
        return finish(out, text=None, problems=out.problems or ["the model neither answered nor abstained"])
    if out.abstain is not None:
        return refuse(str(out.abstain.get("reason") or "no_value"), str(out.abstain.get("detail") or ""))

    answer = out.answer or {}
    on_event({"type": "checking", "claims": len(answer.get("claims") or [])})
    problems = check_answer(conv, answer)
    if problems and not turn["retried"]:
        # once more with the objections; the second refusal is final
        turn["retried"] = True
        on_event({"type": "refused", "problems": problems})
        # the retry names the turn's files too: they must lead its bundle as they led the first ask's, or a
        # size-capped inline cuts them and the retry abstains over evidence it was never shown
        again = _answer_once(conv, question, route, files, backend, model, effort, on_event, feedback=problems,
                             jobs=reported)
        spent += again.spent
        if again.usage_limited:
            return finish(again, text=None, problems=problems + again.problems)
        if again.abstain is not None:
            return refuse(str(again.abstain.get("reason") or "no_value"), str(again.abstain.get("detail") or ""))
        answer = again.answer or {}
        on_event({"type": "checking", "claims": len(answer.get("claims") or [])})
        problems = check_answer(conv, answer)
    if problems and jobs_done:
        # the analyst's result must not be lost to a reply that failed the check: say it from the job's record
        report = job_report(conv, jobs_done)
        if report["text"] and not check_answer(conv, report):
            turn["job_report"] = True
            on_event({"type": "job_report", "problems": problems})
            return finish(text="The reply failed the evidence check, so this is said from the job's own record. "
                               + report["text"], claims=report["claims"])
    claims = [dict(c) for c in (answer.get("claims") or [])]
    return finish(text=str(answer.get("text") or "") if not problems else None,
                  claims=claims if not problems else [], caveats=[str(c) for c in (answer.get("caveats") or [])],
                  cannot_answer=bool(answer.get("cannot_answer")), problems=problems)


__all__ = ["ABSTAINED_WITH_EVIDENCE", "ANSWER_PROMPT_VERSION", "ANSWER_SCHEMA", "GUIDANCE", "JOB_RESULT", "NO_ANSWER",
           "PLANNED", "REASON_SAID", "VERDICT_WORDS", "job_report",
           "ask", "check_answer", "gate", "report_jobs"]

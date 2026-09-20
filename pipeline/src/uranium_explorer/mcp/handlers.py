"""The tools, one function each, over a live session: what `tools/call` runs after auth and before the wire.

Every handler returns a `CallToolResult`. A refusal of any kind (a cell outside the grid, a handle that is not
the caller's, a blind-listed file, a tool the key's scope does not carry, an argument outside its schema) is
a result with `isError` and a reason the model can act on, never an exception, because the model is the one
that has to do something about it (principle 5). The only exceptions that leave this module are bugs.

The gate is middleware here in the first of its three places (principle 8): every read result is shaped by
the contract and every id it carries, the tools' own and the ones minted on the way, is stamped into the
session registry before the result goes out, so `check_claims` later resolves exactly what was served.
Every call is a span under the session's run (principle 10): the arguments' hash, the result ids, the
latency, the session and the run id, written to the run's `spans.jsonl` and mirrored to MLflow Tracing when
that is on. Argument values are hashed, never recorded: a real cell id in a benchmark trace is a leak.

Nothing here samples a model (principle 11). `run_analyst` is long work and so a job, not a blocked call
(principle 7): it hands the cell to the API's job runner (`api.jobs`, the same pool the dashboard uses) and
returns the job id at once; `job_status` reads the row back, its cost as a value with an id.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from functools import partial
from typing import Any, Callable

import duckdb
import mcp.types as types
from jsonschema import Draft202012Validator
from jsonschema import exceptions as js_exceptions

from ..analyst.session import SessionRefusal
from ..api import jobs as JOBS
from ..ids import sha256_json, short
from ..prospect import memo
from ..prospect.inventory import load as load_inventory
from ..prospect.tools import ToolError
from ..runtime.tracing import set_attrs, span, trace
from ..store import connect
from ..values import stat
from . import TOOL_VERSION, holes
from .auth import Principal
from .contract import CATALOGUE, REGISTRY_TOOLS, ToolSpec, absent, id_base, is_val, prose, shape_rows
from .sessions import ABSTAIN_REASONS, RUN_KIND, LiveSession, SessionError, SessionStore, now_utc

#: what a passage's text becomes in the public-safe build: the citation and the ids stay, the words do not
WITHHELD = "(withheld: report text is not redistributable in the public-safe build)"
#: the retrieval tiers whose text is a report's own words
REPORT_TIERS = frozenset({"page", "extracted"})
#: what `run_analyst` says when the server was built without a job runner (a stdio server outside the API)
NOT_AVAILABLE = ("run_analyst is not available on this server: it has no job runner, and the staged analyst then "
                 "runs offline through `ue arm chain`; the API process serves it at /mcp with the runner attached")


#: one traced call at a time across sessions. `runtime.tracing.trace` keeps a process-wide fallback trace for
#: pool threads and restores it on exit; two traces overlapping from two worker threads would leave a stale
#: fallback behind, and a span opened later in a thread with no trace of its own (the chat agent's tool calls in
#: the API process) would attach to a closed MCP run. Serialising the traced block keeps the nesting strict.
#: One client at a time is the prototype's stated limit (PRD §E.3 backlog: per-client quotas).
_TRACE_LOCK = threading.Lock()


def _quiet(_line: str) -> None:
    """The tracing module's log sink: nothing reaches stdout, which over stdio is the wire."""


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def error(reason: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=reason)], is_error=True)


def result(text: str, structured: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structured_content=structured)


def result_ids(res: types.CallToolResult) -> list[str]:
    """The value ids a result carries, for the span: the registry it served, else the ids it minted."""
    sc = res.structured_content or {}
    if isinstance(sc.get("values"), dict):
        return sorted(sc["values"])
    return sorted(v for k, v in sc.items() if k.endswith("_id") and isinstance(v, str))


_validators: dict[str, Draft202012Validator] = {}


def validate_args(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """The first way the arguments fall outside the tool's input schema, worded for the caller, or None."""
    v = _validators.get(spec.name)
    if v is None:
        v = _validators[spec.name] = Draft202012Validator(spec.input)
    err = js_exceptions.best_match(v.iter_errors(args))
    if err is None:
        return None
    where = ".".join(str(p) for p in err.absolute_path) or "arguments"
    return f"{spec.name}: {where}: {err.message}"


class Handlers:
    """The tools over one `SessionStore`. `insight_store` opens the connection `record_insight` writes through
    (the live store's, read-write, by default), so a test hands it a temporary one."""

    def __init__(self, store: SessionStore, *, public_safe: bool = False,
                 insight_store: Callable[[], duckdb.DuckDBPyConnection] | None = None,
                 jobs: JOBS.Runner | None = None) -> None:
        self.store = store
        self.public_safe = public_safe
        self.insight_store = insight_store or partial(connect, None, False)
        #: the job runner `run_analyst` submits to and `job_status` reads from; None on a server without one
        self.jobs = jobs

    # ---------------------------------------------------------------- dispatch

    def call(self, name: str, args: dict[str, Any], principal: Principal) -> types.CallToolResult:
        spec = CATALOGUE.get(name)
        if spec is None:
            return error(f"no tool named {name!r}; tools/list names the ones this key may call")
        if not principal.allows(spec.scope):
            return error(f"{name} needs the `{spec.scope}` scope, which this key does not carry; "
                         f"tools/list shows what it may call")
        args = dict(args or {})
        problem = validate_args(spec, args)
        if problem:
            return error(problem)
        if name == "open_session":
            return self.open_session(args, principal)
        try:
            live = self.store.get(args.get("session_id"), principal.name)
        except SessionError as err:
            return error(err.reason)
        with _TRACE_LOCK, live.lock:
            live.n_calls += 1
            n = live.n_calls
            with trace(live.run_id, RUN_KIND, run_dir=live.run_dir, log=_quiet, session_id=live.session_id,
                       principal=principal.name, call=n):
                with span(f"mcp:{name}", kind="tool", tool=name, session_id=live.session_id, run_id=live.run_id,
                          principal=principal.name, args_sha256=sha256_json(args), call=n):
                    res = self._dispatch(spec, live, args)
                    set_attrs(is_error=bool(res.is_error), result_ids=result_ids(res),
                              n_rows=len((res.structured_content or {}).get("rows") or []))
            live.write_manifest()
        return res

    def _dispatch(self, spec: ToolSpec, live: LiveSession, args: dict[str, Any]) -> types.CallToolResult:
        try:
            if spec.name in REGISTRY_TOOLS:
                return self.read(spec, live, args)
            if spec.name == "hole_crosscheck":
                return self.hole_crosscheck(live, args)
            if spec.name == "check_claims":
                return self.check_claims(live, args)
            if spec.name == "abstain":
                return self.abstain(live, args)
            if spec.name == "record_insight":
                return self.record_insight(live, args)
            if spec.name == "run_analyst":
                return self.run_analyst(live, args)
            if spec.name == "job_status":
                return self.job_status(live, args)
        except SessionRefusal as refusal:
            # the rule that refused is the reason: the session counted it under that rule already
            return error(f"refused under {refusal.rule}: {refusal.args[0].split(': ', 1)[-1]}")
        except (ToolError, holes.HoleError, SessionError) as err:
            return error(str(err))
        return error(f"{spec.name} has no handler in this build")  # pragma: no cover - the catalogue is closed

    # ---------------------------------------------------------------- open_session

    def open_session(self, args: dict[str, Any], principal: Principal) -> types.CallToolResult:
        try:
            live = self.store.open(cell_id=str(args["cell_id"]), purpose=str(args["purpose"]),
                                  fold=args.get("fold"), bench_id=args.get("bench_id"), principal=principal.name)
        except SessionError as err:
            return error(err.reason)
        fold = live.session.fold
        fold_val: dict[str, Any]
        if fold is None:
            fold_val = absent("no fold: a dashboard session serves the fitted scores" if not live.blinded
                              else "the out-of-fold table holds no single fold for this cell; pass fold to open_session")
        else:
            vid = f"{id_base('session', live.shown, live.session.purpose == 'benchmark')}:fold"
            fold_val = stat(vid, int(fold), note=f"the spatial fold whose out-of-fold scores session {live.session_id} serves")
            live.register({vid: fold_val})
        structured = {
            "session_id": live.session_id, "cell": live.shown, "purpose": live.session.purpose, "fold": fold_val,
            "blind_list_hash": live.session.blind_list_hash, "expires_at": _iso(live.expires_at),
            "run_id": live.run_id, "tool_contract": TOOL_VERSION,
        }
        with _TRACE_LOCK, trace(live.run_id, RUN_KIND, run_dir=live.run_dir, log=_quiet, session_id=live.session_id,
                                principal=principal.name, call=0):
            with span("mcp:open_session", kind="tool", tool="open_session", session_id=live.session_id,
                      run_id=live.run_id, principal=principal.name, args_sha256=sha256_json(args),
                      purpose=live.session.purpose, blinded=live.blinded):
                set_attrs(result_ids=[fold_val["id"]] if is_val(fold_val) else [], is_error=False)
        live.write_manifest()
        blind = (f"{len(live.session.blind_list)} file(s) blind-listed (hash {live.session.blind_list_hash[:12]})"
                 if live.blinded else "unblinded")
        text = (f"Session {live.session_id} open on cell {live.shown} for the {live.session.purpose} purpose, "
                f"{blind}; it expires at {_iso(live.expires_at)} and its run is {live.run_id}. Pass session_id to "
                "every other tool. Every number the tools return carries a value id; cite the ids in check_claims "
                "before you answer, and call abstain when the tools cannot answer.")
        return result(text, structured)

    # ---------------------------------------------------------------- the eight reads

    def read(self, spec: ToolSpec, live: LiveSession, args: dict[str, Any]) -> types.CallToolResult:
        name = spec.name
        call_args = {k: v for k, v in args.items() if k != "session_id"}
        if "cell_id" in spec.input["properties"]:
            asked = call_args.get("cell_id")
            if asked is None:
                call_args["cell_id"] = live.shown
            elif live.blinded and asked != live.shown:
                return error(f"a {live.session.purpose} session reads its own cell only: pass cell_id "
                             f"{live.shown} or leave it out (asking about another cell would bypass the label "
                             "mask and the blind-list)")
        if self.public_safe and name == "nearby":
            layer = str(call_args.get("layer"))
            if not self.redistributable(layer):
                return error(f"{layer} is not redistributable under its licence, so the public-safe build does "
                             "not serve it; the inventory (ue://reading/inventory) names the flag")
        res = live.session.call(name, call_args)
        values = dict(res.values)
        rows = shape_rows(name, res.rows, values, id_base(name, live.shown, live.session.purpose == "benchmark"))
        live.register({k: v for k, v in values.items() if k not in res.values})
        if self.public_safe and name == "retrieve":
            for row in rows:
                if row.get("tier") in REPORT_TIERS and "text" in row:
                    row["text"] = WITHHELD
        structured = {"tool": name, "session_id": live.session_id, "cell": live.shown, "note": res.note,
                      "rows": rows, "values": values}
        return result(prose(name, live.shown, live.session_id, res.note, rows, values), structured)

    @staticmethod
    def redistributable(layer: str) -> bool:
        src = next((s for s in load_inventory().sources if s.key == layer), None)
        return bool(src is None or src.licence.redistributable)

    # ---------------------------------------------------------------- hole_crosscheck

    def hole_crosscheck(self, live: LiveSession, args: dict[str, Any]) -> types.CallToolResult:
        if live.blinded:
            return error(f"hole_crosscheck names files, holes and positions: dashboard sessions only, not "
                         f"{live.session.purpose}")
        if self.public_safe:
            return error("hole_crosscheck is not served in the public-safe build: the collars it compares are "
                         "read off report pages and one provincial compilation (geods_holes) is not redistributable")
        asked = args.get("cell_id") or live.shown
        centre = cell_centre(asked)
        if centre is None and not (args.get("file_num") or args.get("hole_id")):
            return error(f"cell {asked} is outside the grid")
        rows, values, note = holes.hole_crosscheck(
            centre=centre, cell_shown=asked, hole_id=args.get("hole_id"), file_num=args.get("file_num"),
            radius_km=float(args.get("radius_km", holes.DEFAULT_RADIUS_KM)))
        shaped = shape_rows("hole_crosscheck", rows, values, id_base("hole_crosscheck", asked, False))
        live.record("hole_crosscheck", holes.as_json(shaped, values, note))
        structured = {"tool": "hole_crosscheck", "session_id": live.session_id, "cell": asked, "note": note,
                      "rows": shaped, "values": values}
        return result(prose("hole_crosscheck", asked, live.session_id, note, shaped, values), structured)

    # ---------------------------------------------------------------- check_claims, abstain, record_insight

    @staticmethod
    def check_claims(live: LiveSession, args: dict[str, Any]) -> types.CallToolResult:
        claims = [dict(c) for c in args["claims"]]
        problems = live.session.check_claims(claims)
        cited = sorted({str(v) for c in claims for v in (c.get("value_ids") or [])})
        resolved = [v for v in cited if v in live.session.values]
        unresolved = [v for v in cited if v not in live.session.values]
        structured = {"ok": not problems, "problems": problems, "resolved": resolved, "unresolved": unresolved,
                      "session_id": live.session_id}
        if problems:
            text = (f"{len(problems)} problem(s) over {len(claims)} claim(s):\n" + "\n".join(f"- {p}" for p in problems)
                    + "\nRewrite or drop each claim named above: a number must cite, in value_ids, the id a tool "
                      "returned it under in this session.")
        else:
            text = f"All {len(claims)} claim(s) resolve: {len(resolved)} value id(s) cited, every number backed."
        return result(text, structured)

    @staticmethod
    def abstain(live: LiveSession, args: dict[str, Any]) -> types.CallToolResult:
        reason = str(args["reason"])
        if reason not in ABSTAIN_REASONS:  # pragma: no cover - the schema refuses it first
            return error(f"reason must be one of {ABSTAIN_REASONS}")
        at = now_utc()
        rec = {"abstain_id": f"a:{live.session_id[:8]}:{len(live.abstentions) + 1}", "session_id": live.session_id,
               "reason": reason, "detail": str(args.get("detail") or ""), "recorded_at": _iso(at)}
        live.abstentions.append(rec)
        text = (f"Abstention {rec['abstain_id']} recorded on session {live.session_id}: {reason}"
                + (f" ({rec['detail']})" if rec["detail"] else "") + ". The session's manifest carries it.")
        return result(text, dict(rec))

    def record_insight(self, live: LiveSession, args: dict[str, Any]) -> types.CallToolResult:
        asked = args.get("cell_id")
        if asked is not None and asked != live.shown:
            return error(f"an insight is recorded on the session's own cell ({live.shown}); open a session on "
                         f"{asked} to record one there")
        text, author = str(args["text"]).strip(), str(args["author"]).strip()
        if not text or not author:
            return error("record_insight needs a non-empty text and author")
        at = now_utc()
        expert_id = "e:" + short(sha256_json([live.session.cell_id, author, text, _iso(at), live.session_id]), 10)
        base = f"{id_base('insight', live.shown, live.session.purpose == 'benchmark')}:{expert_id[2:]}"
        values: dict[str, dict[str, Any]] = {}
        for i, token in enumerate(memo.numbers_in(text)):
            try:
                number = float(token.replace(",", ""))
            except ValueError:  # pragma: no cover - the scanner only yields digits
                continue
            vid = f"{base}:{i}"
            val = stat(vid, int(number) if number.is_integer() else number,
                       fmt="int" if number.is_integer() else "m2",
                       note=f"stated by {author} in insight {expert_id}: the number {token} as written")
            val["tier"] = "expert"
            val["expert"] = True
            values[vid] = val
        row = {"expert_id": expert_id, "cell_id": live.session.cell_id, "author": author, "text": text,
               "value_ids_json": json.dumps(sorted(values)), "values_json": json.dumps(values),
               "session_id": live.session_id, "run_id": live.run_id, "principal": live.principal, "recorded_at": _iso(at)}
        try:
            con = self.insight_store()
        except duckdb.Error as err:
            return error(f"the store could not be opened for writing: {err}")
        try:
            con.execute(
                "insert into expert.insight (expert_id, cell_id, author, text, value_ids_json, values_json, "
                "session_id, run_id, principal, recorded_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [row[k] for k in ("expert_id", "cell_id", "author", "text", "value_ids_json", "values_json",
                                  "session_id", "run_id", "principal", "recorded_at")])
        except duckdb.Error as err:
            return error(f"expert.insight could not be written: {err}; the store needs the expert-tier table "
                         "(migration 0005, or one read-write open of the analytics store)")
        finally:
            con.close()
        live.record("record_insight", {"tool": "record_insight", "expert_id": expert_id, "author": author,
                                       "text": text, "values": values}, expert=True)
        live.insights.append({"expert_id": expert_id, "author": author, "recorded_at": _iso(at),
                              "value_ids": sorted(values), "n_chars": len(text)})
        structured = {"expert_id": expert_id, "session_id": live.session_id, "cell": live.shown, "author": author,
                      "values": values, "value_ids": sorted(values), "recorded_at": _iso(at)}
        ids = ", ".join(f"{v['value']} [{k}]" for k, v in values.items()) or "none"
        return result(f"Insight {expert_id} by {author} recorded on cell {live.shown} in the expert tier; the numbers "
                      f"in it now carry expert-tier value ids: {ids}. A claim that leans on one cites its id and "
                      "is labelled expert-tier (B19).", structured)


    # ---------------------------------------------------------------- run_analyst, job_status

    def run_analyst(self, live: LiveSession, args: dict[str, Any]) -> types.CallToolResult:
        if self.jobs is None:
            return error(NOT_AVAILABLE)
        if live.blinded:
            return error(f"run_analyst runs the analyst over the real cell for the dashboard: open a dashboard "
                         f"session, not a {live.session.purpose} one")
        asked = args.get("cell_id")
        if asked is not None and asked != live.shown:
            return error(f"run_analyst runs over the session's own cell ({live.shown}); open a session on {asked} "
                         "to run it there")
        job_args: dict[str, Any] = {"reason": str(args.get("reason") or ""),
                                    "expert_ids": [str(x) for x in (args.get("expert_ids") or [])]}
        if args.get("config"):
            job_args["arm"] = str(args["config"])
        if args.get("budget_usd") is not None:
            job_args["budget_usd"] = float(args["budget_usd"])
        try:
            job_id = self.jobs.submit("analyst", live.session.cell_id, job_args, requested_by=live.principal)
        except JOBS.JobRefused as err:
            return error(str(err))
        row = self.jobs.get(job_id)
        structured = {"job_id": job_id, "status": row["status"], "session_id": live.session_id, "cell": live.shown}
        live.record("run_analyst", {"tool": "run_analyst", "job_id": job_id, "status": row["status"],
                                    "args": row["args"], "values": {}})
        return result(f"Analyst job {job_id} {row['status']} on cell {live.shown} (arm {row['args']['arm']}, budget "
                      f"${row['args']['budget_usd']:.2f}). Poll job_status(session_id, job_id) until its status is "
                      "done, failed or cancelled; when done the result names the chain id, the verdict and the cost, "
                      "and ue://cell/{id}/chains lists the chain.", structured)

    def job_status(self, live: LiveSession, args: dict[str, Any]) -> types.CallToolResult:
        if self.jobs is None:
            return error(NOT_AVAILABLE)
        try:
            row = self.jobs.get(str(args["job_id"]))
        except JOBS.JobNotFound as err:
            return error(str(err))
        job_id = row["job_id"]
        values: dict[str, dict[str, Any]] = {}
        res = row.get("result")
        if isinstance(res, dict):
            vid = f"c:job:{job_id}:cost_usd"
            values[vid] = stat(vid, float(res.get("cost_usd") or 0.0), fmt="m2", unit="USD",
                               note=f"what analyst job {job_id} spent on live model calls")
            live.register(values)
            shaped: dict[str, Any] = {"chain_id": str(res.get("chain_id") or ""), "verdict": str(res.get("verdict") or ""),
                                      "published": bool(res.get("published")), "cost_usd": values[vid]}
            for key in ("run_id", "arm", "reason"):
                if res.get(key):
                    shaped[key] = str(res[key])
        else:
            shaped = absent("no result yet" if row["status"] not in JOBS.FINAL else f"the job {row['status']} without a result")
        progress = []
        for ev in row.get("progress") or []:
            rest = {k: v for k, v in ev.items() if k not in ("at", "event")}
            entry = {"at": str(ev.get("at") or ""), "event": str(ev.get("event") or "")}
            if rest:
                entry["detail"] = ", ".join(f"{k} {v}" for k, v in rest.items())
            progress.append(entry)
        structured = {
            "job_id": job_id, "kind": row["kind"], "status": row["status"], "requested_by": row["requested_by"],
            "created_at": row["created_at"],
            "started_at": row["started_at"] or absent("not started yet"),
            "finished_at": row["finished_at"] or absent("not finished yet"),
            "progress": progress, "result": shaped,
            "error": row["error"] or absent("no error"),
            "run_id": row["run_id"] or absent("no run claimed yet"),
            "session_id": live.session_id,
        }
        if row.get("cell_id"):
            structured["cell"] = str(row["cell_id"])
        stages = [e["event"] for e in progress if e["event"].startswith("stage:")]
        text = (f"Job {job_id} ({row['kind']}) is {row['status']}, asked by {row['requested_by']} at {row['created_at']}"
                + (f"; stages so far: {', '.join(s.removeprefix('stage:') for s in stages)}" if stages else "")
                + (f"; error: {row['error']}" if row["error"] else ""))
        if isinstance(res, dict):
            text += (f". Result: chain {shaped['chain_id']}, verdict {shaped['verdict']}, "
                     f"{'published' if shaped['published'] else 'not published'}, cost {values[vid]['value']} USD "
                     f"[{vid}].")
        return result(text, structured)


def cell_centre(cell_id: str) -> tuple[float, float] | None:
    """A cell's centre in WGS84, read-only, for the hole crosscheck's distance rule."""
    con = connect(read_only=True)
    try:
        row = con.execute("select lon, lat from derived.cell where cell_id = ?", [cell_id]).fetchone()
    finally:
        con.close()
    return (float(row[0]), float(row[1])) if row else None


__all__ = ["Handlers", "NOT_AVAILABLE", "WITHHELD", "cell_centre", "error", "result", "validate_args"]

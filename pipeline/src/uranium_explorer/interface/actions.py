"""The three actions of PRD §8.3: abstain, record an insight, invoke the analyst.

`abstain` and `record_insight` run the MCP server's own handlers (`mcp.handlers.Handlers`), not a second
implementation: the same id minting, the same number scanning, the same `expert.insight` insert. The
handlers read a handful of attributes of a live MCP session; `Live` gives them those, backed by the
conversation, which is the whole cost of sharing one write path. `invoke_analyst` hands the cell, the
out-of-fold score ids, the expert ids recorded in this conversation and the reason to the job runner
(`api.jobs`, imported lazily because it may not be in the build) and returns a job id; the chain runs
offline and a later turn or the panel's poll reports it (PRD §E.3, principle 7). The analyst runs only on
the enabled cells, within a per-session budget (PRD §9.4).
"""

from __future__ import annotations

import datetime as dt
import os
import tomllib
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

from ..bench import pack as P
from ..paths import PATHS
from ..store import connect
from .conversation import Conversation
from .loop import ABSTAIN_REASONS

ENABLED_CELLS = PATHS.pipeline / "knowledge" / "enabled_cells.toml"
#: what one invocation may spend, and what one conversation may spend over all of them (PRD §9.4); both are
#: read by name from the environment so a deployment can set them without a code change. The runner has its
#: own process-wide ceiling on top; the conversation's is the per-session budget §9.4 names.
JOB_BUDGET_VAR, SESSION_BUDGET_VAR = "UE_ANALYST_JOB_BUDGET_USD", "UE_ANALYST_SESSION_BUDGET_USD"
JOB_BUDGET_USD, SESSION_BUDGET_USD = 0.50, 2.00
#: the §9.4 reason, worded without a figure: a refusal is shown as prose and passes the gate like any other
NOT_ENABLED = ("the analyst runs only on the enabled cells in the dashboard (the scope boundary of the PRD): their "
               "chains are computed offline and served as-is, and a live reading is available only on them, within "
               "a per-session budget; cell {cell} is not one of them")
NO_RUNNER = "the analyst job runner (uranium_explorer.api.jobs) is not in this build, so nothing can be submitted"


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _budget(var: str, default: float) -> float:
    try:
        return float(os.environ.get(var, "") or default)
    except ValueError:
        return default


@lru_cache(maxsize=1)
def enabled_cells() -> tuple[str, ...]:
    """The enabled cells, frozen in `knowledge/enabled_cells.toml` (PRD §9.3)."""
    doc = tomllib.loads(ENABLED_CELLS.read_text())
    return tuple(str(c["id"]) for c in doc.get("cell") or [])


class Live:
    """What `Handlers.abstain` and `Handlers.record_insight` read of a live MCP session, backed by a
    conversation: the session id, the cell shown, the purpose, the principal, the two lists they append to,
    and `record`, which stages an action's payload the way the session would."""

    def __init__(self, conv: Conversation) -> None:
        self.conv = conv
        self.session_id = conv.conversation_id
        self.shown = conv.cell_id
        self.session = SimpleNamespace(cell_id=conv.cell_id, purpose="dashboard")
        self.run_id = None
        self.principal = conv.requested_by
        self.abstentions = conv.abstentions
        self.insights = conv.insights

    def record(self, tool: str, payload: dict[str, Any], expert: bool = False) -> None:
        self.conv.record_action(tool, payload, expert=expert)


def _handlers() -> Any:
    """The MCP handlers class, imported when an action runs: the MCP session module imports the chat, and the
    chat imports this, so an import at the top would be a cycle whichever side loads first."""
    from ..mcp.handlers import Handlers

    return Handlers


def _unwrap(res: Any) -> dict[str, Any]:
    """A handler's `CallToolResult` as a plain dict: the structured content, or the refusal as `error`."""
    if getattr(res, "is_error", False):
        text = "".join(getattr(c, "text", "") for c in (res.content or []))
        return {"error": text or "refused"}
    return dict(res.structured_content or {})


# ---------------------------------------------------------------- abstain


def abstain(conv: Conversation, reason: str, detail: str = "") -> dict[str, Any]:
    """A refusal with its reason, recorded on the conversation under an `abstain_id` and staged like a tool
    result, so a refusal is something the benchmark can count rather than an answer that never came."""
    if reason not in ABSTAIN_REASONS:
        reason, detail = "no_value", f"({reason}) {detail}".strip()
    rec = _unwrap(_handlers().abstain(Live(conv), {"reason": reason, "detail": detail}))
    conv.record_action("abstain", {"tool": "abstain", **rec}, quoted=False)
    return rec


# ---------------------------------------------------------------- record_insight


def record_insight(conv: Conversation, text: str, author: str | None = None) -> dict[str, Any]:
    """A geologist's statement into the `expert` tier, through the MCP handler, with author = the caller's
    label. Returns the handler's structured result (`expert_id`, the minted values) or `{"error": ...}`. On
    success the values join the conversation's registry as expert-tier ids, so from here on a claim may cite
    them and is labelled for it (B19)."""
    if conv.insight_store is None:
        return {"error": "no writable store for the expert tier in this session: nothing was recorded"}
    author = (author or conv.requested_by).strip() or conv.requested_by
    handlers = _handlers()(None, insight_store=conv.insight_store)
    rec = _unwrap(handlers.record_insight(Live(conv), {"cell_id": conv.cell_id, "text": text, "author": author}))
    # the handler staged the insight through `Live.record` when it succeeded; a refusal is staged here so the
    # turn's calls say an insight was attempted and why it was not written
    if "error" in rec:
        conv.record_action("record_insight", {"tool": "record_insight", "author": author, "error": rec["error"]})
    return rec


# ---------------------------------------------------------------- invoke the analyst


def oof_score_ids(cell_id: str) -> list[str]:
    """The out-of-fold score ids for the cell (B18): what a scored analyst run may see, named in the handover
    so the manifest can say which scores the invocation carried. Empty when the store holds none."""
    try:
        con = connect(read_only=True)
    except Exception:  # noqa: BLE001 - no store is an empty handover, not a failed invocation
        return []
    try:
        return sorted(P.oof_result(cell_id, con=con).values)
    except Exception:  # noqa: BLE001 - a store without the out-of-fold table
        return []
    finally:
        con.close()


def invoke_analyst(conv: Conversation, reason: str, *, budget_usd: float | None = None,
                   requested_by: str | None = None) -> dict[str, Any]:
    """Submit the analyst on the conversation's cell. Refuses a cell that is not enabled, a session over its
    budget, and a build without the runner; otherwise returns the job record the conversation keeps."""
    cell = conv.cell_id
    if cell not in enabled_cells():
        rec = {"tool": "run_analyst", "error": NOT_ENABLED.format(cell=cell), "cell_id": cell}
        conv.record_action("run_analyst", rec)
        return rec
    budget = float(budget_usd if budget_usd is not None else _budget(JOB_BUDGET_VAR, JOB_BUDGET_USD))
    cap = _budget(SESSION_BUDGET_VAR, SESSION_BUDGET_USD)
    committed = sum(float(j.get("budget_usd") or 0.0) for j in conv.jobs if "job_id" in j)
    if committed + budget > cap:
        rec = {"tool": "run_analyst", "cell_id": cell, "committed_usd": round(committed, 4),
               "session_budget_usd": cap, "asked_usd": budget,
               "error": ("this conversation has committed its per-session analyst budget; another invocation "
                         "would cross it")}
        conv.record_action("run_analyst", rec)
        return rec
    try:
        from ..api import jobs as J
    except ImportError:
        rec = {"tool": "run_analyst", "cell_id": cell, "error": NO_RUNNER}
        conv.record_action("run_analyst", rec)
        return rec
    who = (requested_by or conv.requested_by).strip() or conv.requested_by
    expert_ids = sorted(conv.expert_ids)
    try:
        job_id = J.submit_analyst(cell, reason=reason, expert_ids=expert_ids, budget_usd=budget, requested_by=who)
    except J.JobRefused as err:
        # the runner's own refusal (its budget, its arm, its enabled set) is the reason, in its words
        rec = {"tool": "run_analyst", "cell_id": cell, "error": str(err)}
        conv.record_action("run_analyst", rec)
        return rec
    rec = {"tool": "run_analyst", "job_id": str(job_id), "cell_id": cell, "reason": reason,
           "expert_ids": expert_ids, "score_ids": oof_score_ids(cell), "budget_usd": budget,
           "requested_by": who, "submitted_at": _iso(dt.datetime.now(dt.UTC)), "status": "submitted",
           "reported": False}
    conv.jobs.append(rec)
    conv.record_action("run_analyst", rec)
    return rec


def job_status(job_id: str) -> dict[str, Any] | None:
    """The runner's view of a job (`status`, `progress`, `result`, `error`), None when the runner is not in
    the build, and a failed row when the runner no longer knows the id: a job the store lost is reported
    once, not polled for ever."""
    try:
        from ..api import jobs as J
    except ImportError:
        return None
    try:
        return dict(J.get(job_id) or {})
    except J.JobNotFound as err:
        return {"job_id": job_id, "status": "failed", "error": str(err)}


__all__ = ["ENABLED_CELLS", "JOB_BUDGET_USD", "JOB_BUDGET_VAR", "Live", "NOT_ENABLED", "NO_RUNNER",
           "SESSION_BUDGET_USD", "SESSION_BUDGET_VAR", "abstain", "enabled_cells", "invoke_analyst",
           "job_status", "oof_score_ids", "record_insight"]

"""The agent panel: proponent, skeptic, adjudicator, each with tools, and a gate no memo can talk past.

Three roles argue about one cell. Each runs as a short loop: the model asks for a tool, the runner executes it
in Python and stages the result as a file, and the model reads it on the next turn. The model never computes
anything; it decides what to ask for and what the answers mean.

**The gate is the point of the whole exercise.** A published prospectivity assistant restricted to
tool-computed scores still fabricated a number in 1 of 150 rated responses. Here, every number in a memo must
match a value the tools actually returned in that session, by id. A memo that prints a number it cannot cite is
rejected and never reaches `agent.*`, so the failure mode is impossible to publish rather than measured after
the fact.

The panel is adversarial by construction: the skeptic sees the proponent's memo and is told to attack it, and
the adjudicator sees both and must separate *unknown* from *absent* and name the one observation that would
change the verdict.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..backends.base import ExtractionRequest, UsageLimitReached
from ..ids import sha256_json, short
from ..paths import PATHS
from ..store import append_frame, connect
from . import tools as T

PROMPT_VERSION = "prospect/memo/v1"
SCHEMA_VERSION = "1.0.0"
HANDBOOK = PATHS.pipeline / "knowledge" / "handbook.md"
CRITERIA_FILE = PATHS.pipeline / "knowledge" / "criteria.toml"

ROLES = ("proponent", "skeptic", "adjudicator")
VERDICTS = ("supports a closer look", "insufficient evidence", "evidence against")

MAX_STEPS = 6

#: A number the memo states as a measurement. Deliberately not every digit in the prose: a hole called HR-014,
#: a map scale of 1:250,000 and a survey period of 1975-1978 are names, not measurements, and the first run of
#: this gate rejected three otherwise sound memos for quoting them. The rule that replaced it: a number must
#: either match a value the claim cites, or appear verbatim in a tool result from this session.
NUMBER = re.compile(r"(?<![\w.:-])(\d(?:[\d,]*\d)?(?:\.\d+)?)(?![\w:-])")
BARE_OK = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "100"}


SYSTEM = """You are part of a three-role panel assessing one 2 km cell of public Saskatchewan data for
unconformity-related uranium. You are not a geologist and neither is the person you are writing for.

Absolute rules:
1. You may not state any number that did not come from a tool result in this session. Every number you write
   must be accompanied by the value id it came from, in the claim's value_ids list. This is checked
   mechanically and a memo that breaks it is discarded.
2. You never compute anything: no arithmetic, no distances, no conversions. Ask a tool.
3. Unknown is not absent. A criterion with no measurement is unknown; a criterion measured and not met is
   absent. Say which.
4. You never recommend drilling, never estimate grade or tonnage, never give a probability that ore is
   present. The strongest verdict available is "supports a closer look".
5. Criteria marked folklore may be named, never used as support.

Work by calling tools first and writing only when you have what you need."""

ROLE_PROMPT = {
    "proponent": """Make the strongest honest case that this cell deserves a closer look, or say plainly that
it does not. Use the criteria breakdown to structure the argument by mineral-systems element (pathway, trap,
cover, detection, dispersal), and retrieve what the assessment record says about this ground.""",
    "skeptic": """Attack the case in {STAGE_DIR}/proponent.json. Look specifically for: a score driven by
exploration effort rather than geology; proximity to a known deposit doing the work; criteria that are unknown
being read as met; thin coverage behind a feature; reliance on contested signatures or on folklore; and any
number that is not backed by a tool result. If the case survives, say which parts survive.""",
    "adjudicator": """Read {STAGE_DIR}/proponent.json and {STAGE_DIR}/skeptic.json and rule. Your verdict must
be one of: "supports a closer look", "insufficient evidence", "evidence against". List which criteria are
unknown and which are absent, separately. Name the single next observation that would most change the verdict.""",
}

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "reasoning"],
    "properties": {
        "action": {"type": "string", "enum": ["call_tool", "write_memo"]},
        "reasoning": {"type": "string"},
        "tool": {"type": "string"},
        "args": {"type": "object", "additionalProperties": True},
        "memo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "claims", "verdict", "unknown_criteria", "absent_criteria",
                         "next_observation"],
            "properties": {
                "summary": {"type": "string"},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["text", "value_ids"],
                        "properties": {
                            "text": {"type": "string"},
                            "value_ids": {"type": "array", "items": {"type": "string"}},
                            "element": {"type": "string"},
                        },
                    },
                },
                "verdict": {"type": "string", "enum": list(VERDICTS)},
                "unknown_criteria": {"type": "array", "items": {"type": "string"}},
                "absent_criteria": {"type": "array", "items": {"type": "string"}},
                "next_observation": {"type": "string"},
                "risks": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


@dataclass
class Session:
    """One role's run over one cell: what it asked for, what it got, and what it may cite."""

    cell_id: str
    role: str
    stage: Path
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    #: everything the tools returned as text, so the gate can tell a quoted name from an invented number
    context: str = ""

    def record(self, result: T.ToolResult) -> Path:
        self.values |= result.values
        payload = json.dumps(result.as_json(), indent=1)
        self.context += "\n" + quotable(result.as_json())
        path = self.stage / f"tool_{len(self.calls) + 1:02d}_{result.tool}.json"
        path.write_text(payload)
        self.calls.append({"tool": result.tool, "args": result.args, "values": len(result.values),
                           "rows": len(result.rows), "file": path.name})
        return path


# ---------------------------------------------------------------- the gate

def quotable(payload: Any) -> str:
    """Only the strings a tool returned, so the gate's leniency for text cannot be borrowed by a number.

    The allowance exists for things that are text: a hole name, a map scale, a stratigraphic period, a sentence
    quoted from a report. It was first written as a substring test over the whole tool payload, which quietly
    meant every number the tools returned was also allowed in every claim — so the value-id binding, the thing
    that makes a citation mean anything at all, stopped doing any work.

    `lr prospect gate-eval` measured the size of that: over 227 deliberately corrupted claims the old allowance
    let 85 through, and it caught none of the 40 cases where a real number was attached to the wrong value —
    the hardest and most plausible failure, because nothing is invented. Keeping only the string leaves restores
    the binding, and the text that needed the allowance is still text.
    """
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return "\n".join(out)




#: unit renderings the gate can verify for itself, so ordinary prose does not need the raw stored unit
SCALES: dict[str, tuple[tuple[str, float], ...]] = {
    "m": (("km", 0.001),),
    "km": (("m", 1000.0),),
}


def _forms(f: float) -> set[str]:
    out = {f"{f:.0f}", f"{f:.1f}", f"{f:.2f}", f"{f:.3f}", f"{f:g}", f"{f:,.0f}", f"{f:,.1f}"}
    if float(f).is_integer():
        out |= {str(int(f)), f"{int(f):,}"}
    return out


def _formatted(val: dict[str, Any]) -> set[str]:
    """Every way a stored value may legitimately appear in prose.

    Includes the obvious unit renderings (a distance in metres written as kilometres), because the gate can
    check that conversion itself. What it never does is accept a number the model arrived at some other way:
    the arithmetic stays on this side of the boundary.
    """
    v = val.get("value")
    if v is None:
        return set()
    out = {str(v)}
    if isinstance(v, (int, float)):
        f = float(v)
        for _unit, factor in SCALES.get(str(val.get("unit") or ""), ()):
            out |= _forms(f * factor)
        out |= {f"{f:.0f}", f"{f:.1f}", f"{f:.2f}", f"{f:.3f}", f"{f:g}"}
        if f.is_integer():
            out.add(str(int(f)))
            out.add(f"{int(f):,}")
        out.add(f"{f:,.0f}")
        out.add(f"{f:,.1f}")
        if 0 <= f <= 1:  # a ratio may be spoken as a percentage
            out |= {f"{f * 100:.0f}", f"{f * 100:.1f}"}
    return out


def check_claims(
    claims: list[dict[str, Any]], values: dict[str, dict[str, Any]], context: str = ""
) -> list[str]:
    """Every number in a claim must cite a value the tools returned, or quote one back verbatim.

    Shared by the memo panel and the conversational agent, so an answer in chat is held to the same rule as a
    published memo: there is no looser path to the screen.
    """
    problems: list[str] = []
    # The allowance is for numbers the tools genuinely returned as text, so it reads the context with the same
    # scanner it reads the claim with. A plain substring test let "72" through because some cell id was
    # 0201_0072, and "1.1" through because it sat inside a longer figure: coincidences, not quotations.
    quoted = {t.strip(".,") for t in NUMBER.findall(context)} if context else set()
    quoted |= {t.replace(",", "") for t in quoted}
    for i, claim in enumerate(claims):
        text = str(claim.get("text") or "")
        ids = [str(v) for v in (claim.get("value_ids") or [])]
        allowed: set[str] = set()
        for vid in ids:
            if vid not in values:
                problems.append(f"claim {i}: cites {vid}, which no tool returned in this session")
                continue
            allowed |= _formatted(values[vid])
        for token in NUMBER.findall(text):
            plain = token.strip(".,")
            if plain in BARE_OK or plain.replace(",", "") in BARE_OK:
                continue
            if plain in allowed or plain.replace(",", "") in {a.replace(",", "") for a in allowed}:
                continue
            if plain and (plain in quoted or plain.replace(",", "") in quoted):
                continue  # the tools returned this number as text: a scale, a period, a quoted passage
            problems.append(f"claim {i}: the number {plain} is not backed by any value this claim cites")
    # the prose is checked against the same ids as the claims, so a bad id would otherwise be reported twice
    return list(dict.fromkeys(problems))


def check_memo(
    memo: dict[str, Any], values: dict[str, dict[str, Any]], context: str = ""
) -> list[str]:
    """Problems that make a memo unpublishable. An empty list means it passes.

    A number in a claim must be one of:

    * a value the claim itself cites by id, in any reasonable written form; or
    * a string the tools actually returned this session (`context`) - a hole name, a map scale, a survey
      period. Quoting the record back is not fabrication.

    Everything else is a number the model produced from nowhere, and the memo is refused.
    """
    problems: list[str] = []
    if memo.get("verdict") not in VERDICTS:
        problems.append(f"verdict {memo.get('verdict')!r} is not one of {VERDICTS}")
    claims = memo.get("claims") or []
    if not claims:
        problems.append("a memo with no claims says nothing")
    problems += check_claims(claims, values, context)
    for key in ("unknown_criteria", "absent_criteria"):
        if not isinstance(memo.get(key), list):
            problems.append(f"{key} must be a list, even an empty one")
    if not str(memo.get("next_observation") or "").strip():
        problems.append("a memo must name the observation that would change the verdict")
    return problems


# ---------------------------------------------------------------- the loop


def _bundle(
    cell_id: str, stage: Path, opening: tuple[tuple[str, dict[str, Any]], ...] = ()
) -> Session:
    """Stage the standing material and any opening tool results, so the first turn already has something."""
    session = Session(cell_id=cell_id, role="", stage=stage)
    (stage / "handbook.md").write_text(HANDBOOK.read_text())
    (stage / "criteria.toml").write_text(CRITERIA_FILE.read_text())
    session.record(T.call("cell_scores", {"cell_id": cell_id}))
    for tool, args in opening:
        if tool == "cell_scores":
            continue
        session.record(T.call(tool, {"cell_id": cell_id, **args}))
    return session


def _prompt(session: Session, role: str, step: int, steps: int = MAX_STEPS) -> str:
    listing = "\n".join(f"  {c['file']}  <- {c['tool']}({json.dumps(c['args'])})" for c in session.calls)
    extra = ""
    if role in ("skeptic", "adjudicator"):
        extra = "\nThe earlier memos are in the stage directory and you must read them."
    left = steps - step - 1
    steps_left = (
        f'You have {left} further tool call(s) available. Reply with action "call_tool" to ask for one, or '
        f'"write_memo" when you can argue from what you have. '
        if left > 0 else
        'Everything the panel normally asks for is already staged above. Reply with action "write_memo". '
    )
    return f"""Cell {session.cell_id}. You are the {role}.

{ROLE_PROMPT[role].replace("{STAGE_DIR}", "{STAGE_DIR}")}

Read these first:
  {{STAGE_DIR}}/handbook.md   what the record supports, and what it does not
  {{STAGE_DIR}}/criteria.toml the criteria, their thresholds, their status and their caveats
Tool results so far:
{listing or "  (none yet)"}
{extra}

Tools you may call:
{chr(10).join("  " + h for h in T.TOOL_HELP.values())}

{steps_left}Every number you write needs the value id it came from."""


#: what a one-shot run is given without being asked: everything the panel usually calls for anyway
OPENING_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("cell_scores", {}),
    ("cell_features", {}),
    ("criteria_breakdown", {}),
    ("label_context", {}),
)


def run_role(
    cell_id: str, role: str, backend: Any, model: str, effort: str, stage: Path,
    prior: dict[str, Any] | None = None, mode: str = "panel", log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """One role over one cell.

    `mode="panel"` lets the role choose its own tools, one call per step: that is the agent-with-tools
    capability, and it costs a model call per tool. `mode="oneshot"` runs the usual tools up front and asks
    for the memo in a single call, which is several times cheaper and right for bulk evaluation runs where
    what is being measured is the judgement, not the tool selection.
    """
    steps = MAX_STEPS if mode == "panel" else 1
    session = _bundle(cell_id, stage, opening=OPENING_CALLS if mode == "oneshot" else ())
    session.role = role
    for name, memo in (prior or {}).items():
        (stage / f"{name}.json").write_text(json.dumps(memo, indent=1))
        session.values |= {k: v for k, v in (memo.get("_values") or {}).items()}

    memo: dict[str, Any] | None = None
    spent = 0.0          # every call in the loop costs money, not just the one that writes the memo
    n_calls = 0
    for step in range(steps):
        prompt = _prompt(session, role, step, steps)
        req = ExtractionRequest(
            task=f"prospect_memo_{role}",
            images=(),
            stage_files=tuple(sorted((p, p.name) for p in stage.iterdir() if p.is_file())),
            system_prompt=SYSTEM,
            user_prompt=prompt,
            schema=STEP_SCHEMA,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            model=model,
            effort=effort,
            # The question is the input here, not the staged files. Without it two different questions over
            # the same evidence hash to the same key and the second one is answered from the first one's
            # cache: a stale answer that looks entirely convincing.
            context_hash=short(sha256_json(prompt)),
        )
        try:
            response = backend.call(req)
        except UsageLimitReached as limit:
            # stop cleanly and keep what the loop already paid for; a re-run resumes from the call cache
            log(f"    {role}: usage limit reached{f' ({limit.resets_at_text})' if limit.resets_at_text else ''}")
            return {"role": role, "published": False, "problems": [f"usage limit: {limit}"],
                    "calls": session.calls, "cost_usd": round(spent, 4), "model_calls": n_calls,
                    "usage_limited": True}
        spent += float(getattr(response, "cost_usd", 0.0) or 0.0)
        n_calls += 1
        out = response.structured
        if out.get("action") == "call_tool":
            try:
                result = T.call(str(out.get("tool")), dict(out.get("args") or {}))
                session.record(result)
                log(f"    {role}: {out.get('tool')}({json.dumps(out.get('args') or {})}) "
                    f"-> {len(result.rows)} rows")
            except T.ToolError as err:
                (stage / f"tool_error_{step}.json").write_text(json.dumps({"error": str(err)}))
                log(f"    {role}: tool error: {err}")
            continue
        memo = out.get("memo") or {}
        break

    if memo is None:
        return {"role": role, "published": False, "problems": ["ran out of steps without writing a memo"],
                "calls": session.calls, "cost_usd": round(spent, 4), "model_calls": n_calls}

    problems = check_memo(memo, session.values, context=session.context
                          + CRITERIA_FILE.read_text())
    memo["_values"] = session.values
    return {
        "role": role, "memo": memo, "published": not problems, "problems": problems,
        "calls": session.calls, "values": len(session.values),
        "cost_usd": round(spent, 4), "model_calls": n_calls,
        "model_resolved": getattr(response, "model_resolved", None),
    }


def run_panel(
    cell_id: str, backend: Any, model: str = "claude-sonnet-5", effort: str = "medium",
    mode: str = "panel", roles: tuple[str, ...] = ROLES, log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Proponent, then skeptic, then adjudicator, over one cell.

    `roles` can be narrowed to a single adjudicator for the cheapest useful run.
    """
    run_id = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    results: dict[str, Any] = {}
    prior: dict[str, Any] = {}
    log(f"  cell {cell_id}")
    for role in roles:
        stage = Path(tempfile.mkdtemp(prefix=f"lr_memo_{role}_"))
        r = run_role(cell_id, role, backend, model, effort, stage, prior=prior, mode=mode, log=log)
        results[role] = r
        if r.get("usage_limited"):
            log("    stopping the panel here; re-run to resume from the call cache")
            break
        if r.get("memo"):
            prior[role] = r["memo"]
        status = "published" if r["published"] else f"REJECTED: {'; '.join(r['problems'][:2])}"
        log(f"    {role}: {r.get('model_calls', 0)} model call(s), {len(r.get('calls', []))} tool results, "
            f"${r.get('cost_usd', 0):.3f}, {status}")
    store_panel(cell_id, run_id, model, results)
    spent = sum(float(r.get("cost_usd") or 0) for r in results.values())
    log(f"    panel total ${spent:.3f}")
    return {"cell_id": cell_id, "run_id": run_id, "results": results, "cost_usd": round(spent, 4)}


def store_panel(cell_id: str, run_id: str, model: str, results: dict[str, Any]) -> None:
    """Write the panel to `agent.*`. A rejected memo is recorded as rejected, never as argument."""
    memos, claims, checks = [], [], []
    for role, r in results.items():
        memo_id = f"{cell_id}:{role}:{uuid.uuid4().hex[:8]}"
        memo = r.get("memo") or {}
        memos.append({
            "memo_id": memo_id, "cell_id": cell_id, "role": role,
            "verdict": memo.get("verdict"), "model": model, "prompt_version": PROMPT_VERSION,
            "run_id": run_id, "cache_key": None, "cost_usd": r.get("cost_usd"),
            "duration_s": None, "created_at": run_id, "published": bool(r.get("published")),
        })
        if r.get("published"):
            for i, claim in enumerate(memo.get("claims") or []):
                claims.append({"memo_id": memo_id, "claim_no": i, "text": str(claim.get("text") or ""),
                               "value_ids": json.dumps(list(claim.get("value_ids") or []))})
        for problem in r.get("problems") or []:
            checks.append({"memo_id": memo_id, "check_id": f"gate_{len(checks)}", "outcome": "fail",
                           "detail": problem})
        if not r.get("problems"):
            checks.append({"memo_id": memo_id, "check_id": "gate", "outcome": "pass",
                           "detail": "every number cites a value the tools returned"})
    con = connect()
    try:
        if memos:
            append_frame(con, "agent", "memo", pd.DataFrame(memos), "agent")
        if claims:
            append_frame(con, "agent", "memo_claim", pd.DataFrame(claims), "agent")
        if checks:
            append_frame(con, "agent", "memo_check", pd.DataFrame(checks), "agent")
    finally:
        con.close()

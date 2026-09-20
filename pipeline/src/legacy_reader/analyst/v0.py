"""Analyst_v0: one closed-book model call per benchmark cell, no tools, no stages, and the same gate a memo
faces.

The panel in `prospect.memo` lets a model ask for evidence a tool at a time. This agent is the floor beneath
it: everything it may know is handed over at once (the map card as an image, the evidence pack and any
retrieved passages as files), it answers once, and the answer is checked. The point of a floor is to be
comparable, so three things are held fixed here and varied only through an `ArmConfig`:

* **Closed book.** The system prompt says the model does not know where the cell is and must not try to
  find out. It names no deposit, camp, lake or region, and `system_prompt` refuses to build if the handbook
  material it draws on ever does, so the rule cannot drift with an edit elsewhere.
* **Switches remove, they do not hide.** An arm with `label_context` off is not told to ignore the distance to
  the nearest deposit; the rows and values are cut from the pack before it is rendered, and the cache key
  carries the switch so two arms can never share an answer.
* **The gate is the memo's gate.** Every number in a claim must cite a value id from the pack the model was
  actually shown, or quote a string the pack contains. `memo.check_claims` does the checking; this module
  only decides what counts as context.

The answer carries a probability. It is the model's probability that the public record labels the cell as a
known deposit or occurrence, asked for so that calibration can be measured; it is not a probability that ore
is present, and the prompt says so.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import tomllib
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..backends.base import ExtractionRequest
from ..ids import sha256_json, short
from ..prospect import models as M
from ..prospect.memo import CRITERIA_FILE, HANDBOOK, check_claims, quotable
from .arms import ArmConfig, Switches

try:  # the benchmark builder's own renderer, when it has landed; otherwise the one below
    from ..bench.pack import pack_text as _pack_text
except ImportError:
    _pack_text = None

TASK = "analyst_v0"
PROMPT_VERSION = "analyst/v0/v1"
SCHEMA_VERSION = "1.0.0"

POSITIVE, ABSTAIN, NEGATIVE = "supports_closer_look", "insufficient", "evidence_against"
VERDICTS = (NEGATIVE, ABSTAIN, POSITIVE)
RATIONALE_MAX = 600

#: what the staged files are called; the CLI backend names a single image `page.png` itself
CARD_FILE, PACK_FILE, PASSAGES_FILE = "page.png", "pack.md", "passages.md"

#: Deposits, camps, lakes and regions the handbook and criteria name. None may reach the closed-book prompt:
#: a model told it is in one basin has been told where to look, and a model told which deposit a criterion's
#: threshold came from can recognise a cell by its numbers.
PLACE_NAMES = (
    "Athabasca", "Saskatchewan", "McArthur", "Cigar Lake", "Patterson Lake", "Hurricane", "Larocque",
    "Arrow", "Kiggavik", "Millennium", "Wolverine", "Triple R", "Key Lake", "Rabbit Lake", "Cluff Lake",
    "Eagle Point", "Phoenix", "Wheeler River", "Shea Creek", "Midwest", "Dawn Lake", "Fond du Lac",
    "Wollaston", "Cree Lake", "Uranium City", "Beaverlodge", "Rook",
)

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "probability", "claims", "unknown_criteria", "absent_criteria",
                 "next_observation", "rationale"],
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "probability": {"type": "number", "minimum": 0, "maximum": 1},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "value_ids"],
                "properties": {
                    "text": {"type": "string"},
                    "value_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "unknown_criteria": {"type": "array", "items": {"type": "string"}},
        "absent_criteria": {"type": "array", "items": {"type": "string"}},
        "next_observation": {"type": "string"},
        "rationale": {"type": "string", "maxLength": RATIONALE_MAX},
    },
}


# ---------------------------------------------------------------- the prompt

RULES = f"""You are an analyst assessing one 2 km cell for unconformity-related uranium, closed-book. You are
given an evidence pack and, when one is provided, a map card of the cell. That is everything you know about
this ground, and you are writing for someone who is not a geologist.

Absolute rules:
1. Only the evidence provided. You do not know where this cell is and you must not guess: no place names, no
   deposit or camp names, no knowledge of what was found anywhere. Do not try to recognise the map. If a name
   appears in the evidence you may quote it; you may not add what you know about it.
2. Every number you state must come from the evidence pack, and the claim that states it must list the value
   id of that number. A value id is a string from the pack's values section (it looks like
   b:b-0001:cell:d_conductor_m). Cite every id a claim needs, and split a claim carrying several numbers rather
   than hoping one id covers them all. Observation counts and coverage shares are numbers too: their ids stand
   in square brackets right after them in the pack. This is checked mechanically; an answer that breaks it is
   discarded.
3. You never compute anything: no arithmetic, no distances, no conversions, no percentages you worked out.
4. Unknown is not absent. A criterion the pack says is unmeasured here is unknown: list it in
   unknown_criteria and say nothing about its value. A criterion measured and not met is absent: list it in
   absent_criteria. A feature with no rows is unknown, not zero.
5. Criteria marked folklore may be named, never used as support.
6. You never recommend drilling and never estimate grade or tonnage. The strongest verdict is
   {POSITIVE}.

The verdict scale, in order: {NEGATIVE} | {ABSTAIN} | {POSITIVE}. "{ABSTAIN}" is a respectable answer and
often the right one: give it when the criteria that would decide are unknown.

The probability is your probability, from 0 to 1, that the public record labels this cell as a known deposit
or occurrence. It is a calibration score for this benchmark, not a probability that ore is present. It is the
one number you may state without a value id, and you state it only in the probability field, never in the
rationale.

The answer is JSON with exactly these fields: verdict; probability; claims, each with text and value_ids
(every number in the text backed by an id in the list); unknown_criteria and absent_criteria, each a list of
criterion keys, either may be empty; next_observation, the single measurement that would most change the
verdict; rationale, at most {RATIONALE_MAX} characters of prose, with no numbers in it that are not cited in a
claim."""

FRAME_ELEMENTS = ("Pathway", "Trap", "Cover", "Detection", "Dispersal")
#: a bullet and the indented lines the handbook wraps it onto
_FRAME_LINE = re.compile(r"^- \*\*(" + "|".join(FRAME_ELEMENTS) + r")\*\*\s*[—–-]+\s*(.+(?:\n[ \t]+\S.*)*)", re.M)
_FOLKLORE_LINE = re.compile(r'^- "(.+?)"\s*$', re.M)


def _frame(handbook_text: str) -> list[str]:
    """The mineral-systems bullets, as the handbook states them, unwrapped."""
    return [f"- {element}: {' '.join(rest.split())}" for element, rest in _FRAME_LINE.findall(handbook_text)]


def _folklore(handbook_text: str) -> list[str]:
    return [f'- "{line}"' for line in _FOLKLORE_LINE.findall(handbook_text)]


def _criteria(criteria_text: str) -> list[str]:
    """One line per criterion: key, element, status, weight, title and caveat. Never the evidence field,
    which is where the source deposits are named."""
    doc = tomllib.loads(criteria_text)
    out = []
    for c in doc.get("criterion", []):
        status = str(c.get("status", ""))
        weight = float(c.get("weight", 0.0))
        tag = "folklore, weight 0: may be named, never used as support" if status == "folklore" \
            else f"{status}, weight {weight:g}"
        caveat = " ".join(str(c.get("caveat", "")).split())
        out.append(f"- {c['key']} ({c.get('element')}; {tag}): {c.get('title')}. Caveat: {caveat}")
    mk = doc.get("min_known_weight")
    if mk is not None:
        out.append(f"- A criteria score is only computed when at least {float(mk):g} of the weighted criteria "
                   f"have a value; below that it is unknown.")
    return out


def place_names_in(text: str) -> list[str]:
    """Whole words only: "Arrow" is a deposit, "narrow band" is not."""
    return [name for name in PLACE_NAMES if re.search(rf"\b{re.escape(name)}\b", text, re.I)]


def system_prompt(handbook_text: str, criteria_text: str) -> str:
    """The condensed closed-book instruction. Refuses to build if any place name reaches it."""
    frame = _frame(handbook_text)
    folklore = _folklore(handbook_text)
    parts = [RULES, ""]
    if frame:
        parts += ["The mineral-systems frame, restricted to what the public record can support:", *frame, ""]
    if folklore:
        parts += ["Folklore: recorded as personal communication with no published test. Name it if a "
                  "geologist might raise it; never lean on it.", *folklore, ""]
    parts += ["The criteria, with status, weight and how each one fails:", *_criteria(criteria_text)]
    text = "\n".join(parts)
    leaked = place_names_in(text)
    if leaked:
        raise ValueError(f"the closed-book prompt would name a place: {leaked}")
    return text


@lru_cache(maxsize=1)
def default_system_prompt() -> str:
    return system_prompt(HANDBOOK.read_text(), CRITERIA_FILE.read_text())


def user_prompt(bench_id: str, card: bool, pack: bool, passages: bool) -> str:
    lines = [f"Cell {bench_id}. Assess it closed-book from what is staged, and nothing else.", "",
             "Read these first:"]
    if card:
        lines.append(f"  {{STAGE_DIR}}/{CARD_FILE}      the map card of the cell")
    if pack:
        lines.append(f"  {{STAGE_DIR}}/{PACK_FILE}       the evidence pack: features, criteria, and the value "
                     f"ids you must cite")
    if passages:
        lines.append(f"  {{STAGE_DIR}}/{PASSAGES_FILE}   passages from the assessment record about this ground")
    lines += ["",
              "Then answer once, as JSON. Every number needs the value id it came from. Where the pack says a "
              "criterion is unmeasured, it is unknown. Do not name places, and do not try to work out where "
              "this is."]
    return "\n".join(lines)


# ---------------------------------------------------------------- the switches

_DRILLHOLE_TOOLS = ("drillhole", "drillholes", "holes", "drilling", "collars", "collar")
_LABEL_TOOLS = ("label_context", "labels", "nearest_label", "nearest_labels")
_SCORE_TOOLS = ("cell_scores", "scores", "oof_scores", "cell_score_oof", "oof")
_ROW_KEYS = ("feature", "feature_key", "key", "name")


def _tool_is(tool: str, names: tuple[str, ...]) -> bool:
    t = tool.lower()
    return t in names or any(t.startswith(n + "_") or t.endswith("_" + n) for n in names)


def tool_rows(pack: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Rows by tool, whichever of the two shapes the pack has: the builder's `tools: {name: {note, rows}}`, or
    a plain `rows: {name: [...]}`."""
    tools = pack.get("tools")
    if isinstance(tools, dict):
        return {k: list((v or {}).get("rows") or []) if isinstance(v, dict) else list(v or []) for k, v in tools.items()}
    rows = pack.get("rows") or {}
    return {k: list(v) for k, v in rows.items()} if isinstance(rows, dict) else {"rows": list(rows)}


def _with_rows(pack: dict[str, Any], rows: dict[str, list[dict[str, Any]]], values: dict[str, Any]) -> dict[str, Any]:
    """The pack rebuilt in its own shape with these rows and values."""
    out = {**pack, "values": values}
    if isinstance(pack.get("tools"), dict):
        out["tools"] = {k: ({**pack["tools"][k], "rows": rs} if isinstance(pack["tools"].get(k), dict) else rs)
                        for k, rs in rows.items()}
    else:
        out["rows"] = rows
    return out


def _effort_row(row: dict[str, Any]) -> bool:
    """The builder's rule: an `is_effort` flag, or a feature named in the effort set."""
    return bool(row.get("is_effort")) or any(str(row.get(k)) in M.EFFORT_FEATURES for k in _ROW_KEYS if k in row)


def _cited_ids(row: dict[str, Any]) -> set[str]:
    return {v for k, v in row.items() if k.endswith("_id") and isinstance(v, str)}


def _id_kind(vid: str) -> tuple[str, str]:
    """(kind, suffix) of a value id in either scheme: `c:<kind>:<cell>:<suffix>` or `b:<bench>:<kind>:<suffix>`."""
    parts = vid.split(":")
    if len(parts) < 3:
        return "", parts[-1]
    kind = parts[2] if parts[0] == "b" else parts[1]
    return kind, parts[-1]


def _value_hidden(vid: str, sw: Switches) -> bool:
    kind, last = _id_kind(vid)
    if not sw.effort_features and last in M.EFFORT_FEATURES:
        return True
    if not sw.label_context and (kind in ("near", "label", "labels") or "label" in kind):
        return True
    if not sw.oof_scores and kind in ("score", "scores", "oof"):
        return True
    if not sw.drillholes and kind in ("hole", "holes", "drillhole", "drill", "collar"):
        return True
    return False


def apply_switches(pack: dict[str, Any], sw: Switches) -> dict[str, Any]:
    """The pack with every switched-off part removed: tools by name, effort rows by feature, and values both
    by the kind their id carries and because a removed row cited them.

    Pre-rendered `text` is dropped whenever anything was removed, since it would otherwise carry the removed
    rows back in. What is left is exactly what the model may see and cite."""
    rows = tool_rows(pack)
    values = dict(pack.get("values") or {})
    dropped_ids: set[str] = set()
    dropped = False
    for tool in list(rows):
        if (not sw.drillholes and _tool_is(tool, _DRILLHOLE_TOOLS)) \
                or (not sw.label_context and _tool_is(tool, _LABEL_TOOLS)) \
                or (not sw.oof_scores and _tool_is(tool, _SCORE_TOOLS)):
            for r in rows.pop(tool):
                if isinstance(r, dict):
                    dropped_ids |= _cited_ids(r)
            dropped = True
    if not sw.effort_features:
        for tool, rs in rows.items():
            kept = []
            for r in rs:
                if isinstance(r, dict) and _effort_row(r):
                    dropped_ids |= _cited_ids(r)
                    dropped = True
                else:
                    kept.append(r)
            rows[tool] = kept
    for vid in list(values):
        if vid in dropped_ids or _value_hidden(vid, sw):
            del values[vid]
            dropped = True
    out = _with_rows(pack, rows, values)
    if dropped:
        out.pop("text", None)
    return out


# ---------------------------------------------------------------- rendering

def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def render_pack(pack: dict[str, Any]) -> str:
    """The fallback renderer: one section per tool, one line per row, then every value with its id."""
    lines = [f"# Evidence pack for cell {pack.get('bench_id', '?')}", ""]
    for tool, rs in tool_rows(pack).items():
        lines.append(f"## {tool} ({len(rs)} row{'s' if len(rs) != 1 else ''})")
        if not rs:
            lines.append("  (no rows: nothing measured here, which is unknown, not absent)")
        for r in rs:
            lines.append("  - " + (", ".join(f"{k}: {_fmt(v)}" for k, v in r.items()) if isinstance(r, dict)
                                   else _fmt(r)))
        lines.append("")
    values = pack.get("values") or {}
    lines.append(f"## values ({len(values)}): the ids a claim may cite")
    for vid, val in values.items():
        v = val.get("value") if isinstance(val, dict) else val
        unit = (val.get("unit") or "") if isinstance(val, dict) else ""
        note = (val.get("note") or "") if isinstance(val, dict) else ""
        lines.append(f"  - {vid} = {_fmt(v)}{' ' + unit if unit else ''}{'  (' + note + ')' if note else ''}")
    if pack.get("text"):
        lines += ["", "## notes", str(pack["text"])]
    return "\n".join(lines) + "\n"


def pack_text(pack: dict[str, Any]) -> str:
    return _pack_text(pack) if _pack_text is not None else render_pack(pack)


def render_passages(passages: list[dict[str, Any]]) -> str:
    lines = ["# Passages from the assessment record", ""]
    for i, p in enumerate(passages, 1):
        lines.append(f"## passage {i}: file {p.get('file', '?')}, page {p.get('page', '?')}, "
                     f"tier {p.get('tier', '?')}")
        lines += [str(p.get("text", "")).strip(), ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- the request

def context_hash(bench_id: str, arm: ArmConfig) -> str:
    """The bench id and the switches, so two arms over one cell can never share a cached answer, even when a
    switch happens to remove nothing from this particular pack."""
    return short(sha256_json({"bench_id": bench_id, "inputs": asdict(arm.inputs),
                              "switches": asdict(arm.switches)}))


def build_request(
    pack: dict[str, Any], card_path: Path | None, passages: list[dict[str, Any]] | None, arm: ArmConfig,
    prompt_version: str | None = None, stage: Path | None = None, system: str | None = None,
) -> ExtractionRequest:
    """Stage what the arm allows and ask once. `stage` is where the rendered files are written; the backend
    copies them into its own stage under the same names, so the directory only has to outlive the call."""
    stage = stage or Path(tempfile.mkdtemp(prefix="lr_analyst_"))
    stage.mkdir(parents=True, exist_ok=True)
    shown = apply_switches(pack, arm.switches)
    files: list[tuple[Path, str]] = []
    if arm.inputs.pack:
        (stage / PACK_FILE).write_text(pack_text(shown))
        files.append((stage / PACK_FILE, PACK_FILE))
    with_passages = bool(arm.inputs.passages and passages)
    if with_passages:
        (stage / PASSAGES_FILE).write_text(render_passages(passages or []))
        files.append((stage / PASSAGES_FILE, PASSAGES_FILE))
    images = (card_path,) if arm.inputs.card and card_path else ()
    if not images and not files:
        raise ValueError(f"arm {arm.name}: nothing to stage (no card, no pack, no passages)")
    bench_id = str(pack.get("bench_id", "?"))
    return ExtractionRequest(
        task=TASK,
        images=images,
        stage_files=tuple(files),
        system_prompt=system if system is not None else default_system_prompt(),
        user_prompt=user_prompt(bench_id, card=bool(images), pack=arm.inputs.pack, passages=with_passages),
        schema=ANSWER_SCHEMA,
        schema_version=SCHEMA_VERSION,
        prompt_version=prompt_version or arm.prompt_version,
        model=arm.model,
        effort=arm.effort,
        context_hash=context_hash(bench_id, arm),
    )


# ---------------------------------------------------------------- the gate

def gate_context(pack: dict[str, Any], passages: list[dict[str, Any]] | None = None,
                 criteria_text: str | None = None) -> str:
    """What a claim may quote without citing: the strings the pack contains, the passages, the criteria file.

    Only the string leaves of the rows, never the values' numbers: `memo.quotable` explains what happened
    when the whole payload was allowed. A pack's values are what a citation is for."""
    parts = [quotable(pack.get("tools") or pack.get("rows") or {}), quotable(pack.get("text") or "")]
    if passages:
        parts.append(quotable(passages))
    parts.append(criteria_text if criteria_text is not None else CRITERIA_FILE.read_text())
    return "\n".join(parts)


def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def gate(answer: dict[str, Any], pack: dict[str, Any], context: str | None = None) -> list[str]:
    """Problems that make an answer unpublishable. `pack` is the pack the model was shown, after switches."""
    problems: list[str] = []
    verdict = answer.get("verdict")
    if verdict not in VERDICTS:
        problems.append(f"verdict {verdict!r} is not one of {VERDICTS}")
    p = answer.get("probability")
    if not _number(p) or not 0.0 <= float(p) <= 1.0:
        problems.append(f"probability {p!r} is not a number between 0 and 1")
    claims = answer.get("claims")
    if not isinstance(claims, list):
        problems.append("claims must be a list")
        claims = []
    if not claims and verdict != ABSTAIN:
        problems.append(f"a {verdict!r} verdict with no claims rests on nothing")
    for key in ("unknown_criteria", "absent_criteria"):
        if not isinstance(answer.get(key), list):
            problems.append(f"{key} must be a list, even an empty one")
    if not str(answer.get("next_observation") or "").strip():
        problems.append("an answer must name the observation that would change the verdict")
    rationale = str(answer.get("rationale") or "")
    if len(rationale) > RATIONALE_MAX:
        problems.append(f"rationale is {len(rationale)} characters; the limit is {RATIONALE_MAX}")
    values = pack.get("values") or {}
    ctx = context if context is not None else gate_context(pack)
    if _number(p):
        ctx += f"\nstated probability {float(p):g} {float(p) * 100:.0f}"  # the model's own number, not evidence
    problems += check_claims(claims, values, context=ctx)
    cited = [v for c in claims if isinstance(c, dict) for v in (c.get("value_ids") or [])]
    problems += [f"rationale: {m.split(': ', 1)[-1]}" for m in
                 check_claims([{"text": rationale, "value_ids": cited}], values, context=ctx)]
    return list(dict.fromkeys(problems))


# ---------------------------------------------------------------- one cell

def run_cell(
    backend: Any, pack: dict[str, Any], card_path: Path | None, passages: list[dict[str, Any]] | None,
    arm: ArmConfig, stage: Path | None = None,
) -> dict[str, Any]:
    """One call, one gate. Backend errors propagate; the harness decides what a failed cell is."""
    own_stage = stage is None
    stage = stage or Path(tempfile.mkdtemp(prefix="lr_analyst_"))
    try:
        shown = apply_switches(pack, arm.switches)
        req = build_request(pack, card_path, passages, arm, stage=stage)
        response = backend.call(req)
    finally:
        if own_stage:
            shutil.rmtree(stage, ignore_errors=True)
    answer = dict(response.structured or {})
    problems = gate(answer, shown, context=gate_context(shown, passages if arm.inputs.passages else None))
    return {
        "bench_id": str(pack.get("bench_id", "?")),
        "answer": answer,
        "problems": problems,
        "published": not problems,
        "cost_usd": float(response.cost_usd or 0.0),
        "duration_s": float(response.duration_s or 0.0),
        "model_resolved": response.model_resolved,
        "cache_key": response.cache_key,
        "from_cache": bool(getattr(response, "from_cache", False)),
    }

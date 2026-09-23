"""Analyst v2: the evidence read family by family, then ranked in one call.

v0 reads the whole pack in one call. With the raw evidence in it (`prospect.evidence`) the pack is long: a
hundred records or more, of four kinds. This agent splits the reading the way the evidence splits. One call per
family (geochemistry, dispersal, structure, setting) sees only that family's records, features and criteria,
and says what they show for or against a deposit in or near the cell. Then one ranking call sees everything
v0 sees (the whole pack and the card) plus the four readings, and answers exactly as v0 does, under the same
gate.

It is the staged rung of the ladder with the parts our runs and the literature found wanting left out: no LLM
verifier (it mostly turned answers into abstentions), no criterion-node template (the executors re-derived the
criteria table), and no decider but the model's own probability. A weighted sum fitted over the stage
strengths can be computed afterwards from the rows at no model cost. What is kept is the one component with a
record, the deterministic gate: a reading that cites a number it was not shown is refused and asked again once,
told why. The ranking call gets no retry, as v0 gets none, so the difference between the two is the staged
reading and nothing else.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..backends.base import ExtractionRequest
from ..ids import sha256_json, short
from ..prospect.extended import FAMILIES as FEATURE_FAMILIES
from ..prospect.memo import CRITERIA_FILE, HANDBOOK, check_claims
from . import v0 as V0
from .arms import ArmConfig

TASK_STAGE, TASK_RANK = "analyst_v2_reading", "analyst_v2"
SCHEMA_VERSION = "1.0.0"
#: readers write 550 to 800 characters when nothing stops them (the smoke run); 500 turned most first answers
#: into refusals and, as a schema limit, some into CLI errors
SUMMARY_MAX = 1000
READINGS_FILE = "readings.md"
ASSESSMENTS = ("for", "neutral", "against", "unknown")


@dataclass(frozen=True)
class Family:
    """One reader's share of the pack: its evidence tools, its features and its criteria, and what to look at."""

    name: str
    tools: tuple[str, ...]
    features: tuple[str, ...]
    criteria: tuple[str, ...]
    focus: str


FAMILIES: tuple[Family, ...] = (
    Family("geochemistry", ("evidence_geochem",), FEATURE_FAMILIES["geochem"],
           ("lake_sediment_uranium", "lake_water_uranium"),
           "the lake-sediment and lake-water samples: uranium against organic content and against thorium, the "
           "pathfinders lead and nickel, where the anomalous samples lie relative to the cell and to the ice "
           "flow, and how much sampling there is"),
    Family("dispersal", ("evidence_boulders",),
           (*FEATURE_FAMILIES["boulders"], "landform_grain_deg", "grain_coherence", "surficial_class"),
           ("boulder_train",),
           "the ice-flow direction and the radioactive boulders: which lie down-ice of the cell (a possible "
           "source here) and which up-ice (cannot have come from here), their rock types, whether several point "
           "back toward the cell, and the surficial cover that carried them"),
    Family("structure", ("evidence_structure",),
           ("d_conductor_m", "conductor_density", "d_fault_m", "fault_density", *FEATURE_FAMILIES["structure"]),
           ("conductor_proximity", "conductor_strength", "em_bright_spot", "fault_proximity", "structural_density"),
           "the lineaments and conductors: distances and trends, whether conductors run along or across "
           "lineaments, the crossings near the cell, and where no survey reached"),
    Family("setting", ("evidence_bedrock", "region"),
           ("graphitic_host", "graphitic_host_surface", "unconformity_depth_m", "elevation_m", "relief_m",
            "water_fraction", "vegetation_fraction", "bare_fraction",
            *(k for k in FEATURE_FAMILIES["setting"] if not k.startswith("domain_"))),
           ("graphitic_host", "unconformity_depth"),
           "the bedrock and the regional setting: cover or basement, the distance to the edge of the cover, the "
           "depth to the unconformity, host rocks, the basement domains in reach and their rocks, and the "
           "domain boundaries"),
)

READING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assessment", "strength", "claims", "unknowns", "summary"],
    "properties": {
        "assessment": {"type": "string", "enum": list(ASSESSMENTS)},
        "strength": {"type": "number", "minimum": 0, "maximum": 1},
        "claims": V0.ANSWER_SCHEMA["properties"]["claims"],
        "unknowns": {"type": "array", "items": {"type": "string"}},
        # no maxLength here: a schema the answer fails is an error from the CLI with nothing in it, while the
        # gate's length check is a refusal the reader is told about and gets its one retry for
        "summary": {"type": "string"},
    },
}

STAGE_RULES = f"""You are one of four readers of the evidence around one 2 km cell, closed-book, each reading one
family of it for unconformity-related uranium. You read {{family}}. Another call will weigh the four readings
and rank the cell; your job is an accurate reading of your family, for someone who is not a geologist.

Absolute rules, the same for every reader:
1. Only the evidence provided. You do not know where this cell is and must not guess: no place names, no
   deposit or camp names, no knowledge of what was found anywhere.
2. Every number you state must come from the evidence, and the claim that states it must list its value id.
   Observation counts and shares are numbers too. This is checked mechanically; an answer that breaks it is
   refused.
3. You never compute anything: no arithmetic, no conversions. Every ratio, distance and bearing is given.
4. Unknown is not absent. A family with no records here is unknown, and you say so rather than guess.
5. Criteria marked folklore may be named, never used as support.

Answer JSON with exactly these fields: assessment, one of {" | ".join(ASSESSMENTS)}: what your family's evidence
says about a deposit in or near this cell ("unknown" when there is too little of it to say); strength, from 0 to
1: how strongly it points toward one (0 against, 0.5 says nothing either way, 1 strongly toward); claims, each
with text and value_ids; unknowns, what your family cannot tell here and why; summary, at most {SUMMARY_MAX}
characters of prose with no number in it that a claim does not cite."""


def stage_system(family: Family, switches: Any) -> str:
    """A reader's instruction: the stage rules, the handbook's frame and criteria, and the evidence guide."""
    frame = V0._frame(HANDBOOK.read_text())
    parts = [STAGE_RULES.replace("{family}", family.focus), ""]
    if frame:
        parts += ["The mineral-systems frame, restricted to what the public record can support:", *frame, ""]
    parts += ["The criteria, with status, weight and how each one fails:", *V0._criteria(CRITERIA_FILE.read_text()),
              "", V0.EVIDENCE_GUIDE]
    text = "\n".join(parts)
    leaked = V0.place_names_in(text)
    if leaked:
        raise ValueError(f"the closed-book reading prompt would name a place: {leaked}")
    return text


# ---------------------------------------------------------------- a family's share of the pack


def _ids(row: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(row, dict):
        for k, v in row.items():
            if k.endswith("_id") and isinstance(v, str):
                out.add(v)
            else:
                out |= _ids(v)
    elif isinstance(row, list):
        for v in row:
            out |= _ids(v)
    return out


def share(shown: dict[str, Any], family: Family) -> dict[str, Any]:
    """The part of the (already switched) pack one reader sees: its evidence tools whole, its own rows of the
    features, coverage and criteria tables, and exactly the values those rows cite."""
    tools = shown.get("tools") or {}
    out_tools: dict[str, Any] = {}
    for name in family.tools:
        if name in tools:
            out_tools[name] = tools[name]
    for name, key, keep in (("cell_features", "feature", family.features), ("coverage", "feature", family.features),
                            ("criteria_breakdown", "criterion", family.criteria)):
        if name in tools:
            rows = [r for r in (tools[name].get("rows") or []) if r.get(key) in keep]
            out_tools[name] = {**tools[name], "rows": rows}
    cited = _ids(out_tools)
    values = {k: v for k, v in (shown.get("values") or {}).items() if k in cited}
    return {"bench_id": shown.get("bench_id"), "version": shown.get("version"), "tools": out_tools, "values": values}


def stage_user(bench_id: str, family: Family, problems: list[str] | None = None) -> str:
    lines = [f"Cell {bench_id}. Read {family.name}: {family.focus}.", "",
             f"Read first: {{STAGE_DIR}}/{V0.PACK_FILE}   your share of the evidence pack, with the value ids "
             "you must cite", "", "Then answer once, as JSON. Every number needs the value id it came from."]
    if problems:
        lines += ["", "Your previous answer was refused by the check, for these reasons:",
                  *[f"- {p}" for p in problems[:8]],
                  "Answer again: cite an id from your share for every number, and state no number you were not "
                  "given."]
    return "\n".join(lines)


def gate_reading(answer: dict[str, Any], part: dict[str, Any]) -> list[str]:
    """What makes a reading unusable: the shape, and any number not bound to a value in the reader's share."""
    problems: list[str] = []
    if answer.get("assessment") not in ASSESSMENTS:
        problems.append(f"assessment {answer.get('assessment')!r} is not one of {ASSESSMENTS}")
    s = answer.get("strength")
    if not V0._number(s) or not 0.0 <= float(s) <= 1.0:
        problems.append(f"strength {s!r} is not a number between 0 and 1")
    claims = answer.get("claims")
    if not isinstance(claims, list):
        problems.append("claims must be a list")
        claims = []
    summary = str(answer.get("summary") or "")
    if len(summary) > SUMMARY_MAX:
        problems.append(f"summary is {len(summary)} characters; the limit is {SUMMARY_MAX}")
    values = part.get("values") or {}
    ctx = V0.gate_context(part)
    if V0._number(s):
        ctx += f"\nstated strength {float(s):g} {float(s) * 100:.0f}"
    problems += check_claims(claims, values, context=ctx)
    cited = [v for c in claims if isinstance(c, dict) for v in (c.get("value_ids") or [])]
    problems += [f"summary: {m.split(': ', 1)[-1]}" for m in
                 check_claims([{"text": summary, "value_ids": cited}], values, context=ctx)]
    return list(dict.fromkeys(problems))


# ---------------------------------------------------------------- the calls


def _request(task: str, system: str, user: str, schema: dict[str, Any], files: list[tuple[Path, str]],
             images: tuple[Path, ...], arm: ArmConfig, context: dict[str, Any]) -> ExtractionRequest:
    return ExtractionRequest(
        task=task, images=images, stage_files=tuple(files), system_prompt=system, user_prompt=user, schema=schema,
        schema_version=SCHEMA_VERSION, prompt_version=arm.prompt_version, model=arm.model, effort=arm.effort,
        context_hash=short(sha256_json({"switches": arm.switches.keyed(), "inputs": arm.inputs.__dict__,
                                        **context})),
    )


def read_family(backend: Any, shown: dict[str, Any], family: Family, arm: ArmConfig, stage: Path,
                sample: int = 0) -> dict[str, Any]:
    """One reader, gated, with one retry told what failed. Returns the reading and its accounting."""
    bench_id = str(shown.get("bench_id", "?"))
    part = share(shown, family)
    d = stage / family.name
    d.mkdir(parents=True, exist_ok=True)
    (d / V0.PACK_FILE).write_text(V0.pack_text(part))
    system = stage_system(family, arm.switches)
    out: dict[str, Any] = {"family": family.name, "attempts": 0, "cost_usd": 0.0, "duration_s": 0.0,
                           "from_cache": True, "answer": None, "problems": [], "published": False,
                           "n_values": len(part["values"])}
    problems: list[str] | None = None
    for attempt in (1, 2):
        req = _request(TASK_STAGE, system, stage_user(bench_id, family, problems), READING_SCHEMA,
                       [(d / V0.PACK_FILE, V0.PACK_FILE)], (), arm,
                       {"bench_id": bench_id, "family": family.name, "attempt": attempt,
                        **({"sample": int(sample)} if sample else {})})
        resp = backend.call(req)
        out["attempts"] = attempt
        out["cost_usd"] += float(resp.cost_usd or 0.0)
        out["duration_s"] += float(resp.duration_s or 0.0)
        out["from_cache"] = out["from_cache"] and bool(getattr(resp, "from_cache", False))
        answer = dict(resp.structured or {})
        problems = gate_reading(answer, part)
        out["answer"], out["problems"] = answer, problems
        if not problems:
            out["published"] = True
            break
    return out


def readings_text(readings: list[dict[str, Any]]) -> str:
    """The four readings as the ranking call sees them. A refused reading is named and left empty: nothing it
    said may be used."""
    lines = ["# Four readings of this cell's evidence, one per family", "",
             "Each reader saw only its own family. They can be wrong; check them against the pack."]
    for r in readings:
        a = r.get("answer") or {}
        lines.append("")
        if not r.get("published"):
            lines += [f"## {r['family']}: refused by the check, not usable"]
            continue
        lines += [f"## {r['family']}: {a.get('assessment')}, strength {a.get('strength')}",
                  f"summary: {a.get('summary', '')}"]
        for c in a.get("claims") or []:
            lines.append(f"- {c.get('text')}  [{', '.join(c.get('value_ids') or [])}]")
        if a.get("unknowns"):
            lines.append("unknown here: " + "; ".join(str(u) for u in a["unknowns"]))
    return "\n".join(lines) + "\n"


def rank_user(bench_id: str, card: bool) -> str:
    """v0's instruction with the readings file listed beside the pack."""
    lines = V0.user_prompt(bench_id, card=card, pack=True, passages=False).split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith("Then answer")) - 1
    lines.insert(at, f"  {{STAGE_DIR}}/{READINGS_FILE}   four readings of the evidence, one per family; check "
                     "them against the pack")
    return "\n".join(lines)


def run_cell(backend: Any, pack: dict[str, Any], card_path: Path | None, arm: ArmConfig,
             stage: Path | None = None, sample: int = 0) -> dict[str, Any]:
    """Four readings, then the ranking call and v0's gate. Backend errors propagate, as in v0."""
    own_stage = stage is None
    stage = stage or Path(tempfile.mkdtemp(prefix="ue_analyst_v2_"))
    try:
        shown = V0.apply_switches(pack, arm.switches)
        bench_id = str(shown.get("bench_id", "?"))
        readings = [read_family(backend, shown, f, arm, stage, sample) for f in FAMILIES]
        (stage / V0.PACK_FILE).write_text(V0.pack_text(shown))
        (stage / READINGS_FILE).write_text(readings_text(readings))
        images = (card_path,) if arm.inputs.card and card_path else ()
        req = _request(TASK_RANK, V0.system_for(arm.switches), rank_user(bench_id, bool(images)), V0.ANSWER_SCHEMA,
                       [(stage / V0.PACK_FILE, V0.PACK_FILE), (stage / READINGS_FILE, READINGS_FILE)], images, arm,
                       {"bench_id": bench_id, "readings": sha256_json([r.get("answer") for r in readings]),
                        **({"sample": int(sample)} if sample else {})})
        resp = backend.call(req)
    finally:
        if own_stage:
            shutil.rmtree(stage, ignore_errors=True)
    answer = dict(resp.structured or {})
    problems = V0.gate(answer, shown, context=V0.gate_context(shown))
    cost = float(resp.cost_usd or 0.0) + sum(r["cost_usd"] for r in readings)
    return {
        "bench_id": bench_id,
        "answer": answer,
        "problems": problems,
        "published": not problems,
        "cost_usd": cost,
        "duration_s": float(resp.duration_s or 0.0) + sum(r["duration_s"] for r in readings),
        "model_resolved": resp.model_resolved,
        "cache_key": resp.cache_key,
        "from_cache": bool(getattr(resp, "from_cache", False)) and all(r["from_cache"] for r in readings),
        "readings": {r["family"]: {"assessment": (r["answer"] or {}).get("assessment"),
                                   "strength": (r["answer"] or {}).get("strength"), "published": r["published"],
                                   "attempts": r["attempts"], "problems": r["problems"],
                                   "claims": len((r["answer"] or {}).get("claims") or []),
                                   "n_values": r["n_values"], "cost_usd": r["cost_usd"]} for r in readings},
    }


def prompt_hashes(switches: Any) -> dict[str, str]:
    """The prompts a v2 run uses, hashed for its manifest."""
    return {**{f"reading/{f.name}": sha256_json(stage_system(f, switches)) for f in FAMILIES},
            "rank": sha256_json(V0.system_for(switches))}


def schema_hashes() -> dict[str, str]:
    return {f"reading/{SCHEMA_VERSION}": sha256_json(READING_SCHEMA),
            f"answer/{V0.SCHEMA_VERSION}": sha256_json(V0.ANSWER_SCHEMA)}

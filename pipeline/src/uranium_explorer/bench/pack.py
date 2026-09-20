"""Evidence packs: what a v0 agent sees about a cell, with everything that says *where* taken out.

A pack is the tool results the panel's tools return — features, the criteria breakdown, coverage — assembled
once, anonymised, and frozen. Two transformations make it a benchmark item rather than a dump:

* **Anonymisation.** The cell id becomes a bench id everywhere, and every value id is rewritten from
  `c:<kind>:<cell>:<suffix>` to `b:<bench>:<kind>:<suffix>`. The suffix survives so the fabrication gate can
  still bind a claim to the value it cites; the cell does not survive, so a model cannot look the ground up.
* **Scrubbing.** Coordinates, NTS sheets, file numbers, company, property and deposit names are removed from
  keys and from text. `scrub` is a pure function over the payload and is tested on its own, because a leak
  here is a leak into every pack.

Effort features are dropped unless the spec turns them on: hole and survey counts predict the labels better
than geology on this grid, and a benchmark that hands them over is measuring where people looked.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import pandas as pd

from ..filenum import _FIND as FILE_NUMBER
from ..prospect import models as M
from ..prospect import tools as T
from ..values import stat
from .spec import BenchSpec

#: keys whose values place or name the ground; dropped wherever they occur in a pack or a passage
FORBIDDEN_KEYS = frozenset({
    "lon", "lat", "cx", "cy", "x", "y", "easting", "northing", "nts", "nts_sheet", "nts_sheets", "file_num",
    "file", "citation", "doc_name", "doc_sha256", "company", "property", "label_name", "name", "hole_names",
    "hole_id", "cell_id", "camp_id", "block_id", "grid_id",
})
#: text fields that are public literature, identical for every cell, and so carry no cell-specific name
KEEP_TEXT = frozenset({"evidence", "caveat"})

#: what a scrubbed token is replaced with
REDACTED = "[redacted]"

#: decimal degrees inside the study region (about -112..-101, 55..60) and a UTM zone 13 northing there
COORDINATE = re.compile(r"(?<![\d.\-])(?:-1(?:0[1-9]|1[0-2])\.\d{3,}|5[5-9]\.\d{3,}|6[3-7]\d{5}(?:\.\d+)?)(?!\d)")
#: an NTS sheet at 1:50,000 (74H09) or with separators (74-H-09)
NTS_SHEET = re.compile(r"(?<![A-Z0-9])\d{2}-?[A-P]-?\d{2}(?![A-Z0-9])")
#: the projected northing range a bare number in a pack may not fall in
NORTHING = (6_300_000.0, 6_700_000.0)
#: a grid cell id (`0123_0045`), whichever cell it names
CELL_ID = re.compile(r"(?<![\d_])\d{4}_\d{4}(?![\d_])")

PATTERNS: dict[str, re.Pattern[str]] = {"file": FILE_NUMBER, "nts": NTS_SHEET, "coordinate": COORDINATE,
                                       "cell id": CELL_ID}

#: words that are somebody's name only when joined to another word
_GENERIC = frozenset("""
uranium mines mining mine resources exploration explorations energy minerals mineral canada canadian oil gold
metals northern western eastern southern saskatchewan power international ltd limited inc corp corporation
company co partners partnership holdings group ventures development developments enterprises services the of
and joint venture jv operator tenure holder partner
""".split())


# ---------------------------------------------------------------- value ids


def rewrite_id(vid: str, cell_id: str, bench_id: str) -> str:
    """`c:cell:0123_0045:d_conductor_m` -> `b:b-0001:cell:d_conductor_m`; ids without the cell keep their suffix."""
    parts = vid.split(":")
    if parts[0] != "c":
        return vid
    rest = [p for p in parts[1:] if p != cell_id]
    return ":".join(["b", bench_id, *rest])


def anonymise(payload: Any, cell_id: str, bench_id: str) -> Any:
    """The bench id in place of the cell id everywhere: value ids, registry keys, and prose."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            key = rewrite_id(k, cell_id, bench_id) if k.startswith("c:") else k
            out[key] = anonymise(v, cell_id, bench_id)
        return out
    if isinstance(payload, list):
        return [anonymise(v, cell_id, bench_id) for v in payload]
    if isinstance(payload, str):
        if payload.startswith("c:"):
            return rewrite_id(payload, cell_id, bench_id)
        return payload.replace(cell_id, bench_id)
    return payload


# ---------------------------------------------------------------- scrubbing


@lru_cache(maxsize=8)
def name_pattern(names: frozenset[str]) -> re.Pattern[str] | None:
    words = sorted((n for n in names if len(n) >= 4), key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?![A-Za-z0-9])", re.I)


def scrub_text(text: str, forbidden: set[str] | frozenset[str]) -> str:
    """Forbidden strings, file numbers, NTS sheets and coordinates replaced in one string."""
    pat = name_pattern(frozenset(forbidden))
    if pat is not None:
        text = pat.sub(REDACTED, text)
    for p in PATTERNS.values():
        text = p.sub(REDACTED, text)
    return text


def scrub(payload: Any, forbidden: set[str] | frozenset[str], keep: frozenset[str] = KEEP_TEXT) -> Any:
    """Drop forbidden keys everywhere and scrub every string. Numbers pass through: the keys that could carry
    a coordinate are dropped, and the audit checks the rest. Pure; returns a new payload."""
    def walk(node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items() if k not in FORBIDDEN_KEYS}
        if isinstance(node, list):
            return [walk(v, key) for v in node]
        if isinstance(node, str):
            return node if key in keep else scrub_text(node, forbidden)
        return node

    return walk(payload)


def company_names(raw: str | None) -> set[str]:
    """Names inside a provincial company string: role tags stripped, joint ventures split, and each name's
    core (without LTD, CORPORATION and the like) added when it is not just a generic word."""
    if not raw:
        return set()
    text = str(raw)
    while True:  # nested parentheses: "(JV: CAMECO (75%)-UEM INC (25%))"
        stripped = re.sub(r"\([^()]*\)", " ", text)
        if stripped == text:
            break
        text = stripped
    # what an unbalanced bracket leaves behind ("(JV: CAMECO -COGEMA") is still two names
    text = re.sub(r"[()]|\bJV:", " ", text)
    out: set[str] = set()
    for part in re.split(r"[;/]|\s-|-\s", text):
        name = re.sub(r"\s+", " ", part).strip(" ,.-:")
        if len(name) < 4:
            continue
        out.add(name)
        core = [w for w in name.replace(".", "").split(" ") if w.lower() not in _GENERIC]
        if core and (len(core) > 1 or len(core[0]) >= 4):
            out.add(" ".join(core))
    return out


#: single words that are place-name parts or directions, never a name on their own: a property called
#: "East" would otherwise flag every "eastern" sentence in the criteria table
NAME_STOPWORDS = frozenset({
    "east", "west", "north", "south", "eastern", "western", "northern", "southern", "central", "upper", "lower",
    "lake", "lakes", "river", "creek", "bay", "hill", "hills", "point", "island", "zone", "trend", "block",
    "main", "group", "project", "claim", "claims", "property", "area", "basin", "shield", "uranium", "mine",
    "camp", "grid", "line", "extension", "deposit", "showing", "prospect", "option", "joint", "venture",
})


def usable_name(name: str) -> bool:
    """A name worth scrubbing for: multiword; an upper-case acronym of three letters or more (SMDC, UEM); or a
    single word of five or more letters that is not a stopword."""
    words = name.split()
    if not words:
        return False
    if len(words) == 1:
        w = words[0]
        if w.isupper() and len(w) >= 3:
            return True
        return len(w) >= 5 and w.lower() not in NAME_STOPWORDS
    return True


def forbidden_strings(con: Any) -> set[str]:
    """Every company, property and deposit name the store knows: the strings no pack may carry."""
    out: set[str] = set()
    rows = con.execute("select company, property from native.corpus_file").fetchall()
    for company, prop in rows:
        out |= company_names(company)
        if prop and len(str(prop).strip()) >= 4:
            out.add(str(prop).strip())
    labels = con.execute("select distinct label_name from derived.cell_label where label_name is not null").fetchall()
    out |= {str(n).strip() for (n,) in labels if n and len(str(n).strip()) >= 4}
    return {n for n in out if usable_name(n)}


def hole_names(con: Any) -> set[str]:
    """Hole names the provincial index lists per file, for scrubbing passages."""
    out: set[str] = set()
    for (names,) in con.execute("select hole_names from native.corpus_file where hole_names is not null").fetchall():
        out |= {n.strip() for n in str(names).split(",") if len(n.strip()) >= 4}
    return out


# ---------------------------------------------------------------- the pack


def build_pack(cell_id: str, bench_id: str, spec: BenchSpec, con: Any = None,
               switches: dict[str, bool] | None = None, forbidden: set[str] | None = None,
               oof: pd.DataFrame | None = None, shared: dict[str, Any] | None = None) -> dict[str, Any]:
    """The anonymised tool results for one cell.

    `switches` override the spec's `[pack]` section; `forbidden` is the name set (read from `con` when not
    given); `oof` is the out-of-fold score table when the switch is on (read from `con` otherwise); `shared`
    may carry a `coverage` result so a build computes the grid-wide table once."""
    sw = {**spec.pack.switches(), **(switches or {})}
    shared = shared or {}
    results: dict[str, T.ToolResult] = {
        "cell_features": T.cell_features(cell_id),
        "criteria_breakdown": T.criteria_breakdown(cell_id),
        "coverage": shared.get("coverage") or T.coverage(),
    }
    if sw.get("label_context"):
        results["label_context"] = T.label_context(cell_id)
    if sw.get("oof_scores"):
        results["cell_scores"] = oof_result(cell_id, con=con, oof=oof)
    if not sw.get("effort_features"):
        results["cell_features"] = drop_effort(results["cell_features"])
        results["coverage"] = drop_effort(results["coverage"])
    values: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    for name, r in results.items():
        tools[name] = {"note": r.note, "rows": r.rows}
        values |= r.values
    pack = {"bench_id": bench_id, "version": spec.version, "switches": sw, "tools": tools, "values": values}
    pack = anonymise(pack, cell_id, bench_id)
    names = set(forbidden) if forbidden is not None else (forbidden_strings(con) if con is not None else set())
    names.add(cell_id)
    return scrub(pack, names)


def drop_effort(result: T.ToolResult) -> T.ToolResult:
    """The tool result without its effort rows and without the values only those rows cite."""
    keep_rows, drop_ids = [], set()
    for row in result.rows:
        effort = bool(row.get("is_effort")) or row.get("feature") in M.EFFORT_FEATURES
        if effort:
            drop_ids |= {v for k, v in row.items() if k.endswith("_id") and isinstance(v, str)}
        else:
            keep_rows.append(row)
    values = {k: v for k, v in result.values.items() if k not in drop_ids}
    return T.ToolResult(result.tool, result.args, keep_rows, values, result.note)


def oof_result(cell_id: str, con: Any = None, oof: pd.DataFrame | None = None) -> T.ToolResult:
    """Out-of-fold scores as a tool result, in place of the served `cell_scores` (which saw every label)."""
    if oof is not None:
        rows = oof[oof["cell_id"] == cell_id][["model", "fold", "score"]].itertuples(index=False, name=None)
    elif con is not None:
        rows = con.execute("select model, fold, score from derived.cell_score_oof where cell_id = ? order by model",
                           [cell_id]).fetchall()
    else:
        rows = []
    out = T.ToolResult("cell_scores", {"cell_id": cell_id})
    for model, fold, score in sorted(rows, key=lambda r: str(r[0])):
        row: dict[str, Any] = {"model": str(model), "fold": int(fold), "out_of_fold": True}
        if score is not None and score == score:
            vid = f"c:score:{cell_id}:{model}"
            out.values[vid] = stat(vid, round(float(score), 4), fmt="ratio3",
                                   note=f"{model} out-of-fold score for cell {cell_id}")
            row["score_id"] = vid
            row["score"] = round(float(score), 4)
        else:
            row["score"] = None
            row["missing"] = "no fold model could score this cell"
        out.rows.append(row)
    out.note = "Each score comes from a model fitted on the other spatial folds; none saw this cell's label."
    return out


# ---------------------------------------------------------------- text rendering


def pack_text(pack: dict[str, Any]) -> str:
    """A compact, stable rendering for a prompt: one line per feature, the criteria table, the coverage flags."""
    lines = [f"bench cell {pack['bench_id']}",
             "every number has an id: a value's id is its row's first column; a count or share carries its id in "
             "square brackets right after it. Cite the id of every number you state."]
    tools = pack.get("tools", {})
    feats = sorted(tools.get("cell_features", {}).get("rows", []), key=lambda r: str(r.get("feature")))

    def count(r: dict[str, Any]) -> str:
        n = r.get("observations", 0)
        oid = r.get("observations_id")
        return f"{n} [{oid}]" if oid else str(n)

    if feats:
        lines.append("features: id | value | unit | observations [count id] | status")
        for r in feats:
            if r.get("value") is None and r.get("text"):
                # a mapped class rather than a number (surficial environment): known, quoted as text
                lines.append(f"  {r.get('value_id') or r.get('feature')} | {r['text']} | - | {count(r)} | known (text)")
                continue
            if r.get("value") is None:
                status = "unknown"
                near = r.get("nearest_observation_id")
                if near:
                    status += f" (nearest observation id {near})"
                lines.append(f"  {r.get('value_id') or r.get('feature')} | - | - | {count(r)} | {status}")
            else:
                lines.append(f"  {r['value_id']} | {r['value']} | {r.get('unit') or '-'} | {count(r)} | known")
    crit = sorted(tools.get("criteria_breakdown", {}).get("rows", []),
                  key=lambda r: (-float(r.get("weight", 0)), str(r.get("criterion"))))
    if crit:
        lines.append("criteria: criterion | state | membership | weight | status | id")
        for r in crit:
            mem = "-" if r.get("membership") is None else r["membership"]
            lines.append(f"  {r.get('criterion')} | {r.get('state')} | {mem} | {r.get('weight')} | {r.get('status')} | "
                         f"{r.get('membership_id') or '-'}")
    cov = sorted(tools.get("coverage", {}).get("rows", []), key=lambda r: str(r.get("feature")))
    if cov:
        lines.append("coverage: feature | share of grid [share id] | thin")
        for r in cov:
            share = f"{r.get('coverage')} [{r['coverage_id']}]" if r.get("coverage_id") else str(r.get("coverage"))
            lines.append(f"  {r.get('feature')} | {share} | {'thin' if r.get('thin') else '-'}")
    scores = tools.get("cell_scores", {}).get("rows", [])
    if scores:
        lines.append("out-of-fold scores: model | score | id")
        for r in sorted(scores, key=lambda r: str(r.get("model"))):
            lines.append(f"  {r.get('model')} | {'-' if r.get('score') is None else r['score']} | {r.get('score_id') or '-'}")
    near = tools.get("label_context", {}).get("rows", [])
    if near:
        lines.append("nearest labels: rank | tier | km | id")
        for r in near:
            lines.append(f"  {r.get('rank')} | {r.get('tier')} | {r.get('distance_km')} | {r.get('distance_km_id')}")
    return "\n".join(lines)


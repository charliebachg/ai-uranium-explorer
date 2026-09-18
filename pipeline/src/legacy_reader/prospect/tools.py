"""The tools the agent panel may call, and the only numbers it may use.

Every tool is deterministic Python against the store. The model never computes geometry or arithmetic: that
boundary exists because GIS agents are documented to skip reprojection steps and because a prospectivity
assistant that was restricted to tool-computed scores still fabricated a number in 1 of 150 rated responses.

Every number a tool returns arrives inside a `Val` with an id. The memo may only print numbers by citing those
ids, and the fabrication gate re-checks each one against this registry before a memo is published. A number
that is not in the registry cannot reach the page, whatever the model writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..store import connect
from ..values import stat
from .criteria import load as load_criteria
from .retrieve import retrieve as retrieve_passages

TOOL_VERSION = "prospect/tools/v1"


@dataclass
class ToolResult:
    """What one tool call returns: prose rows for the model, and the values it may cite."""

    tool: str
    args: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "note": self.note, "rows": self.rows,
                "values": self.values}


def _vals(*items: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["id"]: v for v in items}


def _fmt_for(unit: str | None, value: float) -> str:
    if unit in ("m", "km2", "cps", "ppm"):
        return "m1"
    if float(value).is_integer():
        return "int"
    return "m2"


# ---------------------------------------------------------------- tools


def cell_features(cell_id: str) -> ToolResult:
    """Every feature computed for a cell, with its observation count and what it was built from."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select f.feature_key, f.value, f.value_text, f.unit, f.n_obs, f.nearest_m, s.title, "
            "s.bears_on, s.is_effort, s.notes "
            "from derived.cell_feature f left join derived.feature_spec s using (feature_key) "
            "where f.cell_id = ? order by s.is_effort, f.feature_key", [cell_id]
        ).fetchall()
    finally:
        con.close()
    out = ToolResult("cell_features", {"cell_id": cell_id})
    for key, value, text, unit, n_obs, nearest, title, bears_on, is_effort, notes in rows:
        row: dict[str, Any] = {
            "feature": key, "title": title, "bears_on": bears_on, "is_effort": bool(is_effort),
            "observations": int(n_obs or 0), "note": notes,
        }
        if n_obs:
            obs_vid = f"c:cell:{cell_id}:{key}:n_obs"
            out.values |= _vals(stat(obs_vid, int(n_obs),
                                     note=f"observations behind {title or key} at cell {cell_id}"))
            row["observations_id"] = obs_vid
        if value is not None:
            vid = f"c:cell:{cell_id}:{key}"
            out.values |= _vals(stat(vid, round(float(value), 4), fmt=_fmt_for(unit, value),
                                     unit=unit, note=f"{title or key} for cell {cell_id}"))
            row["value_id"] = vid
            row["value"] = round(float(value), 4)
            row["unit"] = unit
        else:
            row["value"] = None
            row["missing"] = "no observation; this is not a low value"
        if text:
            row["text"] = text
        if nearest is not None and value is None:
            vid = f"c:cell:{cell_id}:{key}:nearest_m"
            out.values |= _vals(stat(vid, round(float(nearest), 1), fmt="m1", unit="m",
                                     note=f"distance from cell {cell_id} to the nearest {key} observation"))
            row["nearest_observation_id"] = vid
        out.rows.append(row)
    out.note = ("A null value means nobody measured it here, which is not the same as a low reading. "
                "Effort features describe where people looked, not what is in the rock.")
    return out


def cell_scores(cell_id: str) -> ToolResult:
    """The three scores for a cell, with how much of each is actually known."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select model, score, known_share, in_aoa, params from derived.cell_score where cell_id = ? "
            "order by model", [cell_id]
        ).fetchall()
        metrics = con.execute(
            "select metric_key, value from derived.metric where metric_key like '%.spatial.%' "
            "order by metric_key"
        ).fetchall()
    finally:
        con.close()
    out = ToolResult("cell_scores", {"cell_id": cell_id})
    for model, score, known, in_aoa, params in rows:
        row: dict[str, Any] = {"model": model, "in_area_of_applicability": bool(in_aoa)}
        if score is not None:
            vid = f"c:score:{cell_id}:{model}"
            out.values |= _vals(stat(vid, round(float(score), 4), fmt="ratio3",
                                     note=f"{model} score for cell {cell_id}"))
            row["score_id"] = vid
            row["score"] = round(float(score), 4)
        else:
            row["score"] = None
            row["missing"] = "too little is known here to score honestly"
        if known is not None:
            known_vid = f"c:score:{cell_id}:{model}:known"
            out.values |= _vals(stat(known_vid, round(float(known), 3), fmt="ratio3",
                                     note=f"share of the {model} score's inputs actually measured here"))
            row["known_share_id"] = known_vid
            row["known_share"] = round(float(known), 3)
        try:
            row["model_note"] = json.loads(params or "{}").get("aoa_note") or ""
        except json.JSONDecodeError:
            pass
        out.rows.append(row)
    for key, value in metrics:
        vid = f"c:metric:{key}"
        out.values |= _vals(stat(vid, round(float(value), 4), fmt="ratio3",
                                 note=f"{key}, measured under spatial folds"))
    out.note = ("Under spatial folds the exploration-effort model outscores the geological one on this grid. "
                "A high learned score is therefore weak evidence about rock and strong evidence about where "
                "people have already worked.")
    return out


def criteria_breakdown(cell_id: str) -> ToolResult:
    """What each targeting criterion contributed here, with its evidence, its caveat and its status."""
    cs = load_criteria()
    by_key = {c.key: c for c in cs.criteria}
    con = connect(read_only=True)
    try:
        rows = con.execute(
            "select criterion, membership, weight, contribution from derived.cell_criterion "
            "where cell_id = ? order by weight desc, criterion", [cell_id]
        ).fetchall()
    finally:
        con.close()
    out = ToolResult("criteria_breakdown", {"cell_id": cell_id})
    for key, membership, weight, contribution in rows:
        c = by_key.get(key)
        row: dict[str, Any] = {
            "criterion": key, "title": c.title if c else key, "element": c.element if c else "",
            "status": c.status if c else "", "weight": float(weight),
            "weight_id": f"c:crit:{cell_id}:{key}:weight",
            "evidence": c.evidence if c else "", "caveat": c.caveat if c else "",
        }
        out.values |= _vals(stat(f"c:crit:{cell_id}:{key}:weight", round(float(weight), 2), fmt="m2",
                                 note=f"weight carried by {key}"))
        # The thresholds are what the argument is actually about ("met, but only against an assumed 500 m
        # cut-off"), so they are values like any other rather than prose the memo has to quote from a file.
        for param, pval in sorted((c.params if c else {}).items()):
            pvid = f"c:crit:{cell_id}:{key}:{param}"
            out.values |= _vals(stat(pvid, round(float(pval), 4), fmt=_fmt_for(None, float(pval)),
                                     note=f"{param} threshold for {key}, status {c.status if c else ''}"))
            row.setdefault("thresholds", {})[param] = {"value": round(float(pval), 4), "value_id": pvid}
        if membership is None:
            row["state"] = "unknown"
            row["note"] = "no value for this criterion here: unknown, not absent"
        else:
            vid = f"c:crit:{cell_id}:{key}"
            out.values |= _vals(stat(vid, round(float(membership), 3), fmt="ratio3",
                                     note=f"{key} membership for cell {cell_id}"))
            row["membership_id"] = vid
            row["membership"] = round(float(membership), 3)
            row["state"] = "met" if membership >= 0.5 else "not met"
        if c and c.status == "folklore":
            row["note"] = ("recorded as personal communication with no published test; carried at weight zero "
                           "and may be named but never counted")
        out.rows.append(row)
    out.note = "Unknown and absent are different answers, and the adjudicator is required to tell them apart."
    return out


def label_context(cell_id: str, radius_km: float = 25.0) -> ToolResult:
    """How close the nearest known deposit or occurrence is: the first thing a skeptic should ask."""
    con = connect(read_only=True)
    try:
        here = con.execute("select lon, lat from derived.cell where cell_id = ?", [cell_id]).fetchone()
        if not here:
            return ToolResult("label_context", {"cell_id": cell_id}, note="no such cell")
        rows = con.execute(
            """
            select l.label_tier, l.label_name, c.lon, c.lat,
                   6371.0 * 2 * asin(sqrt(
                     pow(sin(radians(c.lat - ?) / 2), 2) +
                     cos(radians(?)) * cos(radians(c.lat)) * pow(sin(radians(c.lon - ?) / 2), 2))) as km
            from derived.cell_label l join derived.cell c using (cell_id)
            where l.label_tier <> 'unlabelled'
            order by km limit 5
            """,
            [here[1], here[1], here[0]],
        ).fetchall()
    finally:
        con.close()
    out = ToolResult("label_context", {"cell_id": cell_id, "radius_km": radius_km})
    for i, (tier, name, _lon, _lat, km) in enumerate(rows):
        vid = f"c:near:{cell_id}:{i}"
        out.values |= _vals(stat(vid, round(float(km), 2), fmt="m2", unit="km",
                                 note=f"distance from cell {cell_id} to {tier} {name or '(unnamed)'}"))
        out.rows.append({"rank": i + 1, "tier": tier, "name": name or "(unnamed)",
                         "distance_km_id": vid, "distance_km": round(float(km), 2)})
    out.note = ("A cell beside a known deposit will score well for reasons that have nothing to do with its "
                "own evidence. This is the leakage check, not a recommendation.")
    return out


def coverage(feature_key: str | None = None) -> ToolResult:
    """How much of the grid each feature actually covers: what a silent absence really means."""
    from .readiness import table as readiness_table

    df = readiness_table()
    if feature_key:
        df = df[df["feature_key"] == feature_key]
    out = ToolResult("coverage", {"feature_key": feature_key})
    for _, r in df.iterrows():
        key = str(r["feature_key"])
        vid = f"c:cov:{key}"
        out.values |= _vals(stat(vid, round(float(r["coverage"] or 0), 3), fmt="ratio3",
                                 note=f"share of cells with an observation behind {key}"))
        out.rows.append({"feature": key, "coverage_id": vid, "coverage": round(float(r["coverage"] or 0), 3),
                         "thin": bool(r["thin"]), "is_effort": bool(r["is_effort"])})
    out.note = "A thin feature cannot carry a basin-wide argument on its own."
    return out


def retrieve(query: str, cell_id: str | None = None, k: int = 6, radius_km: float = 40.0) -> ToolResult:
    """Passages from the assessment corpus about this ground, most trustworthy tier first."""
    lon = lat = None
    if cell_id:
        con = connect(read_only=True)
        try:
            row = con.execute("select lon, lat from derived.cell where cell_id = ?", [cell_id]).fetchone()
        finally:
            con.close()
        if row:
            lon, lat = float(row[0]), float(row[1])
    passages = retrieve_passages(query, lon=lon, lat=lat, radius_km=radius_km, k=k)
    out = ToolResult("retrieve", {"query": query, "cell_id": cell_id, "k": k, "radius_km": radius_km})
    for i, p in enumerate(passages):
        row: dict[str, Any] = {
            "tier": p.tier, "citation": p.cite(), "file": p.file_num, "page": p.page,
            "distance_km": p.distance_km, "text": p.text,
            "quotable": p.quotable, "numbers_allowed": p.carries_numbers,
            "value_id": p.value_id,
        }
        if p.page is not None:
            row["page_id"] = f"c:pass:{i}:page"
            out.values |= _vals(stat(f"c:pass:{i}:page", int(p.page),
                                     note=f"page of file {p.file_num} this passage is on"))
        if p.distance_km is not None:
            row["distance_km_id"] = f"c:pass:{i}:km"
            out.values |= _vals(stat(f"c:pass:{i}:km", round(float(p.distance_km), 2), fmt="m2", unit="km",
                                     note=f"distance from the cell to file {p.file_num}"))
        out.rows.append(row)
    out.note = ("Tier `extracted` carries a page, a box and a located quote, and is the only tier a number may "
                "be taken from. Tier `page` is the text layer of a scan, usually somebody's OCR pass, so it is "
                "evidence of what the page says and not that any number in it is right. Tier `metadata` is the "
                "provincial index row and is context only.")
    return out


REGISTRY: dict[str, Callable[..., ToolResult]] = {
    "cell_features": cell_features,
    "cell_scores": cell_scores,
    "criteria_breakdown": criteria_breakdown,
    "label_context": label_context,
    "coverage": coverage,
    "retrieve": retrieve,
}

#: what the model is told it may call, in the prompt
TOOL_HELP = {
    "cell_features": "cell_features(cell_id) - every measured feature here, with its observation count",
    "cell_scores": "cell_scores(cell_id) - the criteria, learned and effort scores, and the model metrics",
    "criteria_breakdown": "criteria_breakdown(cell_id) - what each targeting criterion contributed, with its "
                          "evidence, caveat and whether it is published, assumed or folklore",
    "label_context": "label_context(cell_id) - distance to the nearest known deposit or occurrence",
    "coverage": "coverage(feature_key=None) - how much of the grid a feature covers",
    "retrieve": "retrieve(query, cell_id, k) - passages from the assessment corpus about this ground",
}


class ToolError(Exception):
    """The model asked for a tool that does not exist, or asked for it wrongly."""


def call(tool: str, args: dict[str, Any]) -> ToolResult:
    """Run one tool call. Unknown tools and bad arguments fail loudly rather than returning nothing."""
    fn = REGISTRY.get(tool)
    if fn is None:
        raise ToolError(f"no tool named {tool!r}; available: {', '.join(sorted(REGISTRY))}")
    try:
        return fn(**args)
    except TypeError as err:
        raise ToolError(f"{tool}: {err}") from err

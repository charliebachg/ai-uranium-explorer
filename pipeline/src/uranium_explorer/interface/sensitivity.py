"""What would change the reading: a sensitivity over the criteria table, computed and never reasoned.

The criteria score is a weighted mean over the criteria that have a value: `sum(w * m) / sum(w)` over the
known, counted criteria, and null when the known weight is under `min_known_weight` of the total. So for each
criterion that is unknown here, the arithmetic of "measure it and find it met" is fixed by the weights and the
memberships the store already holds:

    score_if_met      = (known_total + w) / (known_weight + w)
    score_if_not_met  =  known_total      / (known_weight + w)
    delta_if_met      =  score_if_met - score_now

Every number here is a value with an id under `c:sens:<cell>:...`, and the rows are ranked by how far the
score would move if the criterion were measured and met, the largest first. A cell too little known to score
at all is handled the same way: the row says whether one measurement would make it scorable, and at what.
This is the "next observation" rule the tools owe a geologist: the most actionable thing the system can say, and the one place
the interface agent's plan runs arithmetic, in Python, from ids to ids.
"""

from __future__ import annotations

from typing import Any

from ..prospect import tools as T
from ..prospect.criteria import load as load_criteria
from ..values import stat

TOOL = "sensitivity"


def _val(vid: str, value: float, note: str) -> dict[str, Any]:
    return stat(vid, round(float(value), 4), fmt="ratio3", note=note)


def sensitivity(cell_id: str, breakdown: T.ToolResult | None = None,
                min_known_weight: float | None = None) -> T.ToolResult:
    """The ranking for one cell, from its criteria breakdown (fetched when not given) and the criteria file's
    `min_known_weight` (read when not given). Pure over those two inputs, so a test hands in a fake world."""
    breakdown = breakdown if breakdown is not None else T.call("criteria_breakdown", {"cell_id": cell_id})
    if min_known_weight is None:
        min_known_weight = load_criteria().min_known_weight
    base = f"c:sens:{cell_id}"
    out = T.ToolResult(TOOL, {"cell_id": cell_id})

    counted = [r for r in breakdown.rows if float(r.get("weight") or 0.0) > 0]
    total_weight = sum(float(r["weight"]) for r in counted)
    known = [r for r in counted if r.get("membership") is not None]
    unknown = [r for r in counted if r.get("membership") is None]
    known_weight = sum(float(r["weight"]) for r in known)
    known_total = sum(float(r["weight"]) * float(r["membership"]) for r in known)
    share_now = known_weight / total_weight if total_weight else 0.0
    scorable = total_weight > 0 and share_now >= min_known_weight
    score_now = known_total / known_weight if scorable and known_weight else None

    # the standing state, as values: a claim about "the score now" cites these rather than a number recomputed
    # in prose, and the threshold is a value because the argument is often about whether it was crossed
    out.values[f"{base}:known_share"] = _val(f"{base}:known_share", share_now,
                                             f"share of the criteria weight measured at cell {cell_id}")
    out.values[f"{base}:min_known_weight"] = _val(f"{base}:min_known_weight", min_known_weight,
                                                  "the known-weight share under which the criteria score is null")
    if score_now is not None:
        out.values[f"{base}:score_now"] = _val(f"{base}:score_now", score_now,
                                               f"the criteria score at cell {cell_id}, recomputed from the table")
    summary: dict[str, Any] = {
        "row": "now", "known_share": round(share_now, 4), "known_share_id": f"{base}:known_share",
        "min_known_weight": round(min_known_weight, 4), "min_known_weight_id": f"{base}:min_known_weight",
        "scorable": scorable, "n_unknown": len(unknown), "n_known": len(known),
    }
    if score_now is not None:
        summary["score_now"], summary["score_now_id"] = round(score_now, 4), f"{base}:score_now"
    else:
        summary["score_now"] = None
        summary["missing"] = "too little of the criteria weight is known here for a score"
    out.rows.append(summary)

    ranked: list[tuple[tuple[float, float, str], dict[str, Any]]] = []
    for r in unknown:
        key, w = str(r["criterion"]), float(r["weight"])
        weight_after = known_weight + w
        share_after = weight_after / total_weight
        if_met = (known_total + w) / weight_after
        if_not = known_total / weight_after
        scorable_after = share_after >= min_known_weight
        row: dict[str, Any] = {"row": "if_measured", "criterion": key, "title": r.get("title") or key,
                               "status": r.get("status") or "", "weight": w,
                               "weight_id": r.get("weight_id") or f"c:crit:{cell_id}:{key}:weight",
                               "scorable_after": scorable_after}
        ids = {"score_if_met": if_met, "score_if_not_met": if_not, "known_share_after": share_after}
        notes = {"score_if_met": f"the criteria score at cell {cell_id} if {key} were measured and met",
                 "score_if_not_met": f"the criteria score at cell {cell_id} if {key} were measured and not met",
                 "known_share_after": f"share of the criteria weight known at cell {cell_id} once {key} is measured"}
        move = 0.0
        if score_now is not None and scorable_after:
            move = if_met - score_now
            ids["delta_if_met"] = move
            notes["delta_if_met"] = f"how far the criteria score at cell {cell_id} moves if {key} is measured and met"
        for name, value in ids.items():
            vid = f"{base}:{key}:{name}"
            out.values[vid] = _val(vid, value, notes[name])
            row[name], row[f"{name}_id"] = round(value, 4), vid
        if score_now is None:
            row["note"] = ("measuring it would make the cell scorable" if scorable_after else
                           "measuring it alone would still leave too little known for a score")
        # the biggest move first; a cell with no score ranks the measurements that would give it one first, then
        # by weight; the key breaks ties so the order never depends on the table's row order
        rank = (-abs(move), -w if score_now is not None or scorable_after else 0.0, key)
        ranked.append((rank, row))
    ranked.sort(key=lambda item: item[0])
    for i, (_rank, row) in enumerate(ranked):
        row["rank"] = i + 1
        out.rows.append(row)
    out.note = (
        "Each row is one unknown criterion, measured and found met or not met, with everything else held as it "
        "is; the ranking is by how far the criteria score would move if it were met. Arithmetic over the weights "
        "and memberships the store holds, nothing more: it says which measurement matters, not what it would find."
        if unknown else "Every counted criterion is measured here: no single measurement is missing."
    )
    return out


__all__ = ["TOOL", "sensitivity"]

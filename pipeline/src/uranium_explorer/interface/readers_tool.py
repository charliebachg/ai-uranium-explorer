"""What the evidence readers concluded about a cell, as a tool the interface agent can read and cite.

The evidence readers are the benchmark's best analyst design: four readers, one per evidence family, then one
ranking call. Their results for the enabled cells are computed offline (`ue arm readers`) and stored in
`agent.reading_run`; this tool hands the newest one to the chat in the shape every tool result has: one row for
the ranking call's answer and one per reading, each number beside its id, and the values those ids name. A
cell with no run returns no rows and says so, so the agent abstains rather than infers a verdict.

Like the sensitivity, it lives with the interface agent, not in the MCP registry.
"""

from __future__ import annotations

from typing import Any

from ..prospect import tools as T

TOOL = "evidence_readers"


def evidence_readers(cell_id: str, con: Any = None) -> T.ToolResult:
    """The newest evidence readers result for one cell, from the agent tier."""
    from ..analyst.readers import for_cell
    from ..store import connect

    out = T.ToolResult(TOOL, {"cell_id": cell_id})
    own = con is None
    con = con or connect(read_only=True)
    try:
        results = for_cell(con, cell_id)
    finally:
        if own:
            con.close()
    if not results:
        out.note = (f"No evidence readers result is stored for cell {cell_id}: they were run offline on the "
                    "enabled cells only. Say that; do not infer what they would have concluded.")
        return out
    rec, values = results[0]
    answer = rec.get("answer") or {}
    out.rows.append({"row": "answer", "verdict": rec["verdict"] if rec["published"] else None,
                     "probability_id": rec.get("probability_id"), "published": rec["published"],
                     "problems": rec.get("problems") or [], "model": rec["model"], "created_at": rec["created_at"],
                     "unknown_criteria": answer.get("unknown_criteria") or [],
                     "absent_criteria": answer.get("absent_criteria") or [],
                     "next_observation": answer.get("next_observation"),
                     "claims": answer.get("claims") or []})
    for r in rec.get("readings") or []:
        out.rows.append({"row": "reading", "family": r["family"], "published": r["published"],
                         "assessment": r.get("assessment") if r["published"] else None,
                         "strength_id": r.get("strength_id"), "summary": r.get("summary") or "",
                         "unknowns": r.get("unknowns") or [], "claims": r.get("claims") or [],
                         "problems": r.get("problems") or []})
    # every stored value the result cites, with the minted probability and strengths, printed beside their ids
    for row in out.rows:
        for key in ("probability_id", "strength_id"):
            vid = row.get(key)
            if vid and vid in values:
                row[key.removesuffix("_id")] = values[vid]["value"]
    out.values = dict(values)
    out.note = ("The evidence readers, the benchmark's best analyst design (four readers, one per evidence "
                f"family, then one ranking call), run offline on {rec['model']}. A refused reading or answer "
                "failed the number check and says nothing usable; the probability is a calibration figure that the "
                "public record labels this cell a known deposit or occurrence, never a probability that ore is present.")
    return out


__all__ = ["TOOL", "evidence_readers"]

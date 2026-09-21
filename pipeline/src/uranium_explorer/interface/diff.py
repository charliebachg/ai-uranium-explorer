"""The session assessment: a finished analyst job beside the cell's stored chain without the
insight, compared node by node and verdict by verdict. Deterministic: two chains in, one diff out.

The baseline is the newest published dashboard chain for the cell whose nodes cite no expert-tier id: the run
the geologist saw before they said anything. The nodes are matched by id, which the template planner keeps
stable across runs of the same cell; a node one chain has and the other lacks is listed as such rather than
matched by guesswork. Nothing here is a number: statuses and verdicts are words, and the counts are of rows in
this very dict.

What counts as an expert-tier id is decided here, not taken from the node. A node's `expert_ids_json` is the
list the executor wrote ("the subset of value_ids the staged results mark as expert-tier"), and the node gate
checks only that it is a subset of the node's value ids; a cheap executor has listed a cross-check id there
on a run with no insight at all, which made the diff say the chain leaned on an insight it never saw. So both
the diff and the baseline search keep only ids of the tier the expert tier actually mints.
"""

from __future__ import annotations

from typing import Any

from ..analyst import chains as CH
from ..store import connect

#: An expert-tier value id starts with this: `record_insight` (the MCP handler the interface agent shares)
#: mints the numbers in a geologist's statement under `c:insight:<cell>:<insight>:<n>`, with the insight's own
#: id under `e:`. Nothing else in the store mints under `insight`, so the prefix is the tier.
EXPERT_PREFIX = "c:insight:"


def expert_tier(ids: Any) -> list[str]:
    """The ids of the expert tier among `ids`, sorted; anything under another prefix is a value the executor
    mislabelled, and is left out rather than counted as an insight."""
    return sorted({str(i) for i in (ids or []) if str(i).startswith(EXPERT_PREFIX)})


def _current(chain: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n["node_id"]): n for n in CH.current_nodes(list(chain.get("nodes") or []))}


def chain_diff(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    """`after` compared with `before`, both in `analyst.chains.load_chain`'s shape. With no baseline the
    diff says so and lists `after`'s nodes as new."""
    head_after = after.get("chain") or {}
    head_before = (before or {}).get("chain") or {}
    nodes_after = _current(after)
    nodes_before = _current(before) if before else {}
    rows: list[dict[str, Any]] = []
    for node_id in sorted(set(nodes_after) | set(nodes_before)):
        a, b = nodes_after.get(node_id), nodes_before.get(node_id)
        row: dict[str, Any] = {
            "node_id": node_id,
            "criterion": (a or b or {}).get("criterion"),
            "kind": (a or b or {}).get("kind"),
            "before": b.get("status") if b else None,
            "after": a.get("status") if a else None,
            "expert_ids": expert_tier(a.get("expert_ids_json")) if a else [],
        }
        row["changed"] = row["before"] != row["after"]
        rows.append(row)
    verdict_before = head_before.get("final_verdict") if before else None
    verdict_after = head_after.get("final_verdict")
    return {
        "chain_id": head_after.get("chain_id"),
        "baseline_chain_id": head_before.get("chain_id") if before else None,
        "verdict": {"before": verdict_before, "after": verdict_after, "changed": verdict_before != verdict_after},
        "nodes": rows,
        "n_changed": sum(1 for r in rows if r["changed"]),
        "n_leaning_on_expert": sum(1 for r in rows if r["expert_ids"]),
        "expert_ids": sorted({e for r in rows for e in r["expert_ids"]}),
        "note": ("no stored chain without an insight to compare against; every node is new" if before is None else
                 "node statuses and the verdict of the run with the insight, beside the stored run without it"),
    }


def baseline_chain(cell_id: str, con: Any = None, exclude: str | None = None) -> dict[str, Any] | None:
    """The newest published dashboard chain for the cell that leans on no expert-tier value, loaded whole;
    `exclude` keeps the job's own chain out of the search."""
    for head in CH.chains_for_cell(con, cell_id, purpose="dashboard"):
        if not head.get("published") or head.get("chain_id") == exclude:
            continue
        chain = CH.load_chain(con, str(head["chain_id"]))
        if not any(expert_tier(n.get("expert_ids_json")) for n in CH.current_nodes(chain["nodes"])):
            return chain
    return None


def assess(cell_id: str, chain_id: str, con: Any = None) -> dict[str, Any]:
    """The assessment for one finished job: its chain against the cell's baseline. The store is opened
    read-only here when no connection is given: the chains module's own default opens it for writing, and an
    assessment writes nothing."""
    own = con is None
    con = con if con is not None else connect(read_only=True)
    try:
        after = CH.load_chain(con, chain_id)
        return chain_diff(baseline_chain(cell_id, con, exclude=chain_id), after)
    finally:
        if own:
            con.close()


__all__ = ["EXPERT_PREFIX", "assess", "baseline_chain", "chain_diff", "expert_tier"]

"""The session assessment: a job's chain beside the stored chain without the insight, node by node."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from legacy_reader.analyst import chains as CH
from legacy_reader.interface import diff as D
from legacy_reader.store import connect

CELL = "0201_0072"


def node(node_id: str, status: str, criterion: str, expert: list[str] | None = None, round_: int = 1,
         attempt: int = 1) -> dict[str, Any]:
    return {"node_id": node_id, "round": round_, "attempt": attempt, "segment_id": f"seg-{node_id}", "kind": "criterion",
            "criterion": criterion, "status": status, "strength": 3, "value_ids_json": ["c:x"],
            "expert_ids_json": expert or [], "depends_on_json": [], "text": f"{criterion} is {status}",
            "published": True, "problems_json": [], "model": "fake", "cost_usd": 0.0, "duration_s": 0.0,
            "created_at": "t"}


def chain(chain_id: str, verdict: str, nodes: list[dict[str, Any]], created_at: str = "2026-09-21T00:00:00") -> dict[str, Any]:
    return {"chain": {"chain_id": chain_id, "cell_id": CELL, "bench_id": None, "purpose": "dashboard", "run_id": "r",
                      "arm": "v1", "fold": None, "planner": "template", "rounds": 1, "valid": True,
                      "final_verdict": verdict, "final_probability": 0.5, "weighted_score": 0.5, "weights_version": "w",
                      "verifier_label": verdict, "majority_label": None, "abstained_reason": None, "published": True,
                      "models_json": {}, "manifest_sha256": None, "blind_list_hash": None, "cost_usd": 0.0,
                      "duration_s": 0.0, "created_at": created_at},
            "nodes": nodes, "verdicts": [], "decision": None}


def test_the_diff_compares_statuses_and_the_verdict_and_names_what_leans_on_an_insight() -> None:
    before = chain("ch0", "insufficient", [node("n01", "met", "conductor"), node("n02", "unknown", "fault")])
    after = chain("ch1", "supports_closer_look", [node("n01", "met", "conductor"),
                                                   node("n02", "met", "fault", expert=["c:insight:x:1"]),
                                                   node("n03", "unknown", "host")])
    d = D.chain_diff(before, after)
    assert d["verdict"] == {"before": "insufficient", "after": "supports_closer_look", "changed": True}
    by = {r["node_id"]: r for r in d["nodes"]}
    assert by["n01"]["changed"] is False
    assert by["n02"] == {"node_id": "n02", "criterion": "fault", "kind": "criterion", "before": "unknown",
                         "after": "met", "expert_ids": ["c:insight:x:1"], "changed": True}
    assert by["n03"]["before"] is None and by["n03"]["after"] == "unknown" and by["n03"]["changed"] is True
    assert d["n_changed"] == 2 and d["n_leaning_on_expert"] == 1 and d["expert_ids"] == ["c:insight:x:1"]
    assert d["chain_id"] == "ch1" and d["baseline_chain_id"] == "ch0"
    assert D.chain_diff(before, after) == d, "deterministic"


def test_a_node_is_read_at_its_last_attempt() -> None:
    after = chain("ch1", "insufficient", [node("n01", "not_met", "conductor", round_=1),
                                          node("n01", "met", "conductor", round_=2)])
    assert D.chain_diff(None, after)["nodes"][0]["after"] == "met"
    assert "no stored chain" in D.chain_diff(None, after)["note"]


def test_the_baseline_is_the_newest_published_chain_that_leans_on_no_insight(tmp_path: Path) -> None:
    con = connect(tmp_path / "s.duckdb")
    try:
        for c in (chain("older", "insufficient", [node("n01", "unknown", "fault")], "2026-09-20T00:00:00"),
                  chain("with-insight", "supports_closer_look",
                        [node("n01", "met", "fault", expert=["c:insight:x:1"])], "2026-09-21T00:00:00"),
                  chain("job", "supports_closer_look", [node("n01", "met", "fault")], "2026-09-22T00:00:00")):
            CH.store_chain(con, c["chain"], c["nodes"], c["verdicts"], c["decision"])
        base = D.baseline_chain(CELL, con, exclude="job")
        assert base is not None and base["chain"]["chain_id"] == "older"
        d = D.assess(CELL, "job", con)
        assert d["baseline_chain_id"] == "older" and d["verdict"]["changed"] is True
        assert d["nodes"][0]["before"] == "unknown" and d["nodes"][0]["after"] == "met"
    finally:
        con.close()

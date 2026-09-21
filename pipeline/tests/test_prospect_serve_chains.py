"""The evidence record carries a cell's analyst chains : the newest few, current nodes
only, the values they cite merged into the record so every id the panel shows resolves, and none at all when
the store predates the chain tables."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_store_chains import _chain, _decision, _node, _verdict

from uranium_explorer.analyst import chains as C
from uranium_explorer.prospect import serve as S
from uranium_explorer.prospect import tools as T
from uranium_explorer.store import connect
from uranium_explorer.values import stat

CELL = "0201_0072"
CONDUCTOR = f"c:cell:{CELL}:d_conductor_m"
FAULT = f"c:cell:{CELL}:d_fault_m"
#: the cited values as the loop records them: the tools' own value records, keyed by id, plus one entry in the
#: gate's minimal form, which is not a value record and must not reach the web contract
VALUES = {
    CONDUCTOR: stat(CONDUCTOR, 1200.0, fmt="m1", unit="m", note="from the chain"),
    FAULT: stat(FAULT, 3500.0, fmt="m1", unit="m"),
    "v:gate": {"value_id": "v:gate", "value": 7},
}
CLAIMS = [{"text": "The nearest conductor is 1.2 km away.", "value_ids": [CONDUCTOR]}]

CHAIN_KEYS = {
    "chain_id", "run_id", "arm", "purpose", "planner", "rounds", "valid", "final_verdict", "final_probability",
    "weighted_score", "weights_version", "verifier_label", "majority_label", "abstained_reason", "published",
    "created_at", "cost_usd", "nodes", "verdicts", "decision",
}
NODE_KEYS = {"node_id", "segment_id", "kind", "criterion", "status", "strength", "value_ids", "expert_ids",
             "depends_on", "text", "published", "problems", "round", "attempt"}
VERDICT_KEYS = {"round", "valid", "faulty", "feedback", "candidate_label", "candidate_probability", "rationale"}
DECISION_KEYS = {"verdict", "probability", "claims", "unknown_criteria", "absent_criteria", "next_observation",
                 "rationale", "published", "problems"}


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store with the cell and no tool values: the record's registry starts empty, so what the chains add to
    it is exactly what the test sees. The tools are stubbed because `evidence()` calls them for every part."""
    path = tmp_path / "s.duckdb"
    con = connect(path)
    con.execute("insert into derived.cell (cell_id, grid_id, col, row, cx, cy, lon, lat, geom_wkb, in_basin) "
                "values (?, 'g', 1, 1, 0.0, 0.0, -105.1, 57.9, '\\x00'::blob, true)", [CELL])
    con.close()
    monkeypatch.setattr(S, "connect", lambda read_only=False: connect(path, read_only=read_only))
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(tool, args, rows=[], values={}))
    return path


#: `_store`'s decision when the test does not say: the adjudicator's answer, gated and published
_ADJUDICATED = object()


def _store(path: Path, decision: Any = _ADJUDICATED, **chain_over: Any) -> None:
    """One chain with a repaired node, written and closed before the record is read: the chain's ids are the
    cell's own value ids, the form the loop writes, unlike the store test's gate-only fixtures."""
    nodes = [_node("n01", 0, 1, status="not_met", published=False, problems_json=["polarity"],
                   value_ids_json=[CONDUCTOR]),
             _node("n01", 0, 2, value_ids_json=[CONDUCTOR]),
             _node("n02", 0, 1, criterion="fault_distance", value_ids_json=[FAULT], expert_ids_json=[FAULT],
                   text="The nearest fault is 3.5 km away."),
             _node("n01", 1, 1, strength=3, value_ids_json=[CONDUCTOR]),
             _node("n03", 1, 1, kind="crosscheck", criterion=None, depends_on_json=["n01", "n02"],
                   value_ids_json=[CONDUCTOR, FAULT], text="Conductor and fault coincide.")]
    if decision is _ADJUDICATED:
        decision = _decision(claims_json=CLAIMS, values_json=VALUES,
                             adjudicator_json={**_decision()["adjudicator_json"], "claims": CLAIMS})
    con = connect(path)
    try:
        C.store_chain(con, _chain(**{"cell_id": CELL, **chain_over}), nodes,
                      [_verdict(0, False), _verdict(1, True)], decision)
    finally:
        con.close()


def test_evidence_serves_the_newest_chains_with_current_nodes_and_the_values_they_cite(db: Path) -> None:
    _store(db, chain_id="old", created_at="2026-09-20T10:00:00+00:00")
    _store(db, chain_id="new", created_at="2026-09-20T12:00:00+00:00")
    _store(db, chain_id="elsewhere", cell_id="0201_0073")
    ev = S.evidence(CELL)
    assert [c["chain_id"] for c in ev["chains"]] == ["new", "old"], "newest first, this cell only"
    chain = ev["chains"][0]
    assert set(chain) == CHAIN_KEYS
    assert chain["arm"] == "v1-template" and chain["planner"] == "template" and chain["rounds"] == 2
    assert chain["valid"] is True and chain["published"] is True
    assert chain["final_verdict"] == "supports_closer_look" and chain["final_probability"] == 0.61
    assert chain["weighted_score"] == 0.58 and chain["weights_version"] == "w/v1"
    # current nodes only: the last attempt of each id, in plan order, JSON columns decoded and renamed
    assert [(n["node_id"], n["round"], n["attempt"]) for n in chain["nodes"]] == [
        ("n01", 1, 1), ("n02", 0, 1), ("n03", 1, 1)]
    assert all(set(n) == NODE_KEYS for n in chain["nodes"])
    n01, n02, n03 = chain["nodes"]
    assert n01["strength"] == 3 and n01["published"] is True and n01["problems"] == []
    assert n02["expert_ids"] == [FAULT] and n02["status"] == "met"
    assert n03["kind"] == "crosscheck" and n03["criterion"] is None and n03["depends_on"] == ["n01", "n02"]
    assert n03["value_ids"] == [CONDUCTOR, FAULT]
    # the verifier's rounds
    assert [(v["round"], v["valid"]) for v in chain["verdicts"]] == [(0, False), (1, True)]
    assert all(set(v) == VERDICT_KEYS for v in chain["verdicts"])
    assert chain["verdicts"][0]["faulty"] == [{"node_id": "n01", "reason": "polarity"}]
    assert chain["verdicts"][0]["feedback"] == "n01 reads a favourable feature as not met"
    # the adjudicator's decision, flattened from the row and its answer
    d = chain["decision"]
    assert set(d) == DECISION_KEYS
    assert d["verdict"] == "supports_closer_look" and d["probability"] == 0.61 and d["published"] is True
    assert d["claims"] == CLAIMS and d["unknown_criteria"] == ["alteration"] and d["absent_criteria"] == []
    assert d["next_observation"] == "a resistivity line across the conductor" and d["problems"] == []
    # the cited values resolve in the record's registry; the gate-only entry does not reach it
    assert ev["values"][CONDUCTOR]["value"] == 1200.0 and ev["values"][FAULT]["unit"] == "m"
    assert "v:gate" not in ev["values"]
    # and the chain's own numbers print through ids of their own, one per chain and one per node strength
    assert ev["values"]["c:chain:new:final_probability"]["value"] == 0.61
    assert ev["values"]["c:chain:new:weighted_score"]["value"] == 0.58
    assert ev["values"]["c:chain:new:decision_probability"]["value"] == 0.61
    assert ev["values"]["c:chain:new:rounds"]["fmt"] == "int"
    assert ev["values"]["c:chain:new:cost_usd"]["unit"] == "USD"
    strength = ev["values"]["c:chain:new:n01:strength"]
    assert strength["id"] == "c:chain:new:n01:strength" and strength["kind"] == "stat" and strength["value"] == 3
    assert "c:chain:old:n01:strength" in ev["values"], "every served chain registers its numbers"


def test_a_value_the_tools_return_now_is_not_replaced_by_the_chain_copy(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = stat(CONDUCTOR, 1200.0, fmt="m1", unit="m", note="from the tool")
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(tool, args, rows=[], values={CONDUCTOR: fresh}))
    _store(db)
    ev = S.evidence(CELL)
    assert ev["values"][CONDUCTOR]["note"] == "from the tool"
    assert ev["values"][FAULT]["value"] == 3500.0, "the chain fills in only what the tools did not return"


def test_only_the_five_newest_chains_are_served(db: Path) -> None:
    for i in range(6):
        _store(db, chain_id=f"ch{i}", created_at=f"2026-09-20T1{i}:00:00+00:00")
    ev = S.evidence(CELL)
    assert [c["chain_id"] for c in ev["chains"]] == ["ch5", "ch4", "ch3", "ch2", "ch1"]
    assert "c:chain:ch0:final_probability" not in ev["values"], "an unserved chain registers nothing"


def test_an_abstention_serves_no_decision_and_no_probability(db: Path) -> None:
    _store(db, decision=None, valid=False, final_verdict="insufficient", final_probability=None,
           weighted_score=None, weights_version=None, abstained_reason="never validated in 2 rounds")
    chain = S.evidence(CELL)["chains"][0]
    assert chain["decision"] is None and chain["abstained_reason"] == "never validated in 2 rounds"
    assert chain["final_verdict"] == "insufficient" and chain["final_probability"] is None
    ev = S.evidence(CELL)
    assert "c:chain:ch1:final_probability" not in ev["values"], "a null column mints no value"
    assert "c:chain:ch1:weighted_score" not in ev["values"]
    assert ev["values"]["c:chain:ch1:rounds"]["value"] == 2


def test_a_store_without_the_chain_tables_still_serves_the_record(db: Path) -> None:
    con = connect(db)
    for table in ("chain_decision", "chain_verdict", "chain_node", "chain"):
        con.execute(f"drop table agent.{table}")
    con.execute("insert into agent.memo (memo_id, cell_id, role, verdict, model, prompt_version, run_id, "
                "created_at, published) values ('m1', ?, 'skeptic', 'insufficient', 'fake', 'v', 'r', 't', true)",
                [CELL])
    con.close()
    ev = S.evidence(CELL)
    assert ev["chains"] == [] and ev["memos"][0]["memo_id"] == "m1" and ev["lon"] == -105.1

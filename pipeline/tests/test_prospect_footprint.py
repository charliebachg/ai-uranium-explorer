"""Absence of mapping is not absence of features: the layer footprint built in memory from each layer's own
geometry, the 0/1 flag `nearby` and `crosscheck` return under an id, and the node gate's rule that a
`not_met` node outside the footprint is refused and told to record unknown.

The world is a 40 km survey block with a 16 km hole in its middle, drawn three ways (samples on a 3 km grid,
conductor traces every 2 km, one map polygon with an interior ring), and three cells: one in the surveyed
part of the block, one in the hole, one 100 km away. Every answer is known before a tool runs."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from shapely.geometry import LineString, Point, Polygon, mapping

from uranium_explorer import store as ST
from uranium_explorer.analyst import loop as L
from uranium_explorer.analyst import nodegate as G
from uranium_explorer.analyst import wire as W
from uranium_explorer.analyst.arms import Inputs, Switches
from uranium_explorer.analyst.session import Session
from uranium_explorer.prospect import criteria as C
from uranium_explorer.prospect import tools as T
from uranium_explorer.values import stat
from bench_store import bench_frame, make_bench_store
from fake_loop_world import CELL as LOOP_CELL
from fake_loop_world import COND as LOOP_COND
from fake_loop_world import LoopBackend, LoopWorld, loop_registry, node_answer

CX, CY = 500_000.0, 6_400_000.0
BLOCK, HOLE = 21_000, 8_000          # half-widths in metres: the block's samples reach 21 km, the hole 8 km
IN_BLOCK, IN_HOLE, FAR = "0000_0000", "0000_0001", "0000_0002"
CENTRE = {IN_BLOCK: (CX + 15_000, CY + 15_000), IN_HOLE: (CX, CY), FAR: (CX + 100_000, CY)}


def feature(geom, **props) -> dict:
    return {"type": "Feature", "geometry": mapping(geom), "bbox": list(geom.bounds), "properties": props}


def _in_hole(x: float, y: float) -> bool:
    return abs(x) < HOLE and abs(y) < HOLE


#: the same block and hole in three geometries; the fault sits 3 km east of the in-block cell only
WORLD: dict[str, list[dict]] = {
    "lake_sediment_gsc": [feature(Point(CX + x, CY + y), value=2.0)
                          for x in range(-BLOCK, BLOCK + 1, 3000) for y in range(-BLOCK, BLOCK + 1, 3000)
                          if not _in_hole(x, y)],
    "em_conductors": [feature(LineString([(CX + x0, CY + y), (CX + x1, CY + y)]))
                      for y in range(-BLOCK, BLOCK + 1, 2000)
                      for x0, x1 in ((-BLOCK, BLOCK),) if abs(y) >= HOLE] +
                     [feature(LineString([(CX + x0, CY + y), (CX + x1, CY + y)]))
                      for y in range(-BLOCK, BLOCK + 1, 2000) if abs(y) < HOLE
                      for x0, x1 in ((-BLOCK, -HOLE), (HOLE, BLOCK))],
    "faults_250k": [feature(LineString([(CX + 18_000, CY + 10_000), (CX + 18_000, CY + 20_000)]), text="Fault")],
    "bedrock_250k": [feature(Polygon([(CX - BLOCK, CY - BLOCK), (CX + BLOCK, CY - BLOCK), (CX + BLOCK, CY + BLOCK),
                                      (CX - BLOCK, CY + BLOCK)],
                                     [[(CX - HOLE, CY - HOLE), (CX + HOLE, CY - HOLE), (CX + HOLE, CY + HOLE),
                                       (CX - HOLE, CY + HOLE)]]), text="Gneiss")],
    "radioactive_boulders": [],
}


@pytest.fixture
def world(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three cells at the three places, the layer reader replaced by the world, and the footprint cache cleared
    on both sides because it is built from whatever `layer_features` returned last."""
    df = bench_frame(n_dep=1, n_occ=1, n_neg=1, n_probe=1)
    for i, cell in enumerate((IN_BLOCK, IN_HOLE, FAR)):
        df.loc[i, ["cx", "cy"]] = CENTRE[cell]
    make_bench_store(prospect_sandbox.db, df)
    monkeypatch.setattr(ST, "db_path", lambda: prospect_sandbox.db)
    monkeypatch.setattr(T, "layer_features", lambda layer: WORLD.get(layer, []))
    T.layer_footprint.cache_clear()
    yield prospect_sandbox.db
    T.layer_footprint.cache_clear()


# ---------------------------------------------------------------- the footprint itself


def test_one_halo_per_geometry_kind_and_polygons_need_none(world: Path) -> None:
    assert T.FOOTPRINT_HALO_M == {"point": 5000.0, "line": 5000.0}
    sed, em, rock = (T.layer_footprint(k) for k in ("lake_sediment_gsc", "em_conductors", "bedrock_250k"))
    assert (sed.kind, sed.halo_m) == ("point", 5000.0) and "within 5000 m of any lake_sediment_gsc" in sed.rule
    assert (em.kind, em.halo_m) == ("line", 5000.0)
    assert (rock.kind, rock.halo_m) == ("polygon", None) and rock.rule == "the union of the bedrock_250k polygons"
    empty = T.layer_footprint("radioactive_boulders")
    assert empty.kind == "empty" and empty.geometry.is_empty and "no features" in empty.rule
    assert T.layer_footprint("em_conductors") is em, "built once per process"


@pytest.mark.parametrize("layer", ["lake_sediment_gsc", "em_conductors", "bedrock_250k"])
def test_inside_the_block_in_its_hole_and_far_outside(world: Path, layer: str) -> None:
    """The union of halos keeps the hole open where a hull would fill it; the polygon's interior ring is the
    hole itself."""
    fp = T.layer_footprint(layer)
    assert fp.contains(CENTRE[IN_BLOCK]) == 1
    assert fp.contains(CENTRE[IN_HOLE]) == 0, "the hole is 8 km from the nearest feature, past the 5 km halo"
    assert fp.contains(CENTRE[FAR]) == 0
    assert fp.geometry.area < (2 * BLOCK + 2 * 5000) ** 2, "one patch with a hole, not the block's hull"


def test_the_geometry_never_leaves_the_grid_crs_and_a_point_on_the_halo_edge_counts_inside(world: Path) -> None:
    fp = T.layer_footprint("lake_sediment_gsc")
    minx, miny, maxx, maxy = fp.geometry.bounds
    assert (minx, miny) == pytest.approx((CX - BLOCK - 5000, CY - BLOCK - 5000), abs=1.0)
    assert (maxx, maxy) == pytest.approx((CX + BLOCK + 5000, CY + BLOCK + 5000), abs=1.0)
    assert fp.contains((CX + BLOCK + 4990, CY)) == 1 and fp.contains((CX + BLOCK + 5100, CY)) == 0


# ---------------------------------------------------------------- nearby and crosscheck


def test_nearby_returns_the_flag_under_the_call_base_with_the_rule_in_its_note(world: Path) -> None:
    out = T.nearby(IN_HOLE, "em_conductors", radius_m=5000)
    base = f"c:nb:{IN_HOLE}:em_conductors:5000"
    summary = out.rows[0]
    assert summary["n_within"] == 0 and summary["in_footprint"] == 0
    assert summary["in_footprint_id"] == f"{base}:in_footprint"
    assert summary["footprint_halo_m_id"] == f"{base}:footprint_halo_m"
    flag = out.values[f"{base}:in_footprint"]
    assert flag["value"] == 0 and "within 5000 m of any em_conductors feature" in flag["note"]
    halo = out.values[f"{base}:footprint_halo_m"]
    assert halo["value"] == 5000.0 and halo["unit"] == "m"
    assert summary["footprint_note"].startswith("nothing of em_conductors was mapped or sampled around this cell")
    assert "unknown, not an absence" in summary["footprint_note"]
    assert "in_footprint is 1" in out.note and "unknown, not not met" in out.note

    inside = T.nearby(IN_BLOCK, "em_conductors", radius_m=5000).rows[0]
    assert inside["in_footprint"] == 1 and inside["n_within"] >= 1 and "an absence" in inside["footprint_note"]
    wide = T.nearby(IN_HOLE, "em_conductors", radius_m=10_000).rows[0]
    assert wide["n_within"] >= 1 and wide["in_footprint"] == 0, "a trace inside a wide radius does not move the flag"


def test_a_polygon_layer_flags_the_hole_without_a_halo_value(world: Path) -> None:
    hole = T.nearby(IN_HOLE, "bedrock_250k", radius_m=10_000)
    assert hole.rows[0]["in_footprint"] == 0 and "footprint_halo_m" not in hole.rows[0]
    assert hole.rows[0]["n_within"] == 1 and hole.rows[0]["nearest_m"] == float(HOLE), \
        "the map polygon is within reach, but its interior ring is the cell's ground"
    assert not any(vid.endswith(":footprint_halo_m") for vid in hole.values)
    assert T.nearby(IN_BLOCK, "bedrock_250k", radius_m=500).rows[0]["in_footprint"] == 1


def test_crosscheck_tells_absent_from_unknown_by_each_sides_footprint(world: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """With the reach narrowed to 2 km: the in-block cell has a conductor through it and the fault 3 km off,
    so the fault side is empty inside the reach but mapped around the cell (absent); the cell in the hole
    has nothing inside the reach and nothing mapped around it (unknown)."""
    monkeypatch.setattr(T, "CROSSCHECK_RADIUS_M", 2000.0)
    absent = next(r for r in T.crosscheck(IN_BLOCK).rows if r["pair"] == "conductor_fault")
    assert absent["state"] == "absent" and absent["absent"] == "no mapped fault within the radius"
    assert absent["conductor_in_footprint"] == 1 and absent["fault_in_footprint"] == 1
    unknown_out = T.crosscheck(IN_HOLE)
    unknown = next(r for r in unknown_out.rows if r["pair"] == "conductor_fault")
    assert unknown["state"] == "unknown" and "absent" not in unknown
    assert unknown["missing"].startswith("no conductor or fault was mapped around this cell at all")
    for layer, side in (("em_conductors", "conductor"), ("faults_250k", "fault")):
        vid = f"c:x:{IN_HOLE}:conductor_fault:{layer}:in_footprint"
        assert unknown[f"{side}_in_footprint"] == 0 and unknown[f"{side}_in_footprint_id"] == vid
        assert unknown_out.values[vid]["value"] == 0
    sed = next(r for r in unknown_out.rows if r["pair"] == "sediment_sampling")
    assert sed["sediment_in_footprint"] == 0
    assert sed["sediment_in_footprint_id"] == f"c:x:{IN_HOLE}:sediment_sampling:lake_sediment_gsc:in_footprint"


# ---------------------------------------------------------------- the node gate, rule 6

CS = C.load()
CELL = "b01"
COND = f"b:{CELL}:cell:d_conductor_m"
FAULT = f"b:{CELL}:cell:d_fault_m"
NB_COND_OUT = f"b:{CELL}:nb:em_conductors:5000:in_footprint"
NB_FAULT_IN = f"b:{CELL}:nb:faults_250k:5000:in_footprint"
NB_COND_COUNT = f"b:{CELL}:nb:em_conductors:5000:n_within"
X_COND_OUT = f"b:{CELL}:x:conductor_fault:em_conductors:in_footprint"
X_FAULT_IN = f"b:{CELL}:x:conductor_fault:faults_250k:in_footprint"
X_CROSSINGS = f"b:{CELL}:x:conductor_fault:crossings_n"
#: a cell 30 km from the nearest conductor, outside the conductor footprint and inside the fault one
VALUES = {
    COND: stat(COND, 30_000.0, fmt="m1", unit="m"),
    FAULT: stat(FAULT, 4800.0, fmt="m1", unit="m"),
    NB_COND_OUT: stat(NB_COND_OUT, 0),
    NB_FAULT_IN: stat(NB_FAULT_IN, 1),
    NB_COND_COUNT: stat(NB_COND_COUNT, 0),
    X_COND_OUT: stat(X_COND_OUT, 0),
    X_FAULT_IN: stat(X_FAULT_IN, 1),
    X_CROSSINGS: stat(X_CROSSINGS, 0),
}
CONTEXT = ""
REFUSAL = (f"footprint: nothing of em_conductors was mapped or sampled around cell {CELL} ({NB_COND_OUT} is 0), so "
           f"an empty radius there is unknown, not not_met; return status unknown with strength 0 and say so in "
           f"unknown_reason")


def node(criterion: str = "conductor_proximity", status: str = "not_met", strength: int = 1,
         ids: list[str] | None = None, text: str = "No conductor within reach; the nearest is 30,000 m away.",
         **kw) -> W.Node:
    fields = dict(node_id="n01", segment_id="s01", kind="criterion", criterion=criterion, status=status,
                  strength=strength, value_ids=[COND, NB_COND_COUNT] if ids is None else ids, text=text)
    fields.update(kw)
    return W.Node(**fields)


def gate(n: W.Node, values: dict | None = None) -> list[str]:
    return G.check_node(n, VALUES if values is None else values, CONTEXT, CS, {CELL}, set())


def test_rule_6_refuses_not_met_outside_the_footprint_naming_the_layer_and_the_cell() -> None:
    problems = gate(node())
    assert problems == [REFUSAL], "polarity passes (30 km is unfavourable); only the footprint refuses"
    assert "em_conductors" in problems[0] and f"cell {CELL}" in problems[0] and "unknown" in problems[0]
    assert gate(node("fault_proximity", ids=[FAULT], text="The nearest fault is 4,800 m away.")) == [], \
        "the fault footprint reaches the cell, so not_met stands"


def test_rule_6_never_refuses_met_and_lets_unknown_cite_the_feature_value() -> None:
    near = {**VALUES, COND: stat(COND, 820.0, fmt="m1", unit="m")}
    assert gate(node(status="met", strength=4, text="A conductor 820 m away."), near) == [], \
        "a feature inside the radius is evidence whatever the footprint says"
    assert gate(node(status="unknown", strength=0, text="Nothing of this layer was mapped around the cell.",
                     unknown_reason="outside the conductor footprint")) == [], \
        "outside the footprint the distance value is not a measurement, so citing it is no dodge"
    assert gate(node(status="unknown", strength=0, ids=[NB_COND_OUT], text="Unmapped here.")) == []
    assert gate(node(status="unknown", strength=2, ids=[], text="Unmapped here.")) == \
        ["strength: must be 0 when the status is unknown, not 2"]


def test_rule_6_reads_the_crosscheck_flag_for_a_pair_node() -> None:
    pair = node("conductor_fault", ids=[X_CROSSINGS], text="No conductor meets a fault here: 0 crossings.",
                kind="crosscheck", depends_on=["n01", "n02"])
    only_x = {k: v for k, v in VALUES.items() if not k.startswith(f"b:{CELL}:nb:")}
    problems = gate(pair, only_x)
    assert len(problems) == 1 and problems[0].startswith("footprint: nothing of em_conductors")
    assert X_COND_OUT in problems[0], "the pair's own flag, not nearby's"
    mapped = {**only_x, X_COND_OUT: stat(X_COND_OUT, 1)}
    assert gate(pair, mapped) == []
    assert gate(W.Node(**{**pair.__dict__, "status": "met", "strength": 3}), only_x) == []


def test_rule_6_holds_nobody_to_a_footprint_that_was_not_computed() -> None:
    without = {COND: VALUES[COND], FAULT: VALUES[FAULT]}
    assert gate(node(ids=[COND]), without) == [], "no flag in the registry: nothing to refuse on"
    theirs = "b:b02:nb:em_conductors:5000:in_footprint"
    other = {**without, theirs: stat(theirs, 0)}
    assert gate(node(ids=[COND]), other) == [], "another cell's footprint is somebody else's evidence"


def test_rule_6_works_on_live_ids_too() -> None:
    cell = "0000_0053"
    cond, flag = f"c:cell:{cell}:d_conductor_m", f"c:nb:{cell}:em_conductors:5000:in_footprint"
    live = {cond: stat(cond, 30_000.0, fmt="m1", unit="m"), flag: stat(flag, 0)}
    problems = G.check_node(node(ids=[cond]), live, CONTEXT, CS, {cell}, set())
    assert len(problems) == 1 and f"cell {cell}" in problems[0] and flag in problems[0]


def test_rule_6_escalates_like_the_others_and_ends_in_a_recorded_unknown() -> None:
    problems = gate(node())
    one, two, three = (G.feedback(problems, i, sorted(VALUES)) for i in (1, 2, 3))
    assert REFUSAL in one and NB_COND_OUT not in one.split("\n")[2:], "attempt 1: the reason only"
    assert f"  {NB_COND_OUT}" in two and "protocol" not in two.lower()
    assert "protocol" in three.lower() and "in_footprint is 0" in three and "return status unknown" in three
    rec = G.record_unknown(node(attempt=3), problems)
    assert (rec.status, rec.strength, rec.value_ids, rec.published) == ("unknown", 0, [], False)
    assert rec.text.startswith("gate: footprint: nothing of em_conductors was mapped or sampled around cell b01")
    assert rec.problems == problems and rec.check() == [] and gate(rec) == []


# ---------------------------------------------------------------- through the harness, on the fake world


@pytest.fixture
def weights_stub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The weights module as an interface, as the loop tests stub it: a version and a fixed score."""
    stub = types.ModuleType("uranium_explorer.analyst.weights")
    stub.criteria_weights = lambda criteria: SimpleNamespace(version="stub/v1")  # type: ignore[attr-defined]
    stub.score = lambda nodes, weights: 0.7  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uranium_explorer.analyst.weights", stub)
    return stub


def _unmapped_conductors(world: LoopWorld) -> dict[str, Any]:
    """The fake tool world with `nearby` saying the conductor layer's footprint does not reach the cell."""
    tools = loop_registry(world)
    plain = tools["nearby"]

    def nearby(cell_id: str, layer: str, radius_m: float = 5000.0, k: int = 5) -> T.ToolResult:
        out = plain(cell_id, layer, radius_m, k)
        flag = f"c:nb:{cell_id}:{layer}:{radius_m:g}:in_footprint"
        inside = 0 if layer == "em_conductors" else 1
        out.rows[0].update(in_footprint=inside, in_footprint_id=flag)
        out.values[flag] = stat(flag, inside, note=f"1 when cell {cell_id} lies inside the mapped extent of {layer}")
        return out

    return {**tools, "nearby": nearby}


def _run(tmp_path: Path, backend: LoopBackend, world: LoopWorld) -> tuple[dict[str, Any], Session]:
    """One cell on a dashboard session, as the loop tests run it, over the world with the unmapped layer."""
    switches = Switches(drillholes=True, label_context=True, oof_scores=True, effort_features=True, criteria=True)
    session = Session.open(LOOP_CELL, "dashboard", fold=None, switches=switches, stage=tmp_path / "cell" / "stage",
                           tools=_unmapped_conductors(world))
    cfg = L.LoopConfig(executor_model="m-small", verifier_model="m-large", adjudicator_model="m-large",
                       planner_model="m-large", effort="low", segment_workers=2)
    row = L.run_cell_v1(backend, session, None, Inputs(card=False, pack=True, passages=False), switches, cfg, CS,
                        chain_id="ch-1", run_id="run-1", con=None, stage=session.stage)
    return row, session


def test_through_the_harness_a_not_met_outside_the_footprint_is_refused_three_times_then_recorded_unknown(
        tmp_path: Path, weights_stub) -> None:
    """The executor insists on not_met for the conductor criterion (hedged, so polarity passes) while the
    staged `nearby` says the layer was never mapped around the cell: the gate refuses every attempt with the
    layer and the cell in its feedback, and the harness records the node unknown."""
    insists = {**node_answer("conductor_proximity"), "status": "not_met", "strength": 1,
               "text": "The conductor is 820 m away; however, nothing else of the layer is mapped here."}
    backend = LoopBackend(executor={"s01": [insists, insists, insists]})
    row, _session = _run(tmp_path, backend, LoopWorld())
    reqs = backend.executor_requests("s01")
    assert len(reqs) == 3
    reason = f"footprint: nothing of em_conductors was mapped or sampled around cell {LOOP_CELL}"
    assert reason not in reqs[0].user_prompt and reason in reqs[1].user_prompt and reason in reqs[2].user_prompt
    assert "return status unknown" in reqs[1].user_prompt and "last attempt" in reqs[2].user_prompt
    chain = W.Chain.from_dict(row["chain"]).validate()
    n01 = next(n for n in chain.nodes if n.segment_id == "s01")
    assert (n01.status, n01.strength, n01.published, n01.attempt, n01.value_ids) == ("unknown", 0, False, 3, [])
    assert n01.text.startswith("gate: footprint: nothing of em_conductors was mapped") and n01.problems
    assert row["stages"]["n_gate_rejections"] == 3 and row["stages"]["n_recorded_unknown"] == 1
    others = [n for n in chain.nodes if n.segment_id != "s01"]
    assert all(n.published for n in others), "every other layer's footprint reaches the cell: nothing else refused"
    cross = next(n for n in others if n.segment_id == "s09")
    assert LOOP_COND in cross.value_ids, "the cross-check still cites the conductor"


def test_through_the_harness_an_unknown_on_the_second_attempt_is_taken(tmp_path: Path, weights_stub) -> None:
    insists = {**node_answer("conductor_proximity"), "status": "not_met", "strength": 1,
               "text": "The conductor is 820 m away; however, the layer is not mapped here."}
    relents = {**node_answer("conductor_proximity"), "status": "unknown", "strength": 0, "value_ids": [LOOP_COND],
               "text": "Nothing of the conductor layer was mapped around this cell.",
               "unknown_reason": "outside the conductor footprint"}
    backend = LoopBackend(executor={"s01": [insists, relents]})
    row, _session = _run(tmp_path, backend, LoopWorld())
    assert len(backend.executor_requests("s01")) == 2
    n01 = next(n for n in W.Chain.from_dict(row["chain"]).nodes if n.segment_id == "s01")
    assert (n01.status, n01.strength, n01.published, n01.attempt) == ("unknown", 0, True, 2)
    assert n01.value_ids == [LOOP_COND], "outside the footprint the distance value may be cited by an unknown node"
    assert row["stages"]["n_gate_rejections"] == 1 and row["stages"]["n_recorded_unknown"] == 0

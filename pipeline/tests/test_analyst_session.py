"""The analyst session: the one path from a tool to a model, and the leakage rules it enforces in code.

B17 (retrieval), B18 (scores), B19 (expert anchoring) and B30 (the cell's own label) each had a failing test
here before a line of the session existed. The fakes ignore the leakage keywords on purpose, so both layers
of every rule are proved on their own: the argument reached the tool, and the session dropped the row anyway.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from legacy_reader import store as ST
from legacy_reader.analyst import session as S
from legacy_reader.analyst.arms import Switches
from legacy_reader.bench.blind import blind_list
from legacy_reader.ids import sha256_json
from legacy_reader.prospect import tools as T
from legacy_reader.runtime.tracing import read_spans, trace

from bench_store import bench_frame, make_bench_store
from fake_session_world import BENCH, BLIND_FILE, CELL, FAR_FILE, SERVED_SCORE, FakeWorld, fake_registry

ALL_ON = Switches(drillholes=True, label_context=True, oof_scores=True, effort_features=True, criteria=True)
FOLD = 2
#: strings that place or name the ground around CELL: company, property and deposit names, hole names, files.
#: "McArthur River" is not here because the criteria table's literature line names it for every cell alike,
#: and `scrub` keeps that text by design; the passage that names it as a place is checked on its own.
LEAKS = ("ASAMERA", "Asamera", "Cluff Lake", "Cigar Lake", "Rabbit Lake", "Cameco", "SMDC",
         "Q6-1", "MC-361", BLIND_FILE, FAR_FILE, "74H09", "58.3510")


@pytest.fixture
def store(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The synthetic store, with out-of-fold scores for CELL: two models in fold 2 and a stale row in fold 1."""
    db = prospect_sandbox.db
    make_bench_store(db, bench_frame(n_dep=4, n_occ=4, n_neg=4, n_probe=4))
    monkeypatch.setattr(ST, "db_path", lambda: db)
    con = ST.connect(db)
    try:
        for model, fold, score in (("learned", FOLD, 0.61), ("effort", FOLD, 0.83), ("criteria", 1, 0.55)):
            con.execute("insert into derived.cell_score_oof values (?, ?, 'spatial', ?, ?, 'run-1', 'now', 'derived')",
                        [CELL, model, fold, score])
    finally:
        con.close()
    return db


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


def opened(store: Path, world: FakeWorld, tmp_path: Path, purpose: str = "scored", **over) -> S.Session:
    kw = dict(fold=FOLD, switches=ALL_ON, con=ST.connect(store, read_only=True),
              stage=tmp_path / "stage" / purpose, tools=fake_registry(world))
    if purpose == "benchmark":
        kw["bench_id"] = BENCH
    kw.update(over)
    return S.Session.open(CELL, purpose, **kw)


def everything(session: S.Session) -> str:
    """Every string the model could read back: the registry, the context, the call log and the staged files."""
    files = {p.name: json.loads(p.read_text()) for p in sorted(session.stage.glob("tool_*.json"))}
    return json.dumps({"values": session.values, "context": session.context, "calls": session.calls, "files": files})


# ---------------------------------------------------------------- B17: retrieval leakage


def test_b17_a_scored_retrieve_carries_the_blind_list_and_the_session_drops_a_blind_listed_row_anyway(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path)
    con = ST.connect(store, read_only=True)
    try:
        assert s.blind_list == blind_list(CELL, 10.0, con) and BLIND_FILE in s.blind_list
    finally:
        con.close()
    assert s.blind_list_hash == sha256_json(s.blind_list)
    out = s.call("retrieve", {"query": "graphitic conductor", "cell_id": "$cell"})
    got = world.received["retrieve"][-1]
    assert got["exclude_files"] == s.blind_list and got["cell_id"] == CELL, "the blind list reached the tool"
    # the fake returned the blind-listed passage regardless; the session's own filter is the second layer
    assert [r["tier"] for r in out.rows] == ["page", "expert"]
    assert s.refusals == {"B17": 1}
    assert "c:pass:0:page" not in s.values and "c:pass:1:page" in s.values
    text = everything(s)
    assert "Asamera" not in text and "Cluff Lake" not in text and "Q6-1" not in text
    assert "exclude_files" not in text, "the blind list itself is not something the model reads"


def test_b17_a_dashboard_retrieve_is_unblinded(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path, purpose="dashboard")
    assert s.blind_list == [] and s.bench_id == CELL
    out = s.call("retrieve", {"query": "graphitic conductor", "cell_id": "$cell"})
    assert "exclude_files" not in world.received["retrieve"][-1]
    assert len(out.rows) == 3 and s.refusals == {}
    assert "Cluff Lake" in everything(s), "the dashboard keeps the record as it is"


# ---------------------------------------------------------------- B18: score leakage


def test_b18_a_scored_session_serves_only_the_out_of_fold_scores_of_its_fold(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path)
    out = s.call("cell_scores", {"cell_id": "$cell"})
    assert world.received["cell_scores"] == [], "the served table was never asked for"
    assert {r["model"]: (r["fold"], r["score"]) for r in out.rows} == {"effort": (FOLD, 0.83), "learned": (FOLD, 0.61)}
    assert all(r["out_of_fold"] for r in out.rows)
    assert s.scores_seen == [{"model_version": "effort", "fold_kind": "spatial", "fold": FOLD},
                             {"model_version": "learned", "fold_kind": "spatial", "fold": FOLD}]
    assert set(s.values) == {f"c:score:{CELL}:effort", f"c:score:{CELL}:learned"}
    assert s.refusals == {"B18": 1}, "the fold-1 row for the criteria model is a leak and is counted as one"
    assert str(SERVED_SCORE) not in everything(s)


def test_b18_an_injected_oof_table_serves_the_same_way(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    oof = pd.DataFrame({"cell_id": [CELL, CELL, "0000_0001"], "model": ["learned", "criteria", "learned"],
                        "fold_kind": "spatial", "fold": [FOLD, FOLD, 1], "score": [0.7, 0.5, 0.9]})
    s = opened(store, world, tmp_path, oof=oof)
    out = s.call("cell_scores", {"cell_id": "$cell"})
    assert {r["model"]: r["score"] for r in out.rows} == {"criteria": 0.5, "learned": 0.7}


def test_b18_scores_are_refused_when_the_switch_is_off_or_the_fold_is_unknown(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path, switches=replace(ALL_ON, oof_scores=False))
    with pytest.raises(S.SessionRefusal) as err:
        s.call("cell_scores", {"cell_id": "$cell"})
    assert err.value.rule == "switch"
    s = opened(store, world, tmp_path, fold=None)
    with pytest.raises(S.SessionRefusal) as err:
        s.call("cell_scores", {"cell_id": "$cell"})
    assert err.value.rule == "B18"
    assert world.received["cell_scores"] == [] and s.scores_seen == [] and s.calls == []


def test_b18_a_dashboard_may_see_the_served_scores_and_says_so(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    world.served_scores_allowed = True
    s = opened(store, world, tmp_path, purpose="dashboard", fold=None)
    out = s.call("cell_scores", {"cell_id": "$cell"})
    assert world.received["cell_scores"] == [{"cell_id": CELL}]
    assert out.rows[0]["score"] == SERVED_SCORE
    assert s.scores_seen == [{"model_version": "learned", "fold_kind": "served", "fold": None}]


# ---------------------------------------------------------------- B30: the cell's own label


def test_b30_a_scored_label_context_is_masked_and_a_zero_km_row_is_dropped_anyway(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path)
    out = s.call("label_context", {"cell_id": "$cell"})
    assert world.received["label_context"][-1]["mask_cell"] == CELL, "the mask reached the tool"
    assert [r["distance_km"] for r in out.rows] == [12.4], "the fake ignored the mask; the session did not"
    assert f"c:near:{CELL}:0" not in s.values and f"c:near:{CELL}:1" in s.values
    assert s.refusals == {"B30": 1}
    assert "mask_cell" not in everything(s)


def test_b30_a_dashboard_label_context_keeps_the_cells_own_label(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path, purpose="dashboard")
    out = s.call("label_context", {"cell_id": "$cell"})
    assert "mask_cell" not in world.received["label_context"][-1]
    assert [r["distance_km"] for r in out.rows] == [0.0, 12.4] and out.rows[0]["name"] == "Cigar Lake"


def test_label_context_is_refused_when_its_switch_is_off(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path, switches=replace(ALL_ON, label_context=False))
    with pytest.raises(S.SessionRefusal) as err:
        s.call("label_context", {"cell_id": "$cell"})
    assert err.value.rule == "switch" and world.received["label_context"] == []


# ---------------------------------------------------------------- B19: expert anchoring


def test_b19_an_expert_tier_value_is_remembered_and_the_row_that_cites_it_is_marked(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path)
    out = s.call("retrieve", {"query": "offset", "cell_id": "$cell"})
    assert s.expert_ids == {f"c:expert:{CELL}:1"}
    by_tier = {r["tier"]: r for r in out.rows}
    assert by_tier["expert"]["expert"] is True and "expert" not in by_tier["page"]
    b = opened(store, world, tmp_path, purpose="benchmark")
    b.call("retrieve", {"query": "offset", "cell_id": "$cell"})
    assert b.expert_ids == {f"b:{BENCH}:expert:1"}, "in the id scheme the model sees"


# ---------------------------------------------------------------- benchmark purpose


def test_a_benchmark_session_lets_nothing_that_places_the_ground_reach_the_model(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path, purpose="benchmark", blind=[BLIND_FILE])
    assert s.bench_id == BENCH and s.blind_list == [BLIND_FILE]
    for tool, args in (("cell_features", {"cell_id": "$cell"}), ("criteria_breakdown", {"cell_id": "$cell"}),
                       ("label_context", {"cell_id": "$cell", "radius_km": 25.0}),
                       ("retrieve", {"query": "graphitic conductor", "cell_id": "$cell"}),
                       ("cell_scores", {"cell_id": "$cell"}), ("nearby", {"cell_id": "$cell", "layer": "em_conductors"})):
        s.call(tool, args)
    assert [r["cell_id"] for r in sum(world.received.values(), [])] == [CELL] * 5, "every tool got the real cell"
    assert "cell_scores" not in world.received, "except the served scores, which a benchmark never asks for"
    text = everything(s)
    assert CELL not in text and "$cell" not in text
    assert re.search(r"\bc:[a-z]+:", text) is None, "no id in the real scheme survives"
    for leak in LEAKS:
        assert leak not in text, leak
    assert "[redacted]'s drilling at [redacted] followed the P2 conductor" in text, "the passage names no place"
    assert s.values and all(k.startswith(f"b:{BENCH}:") for k in s.values)
    assert f"b:{BENCH}:cell:d_conductor_m" in s.values and f"b:{BENCH}:near:1" in s.values
    assert s.calls[0]["args"] == {"cell_id": BENCH} and s.calls[0]["file"] == "tool_01_cell_features.json"
    assert sorted(s.calls[0]["ids"]) == [f"b:{BENCH}:cell:d_conductor_m", f"b:{BENCH}:cell:d_conductor_m:n_obs",
                                         f"b:{BENCH}:cell:holes_n"]
    assert s.refusals == {"B17": 1, "B18": 1, "B30": 1}
    assert (s.stage / "tool_04_retrieve.json").is_file()
    assert "Jefferson et al. 2007" not in text and "McArthur" not in text, \
        "the literature line goes: the executor reads the raw file, and the line names deposits"


def test_a_benchmark_session_refuses_a_cell_named_by_id_and_needs_a_bench_id(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path, purpose="benchmark")
    with pytest.raises(S.SessionRefusal) as err:
        s.call("cell_features", {"cell_id": "0000_0001"})
    assert err.value.rule == "purpose" and world.received["cell_features"] == []
    assert s.call("cell_features", {"cell_id": BENCH}).rows, "the id the model was shown is the placeholder's equal"
    with pytest.raises(S.SessionRefusal) as err:
        opened(store, world, tmp_path, purpose="benchmark", bench_id=None)
    assert err.value.rule == "purpose"
    with pytest.raises(S.SessionRefusal) as err:
        opened(store, world, tmp_path, purpose="hindcast")
    assert err.value.rule == "purpose"


def test_a_blinded_session_keeps_the_unscrubbed_result_in_a_private_file_the_model_never_sees(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    """Scrubbing drops the file numbers a dashboard cites from; the private copy keeps them, and it sits
    outside the glob a stage uses, so it can be read back without ever being staged."""
    s = opened(store, world, tmp_path)
    s.call("retrieve", {"query": "conductor", "cell_id": "$cell"})
    entry = s.calls[-1]
    assert entry["private_file"] == f"{S.PRIVATE_DIR}/{entry['file']}"
    shown = json.loads((s.stage / entry["file"]).read_text())
    raw = json.loads((s.stage / entry["private_file"]).read_text())
    assert all("file" not in r for r in shown["rows"]) and any("file" in r for r in raw["rows"])
    assert "exclude_files" in raw["args"] and "exclude_files" not in shown["args"]
    assert not list(s.stage.glob("tool_*.json")) == [], "the model-facing files are the top-level ones"
    assert all(p.parent == s.stage for p in s.stage.glob("tool_*.json"))
    live = S.Session.open(CELL, "dashboard", fold=None, switches=ALL_ON, stage=tmp_path / "live-private",
                          tools=fake_registry(world))
    live.call("retrieve", {"query": "conductor", "cell_id": "$cell"})
    assert "private_file" not in live.calls[-1] and not (live.stage / S.PRIVATE_DIR).exists()


def test_a_scored_session_keeps_the_real_cell_id_and_scrubs_the_names(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path)
    out = s.call("cell_features", {"cell_id": "$cell"})
    assert set(s.values) == {f"c:cell:{CELL}:d_conductor_m", f"c:cell:{CELL}:d_conductor_m:n_obs", f"c:cell:{CELL}:holes_n"}
    assert s.values[f"c:cell:{CELL}:d_conductor_m"]["id"] == f"c:cell:{CELL}:d_conductor_m"
    assert out.rows[0]["note"] == f"nearest conductor to cell {CELL}" and s.calls[0]["args"] == {"cell_id": CELL}
    s.call("retrieve", {"query": "conductor", "cell_id": "$cell"})
    text = everything(s)
    assert CELL in text
    for leak in ("Cameco", "McArthur River", "MC-361", FAR_FILE):
        assert leak not in text, leak


# ---------------------------------------------------------------- the gate, the switches, the spans


def test_check_claims_binds_to_the_ids_this_session_served(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path)
    s.call("cell_features", {"cell_id": "$cell"})
    assert s.check_claims([{"text": "The nearest conductor is 820 m away.", "value_ids": [f"c:cell:{CELL}:d_conductor_m"]}]) == []
    problems = s.check_claims([{"text": "Grades reached 2.4% U3O8.", "value_ids": []}])
    assert problems and "2.4" in problems[0]
    assert s.check_claims([{"text": "820 m", "value_ids": [f"c:cell:{CELL}:invented"]}])
    assert s.allowed_ids() == sorted(s.values)


def test_effort_off_drops_effort_rows_and_refuses_the_drillhole_layer(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path, switches=replace(ALL_ON, effort_features=False))
    out = s.call("cell_features", {"cell_id": "$cell"})
    assert [r["feature"] for r in out.rows] == ["d_conductor_m"]
    assert f"c:cell:{CELL}:holes_n" not in s.values and f"c:cell:{CELL}:d_conductor_m" in s.values
    with pytest.raises(S.SessionRefusal) as err:
        s.call("nearby", {"cell_id": "$cell", "layer": "compilation"})
    assert err.value.rule == "switch" and world.received["nearby"] == []
    assert s.call("nearby", {"cell_id": "$cell", "layer": "em_conductors"}).rows[0]["count"] == 4
    assert s.refusals == {"switch": 1} and s.effort_rows_dropped == 1, "one refused call; a dropped effort row is the switch working"


def test_criteria_off_refuses_the_breakdown(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path, switches=replace(ALL_ON, criteria=False))
    with pytest.raises(S.SessionRefusal) as err:
        s.call("criteria_breakdown", {"cell_id": "$cell"})
    assert err.value.rule == "switch" and world.received["criteria_breakdown"] == []
    with pytest.raises(T.ToolError, match="no tool named"):
        s.call("crystal_ball", {"cell_id": "$cell"})


def test_tool_spans_carry_the_purpose_and_the_argument_names_and_never_the_cell(
        store: Path, world: FakeWorld, tmp_path: Path) -> None:
    with trace("R1", "bench", run_dir=tmp_path / "R1"):
        s = opened(store, world, tmp_path, purpose="benchmark", switches=replace(ALL_ON, criteria=False))
        s.call("cell_features", {"cell_id": "$cell"})
        with pytest.raises(S.SessionRefusal):
            s.call("criteria_breakdown", {"cell_id": "$cell"})
    spans = [sp for sp in read_spans(tmp_path / "R1") if sp["kind"] == "tool"]
    assert [sp["name"] for sp in spans] == ["tool:cell_features", "tool:criteria_breakdown"]
    good, refused = spans
    assert good["attrs"] == {"tool": "cell_features", "purpose": "benchmark", "arg_keys": ["cell_id"], "n_values": 3, "n_rows": 2}
    assert refused["status"] == "error" and refused["attrs"]["rule"] == "switch"
    assert CELL not in json.dumps(spans) and BENCH not in json.dumps(spans)


def test_manifest_fields_name_what_the_manifest_records(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    s = opened(store, world, tmp_path)
    s.call("cell_scores", {"cell_id": "$cell"})
    s.call("retrieve", {"query": "conductor", "cell_id": "$cell"})
    m = s.manifest_fields()
    assert m["purpose"] == "scored" and m["fold"] == FOLD and m["blind_list_hash"] == s.blind_list_hash
    assert m["switches"] == {"drillholes": True, "label_context": True, "oof_scores": True, "effort_features": True, "criteria": True}
    assert m["scores_seen"] == s.scores_seen and m["n_values"] == len(s.values) == 5
    assert m["n_expert_ids"] == 1 and m["n_calls"] == 2
    assert m["refusals"] == {"B17": 1, "B18": 1, "B30": 0, "switch": 0, "purpose": 0}
    assert json.dumps(m)


def test_the_default_registry_is_the_live_one(store: Path, tmp_path: Path) -> None:
    s = S.Session.open(CELL, "dashboard", fold=None, switches=ALL_ON, stage=tmp_path / "live")
    out = s.call("cell_features", {"cell_id": "$cell"})
    assert out.tool == "cell_features" and f"c:cell:{CELL}:d_conductor_m" in s.values
    assert (tmp_path / "live" / "tool_01_cell_features.json").is_file()


def test_a_blinded_result_loses_the_literature_evidence_and_every_place_name(store: Path, world: FakeWorld, tmp_path: Path) -> None:
    """The frozen packs kept `evidence` because their renderer never printed it; the executor reads the raw
    file, so a blinded session drops the field and scrubs the handbook's own place names everywhere else."""
    from legacy_reader.prospect.tools import ToolResult

    def criteria_breakdown(cell_id: str) -> ToolResult:
        out = ToolResult("criteria_breakdown", {"cell_id": cell_id})
        out.rows = [{"criterion": "unconformity_depth", "state": "met", "weight": 2.0, "status": "assumed",
                     "evidence": "Cigar Lake 480 m, McArthur River 530 to 640 m",
                     "caveat": "drawn from deposits found in the Athabasca; survival bias"}]
        out.note = "Unknown and absent are different answers."
        return out

    tools = {**fake_registry(world), "criteria_breakdown": criteria_breakdown}
    for purpose in ("benchmark", "scored"):
        s = S.Session.open(CELL, purpose, fold=FOLD, switches=ALL_ON, bench_id="b-0009" if purpose == "benchmark" else None,
                           blind=[], stage=tmp_path / purpose, tools=tools, forbidden=set())
        s.call("criteria_breakdown", {"cell_id": "$cell"})
        text = everything(s)
        assert "evidence" not in json.loads((s.stage / "tool_01_criteria_breakdown.json").read_text())["rows"][0]
        for leak in ("Cigar", "McArthur", "Athabasca"):
            assert leak not in text, (purpose, leak)
        assert "survival bias" in text, "the caveat survives, scrubbed"
    live = S.Session.open(CELL, "dashboard", fold=None, switches=ALL_ON, stage=tmp_path / "live", tools=tools)
    live.call("criteria_breakdown", {"cell_id": "$cell"})
    assert "Cigar Lake" in everything(live), "the dashboard is not closed-book"

"""The interface benchmark's two mechanical tiers, on fixtures: the generators, the abstention gold, the
stratification, the determinism, the audit, and the reader's session rules. No store is opened; one test
audits the shipped v1 and skips when the store or the build is absent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from uranium_explorer.bench import dataset as D
from uranium_explorer.bench.interface import build as B
from uranium_explorer.bench.interface import items as I
from uranium_explorer.bench.interface import spec as S
from uranium_explorer.bench.interface import tier1, tier3
from uranium_explorer.bench.interface.reader import NO_SUCH_CELL, StoreReader
from uranium_explorer.bench.spec import SpecError
from uranium_explorer.prospect.tools import ToolResult
from uranium_explorer.values import stat
from fake_bench import CELLS, make_bench

SPEC = """
version = "{version}"
seed = {seed}
analyst_version = "vtest"
split = "open"

[tier1]
nearby_radius_m = 5000
label_radius_km = 25
nearby_layers = ["em_conductors", "faults_250k", "radioactive_boulders"]

[tier1.kinds]
nearest_deposit_km = 4
nearest_occurrence_km = 4
criteria_unknown = 4
criteria_not_met = 4
criterion_state = 4
coverage_share = 2
coverage_thin = 2
nearby_count = 4
nearby_nearest = 4
nearest_conductor_m = 4
score = 4
holes_count = 4
feature_value = 4
observations = 4
crosscheck_crossings = 4
thin_sampling = 4
not_measured = 4
outside_grid = 2
no_value = 4
out_of_scope = 4

[tier3.sources]
digit_slip = 4
decimal_shift = 4
transposed = 4
false_precision = 4
hand_conversion = 4
invented = 4
uncited = 4
wrong_cell = 4
obs_count_no_id = 4
filename_as_id = 4
negated_premise = 4
folklore_as_fact = 4
grade_request = 4
absence_as_absent = 4
"""

#: the cells the fake world knows: the six benchmark cells and one neighbour of each along the row
KNOWN = {cid for cid, *_ in CELLS.values()} | {f"0002_{i:04d}" for i in range(1, 7)}
FOLD = {cid: fold for cid, _label, fold, _split in CELLS.values()}


def _n(cell_id: str) -> int:
    return int(cell_id.split("_")[1]) + (10 if cell_id.startswith("0002") else 0)


class FakeReader:
    """Tool results in the live tools' shapes, computed from the cell id, so every gold is known beforehand."""

    def __init__(self, blind: dict[str, list[str]] | None = None) -> None:
        self.blind_lists = blind if blind is not None else {b: ["74H09-0039"] for b in CELLS}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def blind(self, bench_id: str) -> list[str]:
        return list(self.blind_lists.get(bench_id, []))

    def call(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((tool, dict(args)))
        cid = str(args.get("cell_id", ""))
        if tool == "coverage":
            return self._coverage(args.get("feature_key"))
        if cid not in KNOWN:
            note = NO_SUCH_CELL if tool in ("label_context", "nearby", "crosscheck", "cell_scores") else ""
            return ToolResult(tool, dict(args), note=note)
        return getattr(self, f"_{tool}")(cid, args)

    def _cell_features(self, cid: str, args: dict[str, Any]) -> ToolResult:
        n = _n(cid)
        out = ToolResult("cell_features", {"cell_id": cid})

        def row(key: str, title: str, value: float | None, unit: str | None, obs: int, effort: bool = False,
                nearest: float | None = None, text: str | None = None) -> None:
            r: dict[str, Any] = {"feature": key, "title": title, "is_effort": effort, "observations": obs}
            if obs:
                oid = f"c:cell:{cid}:{key}:n_obs"
                out.values[oid] = stat(oid, obs, note=f"observations behind {title}")
                r["observations_id"] = oid
            if value is not None:
                vid = f"c:cell:{cid}:{key}"
                out.values[vid] = stat(vid, value, fmt="m1", unit=unit, note=title)
                r["value_id"], r["value"], r["unit"] = vid, value, unit
            else:
                r["value"], r["missing"] = None, "no observation; this is not a low value"
            if text:
                r["text"] = text
            if nearest is not None and value is None:
                nid = f"c:cell:{cid}:{key}:nearest_m"
                out.values[nid] = stat(nid, nearest, fmt="m1", unit="m")
                r["nearest_observation_id"] = nid
            out.rows.append(r)

        row("d_conductor_m", "Distance to nearest EM conductor", 500.0 + 137.0 * n, "m", 3)
        row("d_fault_m", "Distance to nearest mapped fault or lineament", 1200.0 + 251.0 * n, "m", 2)
        row("unconformity_depth_m", "Interpolated depth to the base of the Athabasca sandstone", 300.0 + 10.0 * n, "m", 4)
        row("elevation_m", "Elevation m", 450.0 + n, "m", 1)
        # a density: computed from a line layer, so it has a value and no observation count, as the store's do
        row("fault_density", "Fault and lineament length per km2", round(0.4 + n / 100, 4), None, 0)
        row("sed_u_max_ppm", "Highest lake-sediment uranium within 5 km", None, "ppm", 0, nearest=4200.0 + n)
        row("water_u_max_ppm", "Highest lake-water uranium within 5 km", None, "ppm", 0)
        row("surficial_class", "Dominant surficial environment", None, None, 1, text="Till veneer")
        row("holes_n", "Provincial drillhole collars within 2 km", float(n % 3 * 7), None, 1, effort=True)
        return out

    def _cell_scores(self, cid: str, args: dict[str, Any]) -> ToolResult:
        out = ToolResult("cell_scores", {"cell_id": cid})
        for model, s in (("criteria", 0.61), ("effort", 0.42), ("learned", 0.33)):
            vid = f"c:score:{cid}:{model}"
            out.values[vid] = stat(vid, round(s + _n(cid) / 100, 4), fmt="ratio3")
            out.rows.append({"model": model, "fold": FOLD.get(cid, 0), "out_of_fold": True, "score_id": vid,
                             "score": round(s + _n(cid) / 100, 4)})
        return out

    def _criteria_breakdown(self, cid: str, args: dict[str, Any]) -> ToolResult:
        out = ToolResult("criteria_breakdown", {"cell_id": cid})
        for key, title, status, weight, member in (
                ("conductor_proximity", "Close to a mapped EM conductor", "assumed", 3.0, 0.9),
                ("fault_proximity", "Close to a mapped fault or lineament", "assumed", 2.0, 0.2),
                ("lake_water_uranium", "Uranium anomaly in lake water nearby", "assumed", 1.0, None),
                ("conductor_strength", "Stronger conductors are better", "folklore", 0.0, 0.7)):
            wid = f"c:crit:{cid}:{key}:weight"
            out.values[wid] = stat(wid, weight, fmt="m2")
            r: dict[str, Any] = {"criterion": key, "title": title, "status": status, "weight": weight, "weight_id": wid}
            if member is None:
                r["state"] = "unknown"
            else:
                vid = f"c:crit:{cid}:{key}"
                out.values[vid] = stat(vid, member, fmt="ratio3")
                r["membership_id"], r["membership"], r["state"] = vid, member, "met" if member >= 0.5 else "not met"
            out.rows.append(r)
        return out

    def _label_context(self, cid: str, args: dict[str, Any]) -> ToolResult:
        out = ToolResult("label_context", {"cell_id": cid})
        for i, (tier, km) in enumerate((("occurrence", 3.1 + _n(cid)), ("deposit", 12.4 + _n(cid)))):
            vid = f"c:near:{cid}:{i}"
            out.values[vid] = stat(vid, km, fmt="m2", unit="km")
            out.rows.append({"rank": i + 1, "tier": tier, "distance_km_id": vid, "distance_km": km})
        return out

    def _coverage(self, key: str | None) -> ToolResult:
        out = ToolResult("coverage", {"feature_key": key})
        for feat, share, thin in (("d_conductor_m", 1.0, False), ("water_u_max_ppm", 0.026, True), ("holes_n", 1.0, False)):
            if key and feat != key:
                continue
            vid = f"c:cov:{feat}"
            out.values[vid] = stat(vid, share, fmt="ratio3")
            out.rows.append({"feature": feat, "coverage_id": vid, "coverage": share, "thin": thin, "is_effort": feat == "holes_n"})
        return out

    def _nearby(self, cid: str, args: dict[str, Any]) -> ToolResult:
        layer, radius = str(args["layer"]), float(args["radius_m"])
        base = f"c:nb:{cid}:{layer}:{radius:g}"
        out = ToolResult("nearby", dict(args))
        n_within = {"em_conductors": 3, "faults_250k": 0, "radioactive_boulders": 2}.get(layer, 1)
        out.values[f"{base}:n_within"] = stat(f"{base}:n_within", n_within)
        row: dict[str, Any] = {"layer": layer, "is_effort": False, "n_within": n_within, "n_within_id": f"{base}:n_within"}
        if n_within:
            out.values[f"{base}:nearest_m"] = stat(f"{base}:nearest_m", 820.0 + _n(cid), fmt="m1", unit="m")
            row["nearest_m"], row["nearest_m_id"] = 820.0 + _n(cid), f"{base}:nearest_m"
        out.rows.append(row)
        return out

    def _crosscheck(self, cid: str, args: dict[str, Any]) -> ToolResult:
        out = ToolResult("crosscheck", {"cell_id": cid})
        x = f"c:x:{cid}:conductor_fault"
        out.values[f"{x}:crossings_n"] = stat(f"{x}:crossings_n", _n(cid) % 4)
        out.rows.append({"pair": "conductor_fault", "radius_m": 5000.0, "crossings_n": _n(cid) % 4,
                         "crossings_n_id": f"{x}:crossings_n", "state": "known"})
        s = f"c:x:{cid}:sediment_sampling"
        out.values[f"c:cell:{cid}:sed_samples_n"] = stat(f"c:cell:{cid}:sed_samples_n", 1.0)
        out.values[f"{s}:min_samples"] = stat(f"{s}:min_samples", 3)
        out.rows.append({"pair": "sediment_sampling", "sed_samples_n": 1.0, "sed_samples_n_id": f"c:cell:{cid}:sed_samples_n",
                         "min_samples": 3, "min_samples_id": f"{s}:min_samples", "thin_sampling": True, "state": "unknown"})
        return out


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A six-cell analyst benchmark on disk, a spec directory and a fake reader."""
    bench = make_bench(tmp_path)
    pd.DataFrame([{"bench_id": b, "model": m, "fold_kind": "spatial", "fold": FOLD[c[0]], "score": 0.5}
                  for b, c in CELLS.items() for m in ("criteria", "effort", "learned")]).to_csv(bench.dir / "oof_scores.csv", index=False)
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "interface-t1.toml").write_text(SPEC.format(version="t1", seed=7))
    (configs / "interface-t2.toml").write_text(SPEC.format(version="t2", seed=8))
    monkeypatch.setattr(S, "CONFIG_DIR", configs)
    return {"bench": bench, "reader": FakeReader(), "root": tmp_path / "interface"}


def ctx_for(world) -> I.Ctx:
    spec = S.load_spec("t1")
    analyst = B.load_analyst("vtest", "open", world["bench"].dir)
    return B.make_ctx(spec, analyst, world["reader"])


def cell(bench_id: str) -> dict[str, Any]:
    cid, label, fold, split = CELLS[bench_id]
    return {"bench_id": bench_id, "cell_id": cid, "stratum": label, "fold": fold, "split": split}


# ---------------------------------------------------------------- the spec


def test_shipped_v1_spec_loads_and_sizes_the_tiers() -> None:
    spec = S.load_spec("v1")
    assert spec.version == "v1" and spec.analyst_version == "v2" and spec.split == "open"
    assert set(spec.tier1.kinds) == set(tier1.KINDS) and set(spec.tier3.sources) == set(tier3.SOURCES)
    assert 280 <= sum(spec.tier1.kinds.values()) <= 320    # about 300, PRD §D.3.1
    assert 90 <= sum(spec.tier3.sources.values()) <= 120    # about 100
    assert sum(spec.tier1.kinds[k] for k in ("not_measured", "outside_grid", "no_value", "out_of_scope")) >= 40


def test_spec_is_strict(world) -> None:
    import tomllib

    raw = tomllib.loads(SPEC.format(version="t1", seed=7))
    bad = dict(raw)
    bad["sed"] = 1
    with pytest.raises(SpecError, match="unknown top-level"):
        S.parse_spec(bad, "t1")
    bad = json.loads(json.dumps(raw))
    bad["tier1"]["kinds"]["nearest_deposit"] = 3
    with pytest.raises(SpecError, match="unknown name"):
        S.parse_spec(bad, "t1")
    bad = json.loads(json.dumps(raw))
    bad["tier3"]["sources"]["imagined"] = 3
    with pytest.raises(SpecError, match="unknown name"):
        S.parse_spec(bad, "t1")
    bad = json.loads(json.dumps(raw))
    bad["tier1"]["nearby_layers"] = ["uranium_deposit_footprints"]
    with pytest.raises(SpecError, match="not an evidence layer"):
        S.parse_spec(bad, "t1")


# ---------------------------------------------------------------- tier 1 generators


def test_every_tier1_kind_yields_and_its_gold_resolves_in_the_tools(world) -> None:
    import random

    ctx = ctx_for(world)
    rng = random.Random(1)
    for name, kind in tier1.KINDS.items():
        item = kind.make(ctx, None if not kind.cell_based else cell("b01"), rng, None)
        assert item is not None, name
        assert item["kind"] == name and item["tier"] == 1
        gold = item["gold"]
        assert gold["answer"] in I.ANSWERS
        if kind.answerable:
            assert gold["answer"] != "abstain" and (gold["value_ids"] or gold["keys"] is not None)
        else:
            assert gold["answer"] == "abstain" and gold["reason"] in I.REASONS
        # every id the gold cites is one the recorded tool calls return, with the same value
        returned: dict[str, Any] = {}
        for call in item["tools"]:
            args = dict(call["args"])
            if item["cell_id"] and args.get("cell_id") == item["bench_id"]:
                args["cell_id"] = item["cell_id"]
            returned |= ctx.reader.call(call["tool"], args).values
        for vid, value in gold["values"].items():
            assert returned[vid]["value"] == value, (name, vid)
        if item["bench_value_ids"] is not None:
            assert all(v.startswith(f"b:{item['bench_id']}:") for v in item["bench_value_ids"])


def test_abstention_gold_names_the_reason_and_the_evidence(world) -> None:
    import random

    ctx = ctx_for(world)
    c = cell("b01")
    unmeasured = tier1.KINDS["not_measured"].make(ctx, c, None, {"feature": "sed_u_max_ppm"})
    assert unmeasured["gold"]["reason"] == "not_measured"
    assert unmeasured["gold"]["value_ids"] == [f"c:cell:{c['cell_id']}:sed_u_max_ppm:nearest_m"]
    assert tier1.KINDS["not_measured"].make(ctx, c, None, {"feature": "d_conductor_m"}) is None   # measured: not this kind
    unknown = tier1.KINDS["not_measured"].make(ctx, c, None, {"criterion": "lake_water_uranium"})
    assert unknown["gold"]["reason"] == "not_measured" and unknown["gold"]["value_ids"] == []
    assert tier1.KINDS["not_measured"].make(ctx, c, None, {"criterion": "conductor_proximity"}) is None
    outside = tier1.KINDS["outside_grid"].make(ctx, None, random.Random(3), None)
    assert outside["gold"]["reason"] == "outside_grid" and outside["cell_id"] is None
    assert outside["bench_id"] not in ctx.bench_ids and outside["stratum"] == I.NONE
    assert tier1.KINDS["outside_grid"].make(ctx, None, None, {"bench_id": "b01", "tool": "label_context"}) is None
    no_value = tier1.KINDS["no_value"].make(ctx, c, None, {"quantity": "grade"})
    assert no_value["gold"]["reason"] == "no_value" and "grade" in no_value["question"]
    scope = tier1.KINDS["out_of_scope"].make(ctx, c, None, {"topic": "holder"})
    assert scope["gold"]["reason"] == "out_of_scope" and scope["tools"] == []


def test_set_gold_is_the_key_set_with_the_ids_that_exist(world) -> None:
    ctx = ctx_for(world)
    c = cell("b03")
    unknown = tier1.KINDS["criteria_unknown"].make(ctx, c, None, None)
    assert unknown["gold"]["keys"] == ["lake_water_uranium"] and unknown["gold"]["value_ids"] == []
    not_met = tier1.KINDS["criteria_not_met"].make(ctx, c, None, None)
    assert not_met["gold"]["keys"] == ["fault_proximity"]
    assert not_met["gold"]["value_ids"] == [f"c:crit:{c['cell_id']}:fault_proximity"]


# ---------------------------------------------------------------- the build


def test_build_stratifies_over_open_cells_and_never_touches_the_held_out_one(world) -> None:
    m = B.build("t1", log=lambda _m: None, root=world["root"], reader=world["reader"], adir=world["bench"].dir)
    t1 = B.load_items("t1", 1, world["root"])
    t3 = B.load_items("t1", 3, world["root"])
    assert m["counts"]["tier1"]["items"] == len(t1) and m["counts"]["tier3"]["items"] == len(t3)
    assert all(it["bench_id"] != "b06" for it in t1 + t3)                      # held out stays sealed
    for name, kind in tier1.KINDS.items():
        mine = [it for it in t1 if it["kind"] == name]
        if kind.cell_based and mine:
            per = {s: sum(1 for it in mine if it["stratum"] == s) for s in ("deposit", "occurrence", "negative", "probe")}
            assert max(per.values()) - min(per.values()) <= 1, (name, per)     # an equal share per stratum
    assert {it["stratum"] for it in t1 if it["kind"] == "coverage_share"} == {I.GRID}
    assert {it["stratum"] for it in t1 if it["kind"] == "outside_grid"} == {I.NONE}
    assert m["counts"]["tier1"]["reasons"]["outside_grid"] == 2
    assert m["analyst"]["oof_source"] == "oof_scores.csv" and m["analyst"]["cells_drawable"] == 5
    assert m["session_rules"] == ["B17", "B18", "B30"] and "not run" in m["status"]
    ids = [it["id"] for it in t1]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_build_is_deterministic_and_the_seed_moves_it(world) -> None:
    quiet = lambda _m: None  # noqa: E731
    a = B.build("t1", log=quiet, root=world["root"] / "a", reader=world["reader"], adir=world["bench"].dir)
    b = B.build("t1", log=quiet, root=world["root"] / "b", reader=FakeReader(), adir=world["bench"].dir)
    assert a["content_sha256"] == b["content_sha256"]
    for name in B.TIER_FILES:
        assert (world["root"] / "a" / "t1" / name).read_bytes() == (world["root"] / "b" / "t1" / name).read_bytes()
    c = B.build("t2", log=quiet, root=world["root"] / "c", reader=FakeReader(), adir=world["bench"].dir)
    assert c["content_sha256"] != a["content_sha256"]


def test_tier3_items_cite_their_source_and_never_land_on_the_truth(world) -> None:
    B.build("t1", log=lambda _m: None, root=world["root"], reader=world["reader"], adir=world["bench"].dir)
    t3 = B.load_items("t1", 3, world["root"])
    assert t3, "the fake world yields adversarial items"
    by_source = {it["source"] for it in t3}
    assert by_source <= set(tier3.SOURCES)
    ctx = ctx_for(world)
    for it in t3:
        src = tier3.SOURCES[it["source"]]
        assert it["reference"] == src.reference and it["family"] == src.family
        assert it["gold"]["behaviour"] in tier3.BEHAVIOURS
        if it["source"] in ("digit_slip", "decimal_shift", "transposed", "false_precision", "hand_conversion", "invented", "wrong_cell"):
            vid = it["gold"]["value_ids"][0]
            val = ctx.reader.call("cell_features", {"cell_id": it["cell_id"]}).values[vid]
            assert not I.is_true(str(it["choice"]["token"] if "token" in it["choice"] else it["neighbour"]["value"]), val)
        if it["source"] == "wrong_cell":
            assert it["neighbour"]["cell_id"] in tier3.neighbour_ids(it["cell_id"])
            assert it["neighbour"]["value_id"] != it["gold"]["value_ids"][0]
        if it["source"] == "filename_as_id":
            assert it["choice"]["file"] in world["reader"].blind(it["bench_id"])
        if it["source"] == "absence_as_absent":
            assert it["gold"]["values"][it["gold"]["value_ids"][0]] == 0
        if it["source"] == "folklore_as_fact":
            assert it["gold"]["values"][it["gold"]["value_ids"][0]] == 0.0
        if it["source"] == "grade_request":
            assert it["gold"]["behaviour"] == "decline_grade" and it["gold"]["reason"] == "no_value"
    assert {it["gold"]["behaviour"] for it in t3 if it["source"] == "obs_count_no_id"} >= {"cite_id", "abstain"}
    # a source with two forms comes out even by construction, not by a coin
    forms = [it["choice"]["form"] for it in t3 if it["source"] == "negated_premise"]
    assert sorted(forms) == ["criterion", "criterion", "nearby", "nearby"]


def test_obs_count_gold_follows_the_tool_row(world) -> None:
    """A count id: cite it. A value with no count id (a density): the count is a value the store lacks. No
    value at all: not measured. The zero the tool prints is never the answer in the last two."""
    ctx = ctx_for(world)
    c = cell("b01")
    make = tier3.SOURCES["obs_count_no_id"].make
    counted = make(ctx, c, None, {"feature": "d_conductor_m"})
    assert counted["gold"]["behaviour"] == "cite_id" and counted["gold"]["value_ids"] == [f"c:cell:{c['cell_id']}:d_conductor_m:n_obs"]
    density = make(ctx, c, None, {"feature": "fault_density"})
    assert density["gold"]["behaviour"] == "abstain" and density["gold"]["reason"] == "no_value" and density["gold"]["value_ids"] == []
    unmeasured = make(ctx, c, None, {"feature": "water_u_max_ppm"})
    assert unmeasured["gold"]["behaviour"] == "abstain" and unmeasured["gold"]["reason"] == "not_measured"
    assert make(ctx, c, None, {"feature": "holes_n"}) is None          # effort features are not asked
    assert make(ctx, c, None, {"feature": "surficial_class"}) is None  # a mapped class has no count to quote


def test_corruptions_skip_the_truth_and_scientific_notation(world) -> None:
    ctx = ctx_for(world)
    c = cell("b01")
    shift = tier3.SOURCES["decimal_shift"].make
    assert shift(ctx, c, None, {"feature": "d_conductor_m", "token": "3e-05"}) is None   # not how anyone writes a premise
    assert shift(ctx, c, None, {"feature": "d_conductor_m", "token": "637.0"}) is None   # landed on the truth (637 m)
    item = shift(ctx, c, None, {"feature": "d_conductor_m", "token": "6370"})
    assert item["premise"] == "Distance to nearest EM conductor at cell b01 is 6370 m."
    assert item["gold"]["values"] == {f"c:cell:{c['cell_id']}:d_conductor_m": 637.0}
    conv = tier3.SOURCES["hand_conversion"].make(ctx, c, None, {"feature": "d_conductor_m", "token": "6.37"})
    assert conv["premise"].endswith("is 6.37 km.")                                       # metres over 100, said in km
    assert tier3.SOURCES["hand_conversion"].make(ctx, c, None, {"feature": "holes_n", "token": "0.07"}) is None  # no unit to convert


def test_negated_nearby_premise_needs_something_inside_the_radius(world) -> None:
    import random

    ctx = ctx_for(world)
    c = cell("b02")
    make = tier3.SOURCES["negated_premise"].make
    assert make(ctx, c, None, {"form": "nearby", "layer": "faults_250k"}) is None       # nothing mapped: nothing to negate
    item = make(ctx, c, None, {"form": "nearby", "layer": "em_conductors"})
    assert item["premise"].startswith("No em_conductors feature is mapped within 5000 m of cell b02")
    assert item["gold"]["behaviour"] == "correct_premise" and list(item["gold"]["values"].values()) == [3]
    # drawn blind, the nearby form only ever lands on a layer with something inside the radius
    for seed in range(12):
        drawn = make(ctx, c, random.Random(seed), None)
        if drawn is not None and drawn["choice"]["form"] == "nearby":
            assert drawn["choice"]["layer"] != "faults_250k"
    assert make(ctx, c, None, {"form": "criterion", "criterion": "conductor_strength"}) is None   # folklore is never negated
    assert make(ctx, c, None, {"form": "criterion", "criterion": "fault_proximity"}) is None      # not met: nothing to negate


def test_a_source_that_cannot_yield_is_a_shortfall_not_padding(world) -> None:
    reader = FakeReader(blind={b: [] for b in CELLS})      # no file near any cell: nothing to cite as an id
    m = B.build("t1", log=lambda _m: None, root=world["root"], reader=reader, adir=world["bench"].dir)
    assert m["shortfalls"]["tier3"]["filename_as_id"] == 4
    assert not any(it["source"] == "filename_as_id" for it in B.load_items("t1", 3, world["root"]))


def test_wording_rule_is_enforced(world) -> None:
    with pytest.raises(I.ItemError, match="banned wording"):
        I.check_wording("Is this a drill " + "target?")   # joined so the phrase is not written anywhere in the tree
    with pytest.raises(I.ItemError, match="real cell id"):
        I.check_wording("How far is cell 0001_0001 from a conductor?")
    B.build("t1", log=lambda _m: None, root=world["root"], reader=world["reader"], adir=world["bench"].dir)
    for tier in (1, 3):
        for it in B.load_items("t1", tier, world["root"]):
            I.check_wording(it["question"], it.get("premise"), it["gold"]["note"])


def test_audit_is_clean_and_catches_a_changed_gold(world) -> None:
    B.build("t1", log=lambda _m: None, root=world["root"], reader=world["reader"], adir=world["bench"].dir)
    assert B.audit("t1", root=world["root"], reader=world["reader"], adir=world["bench"].dir) == []
    path = world["root"] / "t1" / "tier1.jsonl"
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    vid = first["gold"]["value_ids"][0] if first["gold"]["value_ids"] else None
    if vid is None:
        first["gold"]["reason"] = "no_value"
    else:
        first["gold"]["values"][vid] = -1
    lines[0] = json.dumps(first, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    problems = B.audit("t1", root=world["root"], reader=world["reader"], adir=world["bench"].dir)
    assert any("hash differs" in p for p in problems)
    assert any(p.startswith(first["id"]) and "differs from its regeneration" in p for p in problems)


# ---------------------------------------------------------------- the reader's session rules


def test_store_reader_applies_the_mask_and_serves_the_fold_out_of_fold(world) -> None:
    seen: list[dict[str, Any]] = []

    def label_context(cell_id: str, radius_km: float = 25.0, mask_cell: str | None = None) -> ToolResult:
        seen.append({"cell_id": cell_id, "mask_cell": mask_cell})
        out = ToolResult("label_context", {"cell_id": cell_id})
        for i, km in enumerate((0.0, 4.2)):
            vid = f"c:near:{cell_id}:{i}"
            out.values[vid] = stat(vid, km, fmt="m2", unit="km")
            out.rows.append({"rank": i + 1, "tier": "deposit", "distance_km_id": vid, "distance_km": km})
        return out

    cells = [cell(b) for b in CELLS]
    oof = pd.DataFrame([{"cell_id": c["cell_id"], "model": m, "fold_kind": "spatial", "fold": f, "score": 0.1 * f}
                        for c in cells for m in ("learned",) for f in (0, 1)])
    reader = StoreReader(cells, oof=oof, blind={"b01": ["74H09-0039"]}, tools={"label_context": label_context})
    r = reader.call("label_context", {"cell_id": "0001_0001", "radius_km": 25.0})
    assert seen[-1]["mask_cell"] == "0001_0001"                      # B30: asked with the cell masked
    assert [row["distance_km"] for row in r.rows] == [4.2] and "c:near:0001_0001:0" not in r.values
    assert reader.call("label_context", {"cell_id": "0001_0001", "radius_km": 25.0}) is r and len(seen) == 1  # memoised
    s = reader.call("cell_scores", {"cell_id": "0001_0002"})          # b02 is in fold 1
    assert [(row["model"], row["fold"], row["score"]) for row in s.rows] == [("learned", 1, 0.1)]   # B18: this fold only
    assert reader.blind("b01") == ["74H09-0039"] and reader.blind("b02") == []


# ---------------------------------------------------------------- the shipped build


def require_shipped(version: str) -> None:
    """Skip unless the dataset directory holds this interface build and the store is there to audit it against:
    a public clone has neither, and the test must say so rather than fail."""
    from uranium_explorer.store import db_path

    if not B.is_built(version):
        pytest.skip(f"interface {version} is not in the dataset directory {D.knowledge_dir()}")
    if not db_path().is_file():
        pytest.skip("no store to audit against")


def test_shipped_v1_audits_clean_against_the_store() -> None:
    require_shipped("v1")
    problems = B.audit("v1", limit=30)
    assert problems == []


def test_the_shipped_audit_skips_cleanly_when_the_dataset_is_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(D.ENV, str(tmp_path))                   # an empty dataset directory: a public clone
    with pytest.raises(pytest.skip.Exception, match="not in the dataset directory"):
        test_shipped_v1_audits_clean_against_the_store()

"""The analyst's two spatial tools and the label mask: counts and distances computed on hand-placed geometry,
never reasoned; every number under an id; labels refused; the evaluated cell's own label hidden from a run.

The world is a few features in grid metres round one cell, so every distance the tools should return is known
to the metre before the tool runs."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import LineString, Point, box, mapping

from legacy_reader import index as IX
from legacy_reader import store as ST
from legacy_reader.prospect import tools as T
from bench_store import NOW, bench_frame, make_bench_store

CELL, FAR = "0000_0000", "0000_0001"
CX, CY = 500_000.0, 6_400_000.0
PLACING_KEYS = {"lon", "lat", "x", "y", "cx", "cy", "easting", "northing", "bbox", "geometry", "coordinates"}


def feature(geom, **props) -> dict:
    return {"type": "Feature", "geometry": mapping(geom), "bbox": list(geom.bounds), "properties": props}


#: hand-placed geometry in grid metres round CELL; FAR sits 60 km east and sees none of it
WORLD: dict[str, list[dict]] = {
    "em_conductors": [
        feature(LineString([(CX - 3000, CY - 1000), (CX + 3000, CY - 1000)]), text="TEM"),   # 1000 m south
        feature(LineString([(CX + 8000, CY), (CX + 9000, CY + 500)])),                       # 8000 m east
    ],
    # 2000 m east, north-south, so it crosses the first conductor at (CX + 2000, CY - 1000)
    "faults_250k": [feature(LineString([(CX + 2000, CY - 4000), (CX + 2000, CY + 4000)]), text="Fault")],
    "radioactive_boulders": [
        feature(Point(CX + 300, CY + 400), value=450.0, text="SANDSTONE"),   # 500 m
        feature(Point(CX - 1500, CY), value=1200.0),                         # 1500 m
        feature(Point(CX, CY + 2500), value=None),                           # 2500 m, no reading
        feature(Point(CX + 30_000, CY), value=9000.0),                       # outside every radius
    ],
    # one sample 4000 m east: inside the 5 km footprint halo, outside a 2500 m radius
    "lake_sediment_gsc": [feature(Point(CX + 4000, CY), value=3.0)],
    "bedrock_250k": [feature(box(CX - 5000, CY - 5000, CX + 5000, CY + 5000), text="Quartz arenite")],
    "compilation": [feature(Point(CX + 100, CY), text="Uranium"), feature(Point(CX, CY - 4000))],
}


def _add_feature(db: Path, cell_id: str, key: str, value: float | None, n_obs: int, nearest: float) -> None:
    """One more `derived.cell_feature` row (and its spec), the shape the feature builder writes."""
    con = ST.connect(db)
    try:
        con.execute(
            "insert into derived.cell_feature (cell_id, feature_key, value, value_text, unit, n_obs, nearest_m, "
            "from_tier, op, tool, params, inputs, computed_at) "
            "values (?, ?, ?, null, 'ppm', ?, ?, 'native', 'test', 'test', null, null, ?)",
            [cell_id, key, value, n_obs, nearest, NOW],
        )
        con.execute(
            "insert or ignore into derived.feature_spec (feature_key, title, unit, from_tier, source_keys, bears_on, "
            "is_effort, is_label, is_count, notes) "
            "values (?, 'highest lake-sediment uranium within 5 km', 'ppm', 'native', '[\"lake_sediment_gsc\"]', "
            "'detection', false, false, false, null)",
            [key],
        )
    finally:
        con.close()


@pytest.fixture
def world(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Eight cells, the first at the centre of the hand-placed world, the second far from it; the layer reader
    replaced by the world itself, already in grid metres, so no reprojection blurs a distance. The footprint
    cache is cleared on both sides, because it is built from whatever `layer_features` returned last."""
    df = bench_frame(n_dep=2, n_occ=2, n_neg=2, n_probe=2)
    df.loc[0, ["cx", "cy"]] = (CX, CY)
    df.loc[1, ["cx", "cy"]] = (CX + 60_000, CY)
    df.loc[0, "sed_samples_n"], df.loc[1, "sed_samples_n"] = 2.0, 5.0
    make_bench_store(prospect_sandbox.db, df)
    _add_feature(prospect_sandbox.db, CELL, "sed_u_max_ppm", 12.5, 2, 800.0)
    _add_feature(prospect_sandbox.db, FAR, "sed_u_max_ppm", None, 0, 4200.0)
    monkeypatch.setattr(ST, "db_path", lambda: prospect_sandbox.db)
    monkeypatch.setattr(T, "layer_features", lambda layer: WORLD.get(layer, []))
    T.layer_footprint.cache_clear()
    yield prospect_sandbox.db
    T.layer_footprint.cache_clear()


def assert_every_number_has_an_id(result: T.ToolResult) -> None:
    """The contract every tool keeps: a numeric leaf under `k` is paired with `k_id`, and that id resolves to
    the same number in the result's registry. Booleans are flags, not numbers."""
    def walk(node: dict) -> None:
        for key, v in node.items():
            if isinstance(v, dict):
                walk(v)
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            vid = node.get(f"{key}_id")
            assert vid, f"{key}={v} is a number with no id"
            assert vid in result.values and result.values[vid]["value"] == v, key
    for row in result.rows:
        walk(row)


# ---------------------------------------------------------------- nearby


def test_counts_nearest_distance_and_readings_are_exact(world: Path) -> None:
    out = T.nearby(CELL, "radioactive_boulders", radius_m=5000, k=2)
    summary, first, second = out.rows
    base = f"c:nb:{CELL}:radioactive_boulders:5000"
    assert summary["n_within"] == 3 and summary["n_within_id"] == f"{base}:n_within"
    assert summary["nearest_m"] == 500.0 and summary["nearest_m_id"] == f"{base}:nearest_m"
    assert first["dist_m"] == 500.0 and first["dist_m_id"] == f"{base}:0:dist_m"
    assert first["cps"] == 450.0 and first["cps_id"] == f"{base}:0:cps" and first["text"] == "SANDSTONE"
    assert second["dist_m"] == 1500.0 and second["cps"] == 1200.0 and "text" not in second
    assert out.values[f"{base}:0:cps"]["unit"] == "cps" and out.values[f"{base}:0:dist_m"]["unit"] == "m"
    assert out.args == {"cell_id": CELL, "layer": "radioactive_boulders", "radius_m": 5000.0, "k": 2}


def test_the_k_nearest_come_sorted_and_a_feature_without_a_reading_says_so(world: Path) -> None:
    out = T.nearby(CELL, "radioactive_boulders", radius_m=5000, k=5)
    rows = out.rows[1:]
    assert [r["dist_m"] for r in rows] == [500.0, 1500.0, 2500.0], "three inside 5 km, nearest first"
    assert "cps" not in rows[2] and "cps_id" not in rows[2] and "missing" in rows[2]
    assert len(T.nearby(CELL, "radioactive_boulders", radius_m=5000, k=0).rows) == 1, "k=0 is the summary alone"


def test_the_radius_changes_the_count_and_is_part_of_every_id(world: Path) -> None:
    near = T.nearby(CELL, "em_conductors", radius_m=5000)
    wide = T.nearby(CELL, "em_conductors", radius_m=10_000)
    odd = T.nearby(CELL, "em_conductors", radius_m=2500.5)
    assert near.rows[0]["n_within"] == 1 and near.rows[0]["nearest_m"] == 1000.0
    assert wide.rows[0]["n_within"] == 2 and wide.rows[2]["dist_m"] == 8000.0
    assert all(f":em_conductors:5000:" in vid for vid in near.values)
    assert all(f":em_conductors:10000:" in vid for vid in wide.values)
    assert all(f":em_conductors:2500.5:" in vid for vid in odd.values)
    assert near.rows[1]["text"] == "TEM"


def test_zero_inside_the_radius_is_an_answer_with_an_id(world: Path) -> None:
    """Inside the layer's footprint (the one sample sits 4 km off, within the 5 km halo) an empty 2.5 km radius
    is an absence; the summary carries the count, the footprint flag and the halo, each under an id."""
    out = T.nearby(CELL, "lake_sediment_gsc", radius_m=2500)
    base = f"c:nb:{CELL}:lake_sediment_gsc:2500"
    summary, = out.rows
    assert summary["n_within"] == 0 and summary["n_within_id"] == f"{base}:n_within"
    assert summary["in_footprint"] == 1 and summary["in_footprint_id"] == f"{base}:in_footprint"
    assert summary["footprint_halo_m"] == 5000.0 and summary["footprint_halo_m_id"] == f"{base}:footprint_halo_m"
    assert "an absence" in summary["footprint_note"] and "unknown" not in summary["footprint_note"]
    assert out.values[f"{base}:n_within"]["value"] == 0 and out.values[f"{base}:in_footprint"]["value"] == 1
    assert "nearest_m" not in summary and "in_footprint is 1" in out.note
    far = T.nearby(FAR, "radioactive_boulders", radius_m=20_000)
    assert far.rows[0]["n_within"] == 0 and far.rows[0]["in_footprint"] == 0


def test_a_polygon_over_the_cell_is_at_distance_zero_and_carries_its_unit_as_text(world: Path) -> None:
    out = T.nearby(CELL, "bedrock_250k", radius_m=500)
    assert out.rows[0]["n_within"] == 1 and out.rows[0]["nearest_m"] == 0.0
    assert out.rows[1]["dist_m"] == 0.0 and out.rows[1]["text"] == "Quartz arenite"


def test_labels_context_unknown_layers_and_bad_radii_are_refused(world: Path) -> None:
    for layer, why in (("mineral_deposits_uranium", "label"), ("uranium_deposit_footprints", "label"),
                       ("smdi_uranium", "label"), ("basin_geology", "context"), ("graphitic_host", "no evidence"),
                       ("nonsense", "no evidence")):
        with pytest.raises(T.ToolError, match=why):
            T.nearby(CELL, layer)
    for radius in (100, 499.9, 20_000.1, 50_000):
        with pytest.raises(T.ToolError, match="radius_m"):
            T.nearby(CELL, "faults_250k", radius_m=radius)
    for k in (-1, T.NEARBY_MAX_K + 1):
        with pytest.raises(T.ToolError, match="k must"):
            T.nearby(CELL, "faults_250k", k=k)


def test_an_unknown_cell_is_a_result_that_says_so(world: Path) -> None:
    for out in (T.nearby("9999_9999", "faults_250k"), T.crosscheck("9999_9999")):
        assert out.note == "no such cell" and out.rows == [] and out.values == {}


def test_drillhole_rows_are_effort_and_nothing_else_is(world: Path) -> None:
    holes = T.nearby(CELL, "compilation", radius_m=5000)
    assert holes.rows[0]["n_within"] == 2 and holes.rows[1]["text"] == "Uranium"
    assert all(r["is_effort"] is True for r in holes.rows), "the summary and every feature row"
    for layer in WORLD:
        if layer != "compilation":
            assert all(r["is_effort"] is False for r in T.nearby(CELL, layer, radius_m=5000).rows), layer


def test_rows_never_carry_a_coordinate_and_every_number_has_an_id(world: Path) -> None:
    for layer in WORLD:
        out = T.nearby(CELL, layer, radius_m=5000, k=5)
        assert not any(set(r) & PLACING_KEYS for r in out.rows), layer
        text = json.dumps(out.as_json())
        assert "500000" not in text and "6400000" not in text, layer
        assert_every_number_has_an_id(out)
    assert_every_number_has_an_id(T.crosscheck(CELL))
    assert_every_number_has_an_id(T.crosscheck(FAR))


def test_layer_features_reads_the_pulled_file_the_way_the_card_does(prospect_sandbox,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the world fixture: the real cached reader, over a pulled file in lon/lat, must reproject into
    grid metres, carry the reading and the mapped-unit text, and drop the other attributes."""
    x, y = Transformer.from_crs(4326, 2957, always_xy=True).transform(-105.0, 57.5)
    df = bench_frame(n_dep=1, n_occ=1, n_neg=1, n_probe=1)
    df.loc[0, ["cx", "cy"]] = (x, y)
    make_bench_store(prospect_sandbox.db, df)
    monkeypatch.setattr(ST, "db_path", lambda: prospect_sandbox.db)
    pulled = {
        "lake_sediment_sgs": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-105.0, 57.5]},
             "properties": {"U_PPM": 12.5}},
            {"type": "Feature", "geometry": None, "properties": {"U_PPM": 1.0}},
        ],
        "bedrock_250k": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[-105.1, 57.4], [-104.9, 57.4], [-104.9, 57.6],
                                                             [-105.1, 57.6], [-105.1, 57.4]]]},
            "properties": {"LITHOLOGY": "graphitic pelitic gneiss", "NAME": "not-copied", "OBJECTID": 7},
        }],
    }
    monkeypatch.setattr(IX, "read_features", lambda key: pulled[key])
    T.layer_features.cache_clear()
    T.layer_footprint.cache_clear()
    try:
        sed = T.nearby(CELL, "lake_sediment_sgs", radius_m=1000)
        rock = T.nearby(CELL, "bedrock_250k", radius_m=1000)
        # each layer read from the file once (the footprint's second look is a cache hit) and its footprint built once
        assert T.layer_features.cache_info().misses == 2 and T.layer_features.cache_info().hits == 2
        assert T.layer_footprint.cache_info().misses == 2
        T.nearby(CELL, "bedrock_250k", radius_m=2000)
        assert T.layer_features.cache_info().misses == 2, "a layer is read once per process"
        assert T.layer_footprint.cache_info().hits == 1 and T.layer_footprint.cache_info().misses == 2
    finally:
        T.layer_features.cache_clear()
        T.layer_footprint.cache_clear()
    assert sed.rows[0]["n_within"] == 1 and sed.rows[1]["dist_m"] < 1.0 and sed.rows[1]["ppm"] == 12.5
    assert sed.rows[0]["in_footprint"] == 1 and rock.rows[0]["in_footprint"] == 1
    assert rock.rows[1]["dist_m"] == 0.0 and rock.rows[1]["text"] == "graphitic pelitic gneiss"
    assert "not-copied" not in json.dumps(rock.as_json())


# ---------------------------------------------------------------- crosscheck


def test_a_crossing_pair_gives_one_crossing_and_zero_separation(world: Path) -> None:
    out = T.crosscheck(CELL)
    row = next(r for r in out.rows if r["pair"] == "conductor_fault")
    assert row["crossings_n"] == 1 and row["crossings_n_id"] == f"c:x:{CELL}:conductor_fault:crossings_n"
    assert row["min_sep_m"] == 0.0 and row["min_sep_m_id"] == f"c:x:{CELL}:conductor_fault:min_sep_m"
    assert row["state"] == "known" and row["radius_m"] == 5000.0 and row["radius_m_id"] in out.values
    # the echoed features are cell_features' own ids and values, so a registry merges them
    feats = T.cell_features(CELL)
    for key in ("d_conductor_m", "d_fault_m"):
        vid = f"c:cell:{CELL}:{key}"
        assert row[f"{key}_id"] == vid and row[key] == feats.values[vid]["value"]
        assert out.values[vid] == feats.values[vid]


def test_a_side_with_nothing_in_reach_and_no_footprint_makes_the_pair_unknown(world: Path) -> None:
    """The far cell is 60 km from every line: neither layer was surveyed there, so the pair is unknown, not
    absent, and each side's footprint flag is a 0 with an id of the pair's own."""
    out = T.crosscheck(FAR)
    row = next(r for r in out.rows if r["pair"] == "conductor_fault")
    assert row["crossings_n"] == 0 and row["crossings_n_id"] == f"c:x:{FAR}:conductor_fault:crossings_n"
    assert row["state"] == "unknown" and "min_sep_m" not in row and "absent" not in row
    assert row["missing"] == ("no conductor or fault was mapped around this cell at all, so the pair is unknown, "
                              "not absent")
    for side, layer in (("conductor", "em_conductors"), ("fault", "faults_250k")):
        vid = f"c:x:{FAR}:conductor_fault:{layer}:in_footprint"
        assert row[f"{side}_in_footprint"] == 0 and row[f"{side}_in_footprint_id"] == vid
        assert out.values[vid]["value"] == 0 and layer in out.values[vid]["note"]
    near = next(r for r in T.crosscheck(CELL).rows if r["pair"] == "conductor_fault")
    assert near["conductor_in_footprint"] == 1 and near["fault_in_footprint"] == 1 and near["state"] == "known"


def test_the_sediment_pair_conditions_the_anomaly_on_its_sampling(world: Path) -> None:
    out = T.crosscheck(CELL)
    row = next(r for r in out.rows if r["pair"] == "sediment_sampling")
    feats = T.cell_features(CELL)
    assert row["sed_u_max_ppm"] == 12.5 and row["sed_u_max_ppm_id"] == f"c:cell:{CELL}:sed_u_max_ppm"
    assert row["sed_samples_n"] == 2.0 and row["sed_samples_n_id"] == f"c:cell:{CELL}:sed_samples_n"
    assert out.values[f"c:cell:{CELL}:sed_u_max_ppm"] == feats.values[f"c:cell:{CELL}:sed_u_max_ppm"]
    assert row["thin_sampling"] is True and row["state"] == "known"
    assert row["min_samples"] == T.MIN_SED_SAMPLES == 3
    assert row["min_samples_id"] == f"c:x:{CELL}:sediment_sampling:min_samples"
    assert "Unknown and absent are different" in out.note


def test_an_unsampled_cell_is_unknown_and_carries_the_nearest_observation_id(world: Path) -> None:
    out = T.crosscheck(FAR)
    row = next(r for r in out.rows if r["pair"] == "sediment_sampling")
    nid = f"c:cell:{FAR}:sed_u_max_ppm:nearest_m"
    assert row["sed_u_max_ppm"] is None and "sed_u_max_ppm_id" not in row
    assert row["sed_u_max_ppm_nearest_m_id"] == nid and out.values[nid]["value"] == 4200.0
    assert row["state"] == "unknown" and "unknown, not low" in row["missing"]
    assert row["sed_samples_n"] == 5.0 and row["thin_sampling"] is False
    assert row["sediment_in_footprint"] == 0 and "none around this cell at all" in row["missing"]
    assert row["sediment_in_footprint_id"] == f"c:x:{FAR}:sediment_sampling:lake_sediment_gsc:in_footprint"


# ---------------------------------------------------------------- ids, registry, label mask


def test_every_id_carries_the_cell_in_second_position(world: Path) -> None:
    pattern = re.compile(rf"^c:(nb|x|cell|near):{CELL}:")
    results = [T.nearby(CELL, layer, radius_m=5000, k=3) for layer in WORLD]
    results += [T.crosscheck(CELL), T.label_context(CELL, mask_cell=CELL)]
    for res in results:
        assert res.values, res.tool
        for vid, v in res.values.items():
            assert pattern.match(vid) and v["id"] == vid, vid
        cited = {v for r in res.rows for k, v in r.items() if k.endswith("_id")}
        assert cited <= set(res.values), res.tool


def test_both_tools_are_registered_and_reachable_through_call(world: Path) -> None:
    assert {"nearby", "crosscheck"} <= set(T.REGISTRY) and {"nearby", "crosscheck"} <= set(T.TOOL_HELP)
    out = T.call("nearby", {"cell_id": CELL, "layer": "radioactive_boulders", "radius_m": 5000, "k": 1})
    assert out.rows[0]["n_within"] == 3 and len(out.rows) == 2
    assert T.call("crosscheck", {"cell_id": CELL}).rows[0]["pair"] == "conductor_fault"
    with pytest.raises(T.ToolError):
        T.call("nearby", {"cell_id": CELL, "layer": "radioactive_boulders", "radius": 5000})


@pytest.fixture
def labels(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_bench_store(prospect_sandbox.db)
    monkeypatch.setattr(ST, "db_path", lambda: prospect_sandbox.db)
    return prospect_sandbox.db


def test_the_mask_drops_the_cells_own_label_and_reranks_the_rest_without_a_gap(labels: Path) -> None:
    plain = T.label_context(CELL)
    masked = T.label_context(CELL, mask_cell=CELL)
    assert plain.rows[0]["distance_km"] == 0.0 and "mask_cell" not in plain.args, "a deposit cell sees itself first"
    assert all(r["distance_km"] > 0 for r in masked.rows) and masked.args["mask_cell"] == CELL
    key = lambda r: (r["tier"], r["name"], r["distance_km"])  # noqa: E731
    assert [key(r) for r in masked.rows[:4]] == [key(r) for r in plain.rows[1:]], "the others are the same rows"
    assert [r["rank"] for r in masked.rows] == [1, 2, 3, 4, 5], "ranked from 1: a gap would say the cell is labelled"
    assert [r["distance_km_id"] for r in masked.rows] == [f"c:near:{CELL}:{i}" for i in range(5)]
    assert masked.note.startswith(plain.note) and masked.note.endswith("The evaluated cell's own label is masked.")


def test_the_mask_changes_nothing_but_the_note_for_a_cell_without_a_label(labels: Path) -> None:
    probe = "0002_0099"   # the last cell of the frame, never labelled
    plain, masked = T.label_context(probe), T.label_context(probe, mask_cell=probe)
    assert plain.rows == masked.rows and plain.values == masked.values
    assert plain.note != masked.note and "masked" in masked.note

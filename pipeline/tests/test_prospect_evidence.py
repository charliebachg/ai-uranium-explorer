"""The raw evidence tools: every record in reach, nearest first, every number bound to a value id, the ratios and
bearings computed by the tool, and nothing that names the ground."""

from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

from uranium_explorer.prospect import evidence as E
from uranium_explorer.prospect import features as F

EPSG = 2957
CELL = "0001_0001"
CENTRE = (1000.0, 1000.0)


def gdf(rows: list[dict], geoms: list) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=f"EPSG:{EPSG}")


@pytest.fixture
def world(monkeypatch):
    """Synthetic layers round one cell whose centre is (1000, 1000) and whose ice moved south."""
    layers: dict[str, gpd.GeoDataFrame] = {
        "lake_sediment_gsc": gdf(
            [{"U": 40.0, "U_INA": 30.0, "TH_INA": 6.0, "LOI": 20.0, "LK_DEPTH": 4.0},
             {"U": 2.0, "U_INA": "*", "TH_INA": "*", "LOI": 2.0, "LK_DEPTH": 7.0},
             {"U": -0.5, "U_INA": 1.0, "TH_INA": 0.2, "LOI": 30.0, "LK_DEPTH": 3.0},
             {"U": 1.0, "U_INA": 1.0, "TH_INA": 5.0, "LOI": 30.0, "LK_DEPTH": 3.0}],
            [Point(1000, -2000), Point(1000, 5000), Point(4000, 1000), Point(40_000, 40_000)]),
        "lake_sediment_sgs": gdf([{"U_PPM": 5.0, "PB_PPM": -2.0, "NI_PPM": 30.0, "LOI_PERC": 50.0}],
                                 [Point(1500, 1500)]),
        "lake_water_sgs": gdf([{"U_PPM": 0.0, "PH": 6.5, "EH_MV": 120.0}], [Point(900, 900)]),
        "radioactive_boulders": gdf(
            [{"CPS": 9000.0, "LITHOLOGY": "SANDSTONE", "BACKGROUND": 0.0, "YEAR": 2012, "U308_ASSAY_RESULTS": "6.6%"},
             {"CPS": 0.0, "LITHOLOGY": "granite", "BACKGROUND": 0.0},
             {"CPS": 500.0, "LITHOLOGY": "Pink granite", "BACKGROUND": 90.0},
             {"CPS": 800.0, "LITHOLOGY": "ATHABASCA SANDSTONE", "BACKGROUND": 0.0}],
            [Point(1000, -4000), Point(1000, -3000), Point(1000, 7000), Point(5000, 1000)]),
        "faults_250k": gdf([{"FEAT_TYPE": "Lineament", "FEAT_NAME": "Named Fault"}] * 2 + [{"FEAT_TYPE": "Lineament"}],
                           [LineString([(-3000, -3000), (5000, 5000)]), LineString([(-5000, 2000), (7000, 2000)]),
                            LineString([(50_000, 0), (50_000, 100)])]),
        "em_conductors": gdf([{"CONDUCTOR_TYPE": "VTEM", "FILE_NUMBER": "74H09-0039"}],
                             [LineString([(0, -5000), (0, 6000)])]),
        "bedrock_250k": gdf(
            [{"LITHOLOGY": "Quartz arenite", "DOMAIN": "Athabasca Basin", "FORMATION": "Manitou Falls"},
             {"LITHOLOGY": "pelitic gneiss", "DOMAIN": "Wollaston", "FORMATION": " "},
             {"LITHOLOGY": "granite", "DOMAIN": "Mudjatik", "FORMATION": " "}],
            [box(-10_000, -10_000, 3000, 10_000), box(3000, -10_000, 30_000, 10_000),
             box(30_000, -10_000, 60_000, 10_000)]),
        "surficial_250k": gdf([{"MAIN_ENVIRONMENT": "Morainal"}], [box(-10_000, -10_000, 10_000, 10_000)]),
    }
    monkeypatch.setattr(F, "_layer", lambda key, epsg=EPSG: layers[key])
    monkeypatch.setattr(E, "_cell_centre", lambda cell_id: CENTRE if cell_id == CELL else None)
    monkeypatch.setattr(E, "down_ice", lambda cell_id: (180.0, True))
    monkeypatch.setattr(F, "_basin_outline", lambda: box(-10_000, -10_000, 3000, 10_000))
    E.clear_caches()
    yield layers
    E.clear_caches()


def numbers_have_ids(result) -> list[str]:
    """Every numeric field in every row must sit beside an id that resolves to the same value."""
    bad = []
    for row in result.rows:
        for k, v in row.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vid = row.get(f"{k}_id")
                if vid is None or vid not in result.values or result.values[vid]["value"] != v:
                    bad.append(f"{row.get('kind')}.{k}")
    return bad


def test_geochemistry_lists_samples_nearest_first_with_the_ratios_computed(world):
    out = E.evidence_geochem(CELL)
    assert not numbers_have_ids(out)
    sed = [r for r in out.rows if r["kind"] == "lake sediment"]
    assert [r["survey"] for r in sed] == ["regional survey", "regional survey", "regional survey", "1975-1978 survey"]
    assert [r["dist_m"] for r in sed[:3]] == [3000.0, 3000.0, 4000.0], "nearest first; the far sample is out of reach"
    south, east, north = sed[:3]
    assert south["bearing"] == "S" and south["ice"] == "down-ice"
    assert south["u_th"] == 5.0 and south["u_per_loi"] == 2.0, "30/6 by activation and 40 ppm over 20% LOI"
    assert south["regional_rank"] == "in the top 5% of the survey"
    assert north["bearing"] == "N" and north["ice"] == "up-ice"
    assert "u_th" not in north and "u_per_loi" not in north, "no thorium reading; 2% LOI is under the floor"
    assert east["u_ppm"] == E.BELOW_DETECTION and east["th_ppm"] == 0.2
    assert "u_th" not in east, "a thorium of 0.2 ppm is under the floor: a division, not a measurement"
    assert east["ice"] == "across the ice flow"
    sgs = sed[3]
    assert sgs["pb_ppm"] == E.BELOW_DETECTION and sgs["ni_ppm"] == 30.0 and sgs["u_per_loi"] == 0.1
    water = next(r for r in out.rows if r["kind"] == "lake water")
    assert water["ph"] == 6.5 and water["eh_mv"] == 120.0


def test_boulders_start_with_the_ice_flow_and_leave_blanks_assays_and_years_out(world):
    out = E.evidence_boulders(CELL)
    assert not numbers_have_ids(out)
    ice, *boulders = out.rows
    assert ice["kind"] == "ice flow" and ice["down_ice"] == "S" and ice["down_ice_deg"] == 180.0
    assert [b["cps"] for b in boulders] == [800.0, 9000.0, 500.0], "the 0 cps blank is gone; nearest first"
    by_cps = {b["cps"]: b for b in boulders}
    assert by_cps[9000.0]["ice"] == "down-ice" and by_cps[9000.0]["rock_group"] == "cover (sandstone family)"
    assert by_cps[500.0]["ice"] == "up-ice" and by_cps[500.0]["rock_group"] == "basement"
    assert by_cps[500.0]["background_cps"] == 90.0 and "background_cps" not in by_cps[9000.0]
    text = json.dumps(out.as_json())
    assert "6.6%" not in text and "2012" not in text, "no assay and no year reaches the reader"


def test_structure_gives_trends_lengths_and_crossings(world):
    out = E.evidence_structure(CELL)
    assert not numbers_have_ids(out)
    lines = [r for r in out.rows if r["kind"] == "lineament"]
    assert [r["trend"] for r in lines] == ["NE-SW", "E-W"], "the far lineament is out of reach"
    assert lines[0]["dist_m"] == 0.0 and lines[1]["dist_m"] == 1000.0
    cond = next(r for r in out.rows if r["kind"] == "conductor")
    assert cond["trend"] == "N-S" and cond["length_in_reach_km"] == 11.0
    crossings = {r["what"]: r for r in out.rows if r["kind"] == "crossings"}
    assert crossings["lineament crossing another lineament"]["within_5km"] == 1.0
    assert crossings["lineament crossing a conductor"]["within_5km"] == 2.0
    text = json.dumps(out.as_json())
    assert "Named Fault" not in text and "VTEM" not in text and "74H09" not in text


def test_bedrock_describes_units_by_share_and_says_what_is_under_the_cell(world):
    out = E.evidence_bedrock(CELL)
    assert not numbers_have_ids(out)
    units = [r for r in out.rows if r["kind"] == "bedrock unit"]
    assert [u["description"] for u in units] == ["quartz arenite", "pelitic gneiss"]
    assert units[0]["under_cell_centre"] == "yes" and units[0]["group"] == "sandstone"
    assert units[1]["group"] == "graphitic_pelite" and units[1]["dist_m"] == 2000.0
    assert sum(u["share_of_area"] for u in units) == pytest.approx(1.0, abs=0.01)
    assert "Manitou" not in json.dumps(out.as_json())


def test_the_region_names_domains_by_letter_and_describes_their_rocks(world):
    out = E.region(CELL)
    assert not numbers_have_ids(out)
    cover = out.rows[0]
    assert cover["under_sandstone_cover"] == "yes" and cover["dist_to_cover_edge_m"] == 2000.0
    doms = [r for r in out.rows if r["kind"] == "basement domain"]
    assert [d["domain"] for d in doms] == ["A", "B"] and doms[0]["dist_m"] == 2000.0
    assert doms[0]["rock_1"] == "graphitic_pelite" and doms[0]["rock_1_share"] == 1.0
    boundary = next(r for r in out.rows if r["kind"] == "domain boundary")
    assert boundary["dist_m"] == pytest.approx(28_950.0)
    text = json.dumps(out.as_json())
    for name in ("Wollaston", "Mudjatik", "Athabasca"):
        assert name not in text


def test_an_unknown_cell_and_an_empty_neighbourhood_say_so(world, monkeypatch):
    assert E.evidence_geochem("9999_9999").note == "no such cell"
    monkeypatch.setattr(E, "_cell_centre", lambda cell_id: (500_000.0, 500_000.0))
    out = E.evidence_boulders(CELL)
    assert out.rows[-1]["kind"] == "none" and "unknown, not absent" in out.rows[-1]["note"]
    assert E.evidence_geochem(CELL).rows[-1]["kind"] == "none"


def test_compass_trend_and_ice_relation():
    assert [E.compass(d) for d in (0, 44, 46, 180, 225, 359)] == ["N", "NE", "NE", "S", "SW", "N"]
    assert [E.trend_name(a) for a in (0, 30, 90, 135, 170)] == ["N-S", "NE-SW", "E-W", "NW-SE", "N-S"]
    assert E.ice_relation(200, 180) == "down-ice" and E.ice_relation(10, 180) == "up-ice"
    assert E.ice_relation(90, 180) == "across the ice flow"
    assert np.isclose(E._line_axis(LineString([(0, 0), (1, 1)])), 45.0)

"""Feature maths, and the two rules that matter: labels never become features, and a gap is never a zero."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from uranium_explorer.prospect import features as F
from uranium_explorer.prospect.inventory import load as load_inventory

EPSG = 2957


def cells(n: int = 3, size: int = 2000) -> gpd.GeoDataFrame:
    """A row of square cells starting at the origin, in the grid's metric CRS."""
    geoms = [box(i * size, 0, (i + 1) * size, size) for i in range(n)]
    return gpd.GeoDataFrame(
        {"cell_id": [f"{i:04d}_0000" for i in range(n)]}, geometry=geoms, crs=f"EPSG:{EPSG}"
    )


@pytest.fixture
def patch_layer(monkeypatch):
    """Serve synthetic layers in place of the pulled GeoJSON."""
    store: dict[str, gpd.GeoDataFrame] = {}

    def fake(key: str, epsg: int = EPSG) -> gpd.GeoDataFrame:
        return store[key]

    monkeypatch.setattr(F, "_layer", fake)
    return store


# ---------------------------------------------------------------- leakage


def test_no_feature_reads_a_label_layer():
    """Deposit footprints and occurrences are the positive class; reading one would be leakage."""
    labels = F.label_keys(load_inventory())
    assert labels, "the inventory declares no labels"
    for spec in F.SPECS:
        assert not (set(spec.source_keys) & labels), f"{spec.key} reads label layer(s)"


def test_build_refuses_a_spec_that_reads_a_label(monkeypatch):
    bad = F.FeatureSpec("leaky", "Leaky", "pathway", "distance_to_lines", ("uranium_deposit_footprints",))
    monkeypatch.setattr(F, "SPECS", (bad,))
    with pytest.raises(RuntimeError) as e:
        F.build(log=lambda *_: None)
    assert "labels are never features" in str(e.value)


def test_effort_features_are_declared_as_such():
    """The null model trains on these alone, so the marking has to be right."""
    effort = {s.key for s in F.SPECS if s.is_effort}
    assert {"holes_n", "holes_first_year", "airborne_surveys_n", "ground_surveys_n"} <= effort
    for key in ("d_conductor_m", "graphitic_host", "sed_u_max_ppm", "unconformity_depth_m"):
        assert not next(s for s in F.SPECS if s.key == key).is_effort


# ---------------------------------------------------------------- distance and density


def test_distance_to_a_line_is_measured_in_metres(patch_layer):
    c = cells(2)
    # a line 3 km north of the cell row, running east-west
    patch_layer["lines"] = gpd.GeoDataFrame(
        geometry=[LineString([(-10_000, 4000), (10_000, 4000)])], crs=f"EPSG:{EPSG}"
    )
    out = F.distance_to_lines(c, "lines")
    # cell centres sit at y = 1000, so the line is 3000 m away
    assert np.allclose(out["value"].to_numpy(), 3000.0)


def test_line_density_counts_kilometres_per_square_kilometre(patch_layer):
    c = cells(1)  # one 2 km cell: 4 km2
    patch_layer["lines"] = gpd.GeoDataFrame(
        geometry=[LineString([(0, 1000), (2000, 1000)])], crs=f"EPSG:{EPSG}"
    )
    out = F.line_density(c, "lines")
    assert out["value"].iloc[0] == pytest.approx(2.0 / 4.0)  # 2 km of line in 4 km2
    assert out["n_obs"].iloc[0] == 1


def test_a_cell_with_no_line_nearby_still_gets_a_distance(patch_layer):
    c = cells(1)
    patch_layer["lines"] = gpd.GeoDataFrame(
        geometry=[LineString([(500_000, 500_000), (500_100, 500_100)])], crs=f"EPSG:{EPSG}"
    )
    out = F.distance_to_lines(c, "lines")
    assert out["value"].iloc[0] > 100_000  # far, but a real measurement rather than a gap


# ---------------------------------------------------------------- points: a gap is not a zero


def test_a_cell_with_no_sample_gets_null_not_zero(patch_layer):
    c = cells(2)
    # one sample beside the first cell only
    patch_layer["pts"] = gpd.GeoDataFrame(
        {"U": [40.0]}, geometry=[Point(1000, 1000)], crs=f"EPSG:{EPSG}"
    )
    out = F.point_stat(c, "pts", "U", radius_m=1500, stat="max")
    assert out["value"].iloc[0] == 40.0 and out["n_obs"].iloc[0] == 1
    assert np.isnan(out["value"].iloc[1]), "a cell with no sample must not read as a low value"
    assert out["n_obs"].iloc[1] == 0
    assert out["nearest_m"].iloc[1] > 1500  # how far the nearest observation is, so the gap is measurable


def test_point_stat_max_and_count(patch_layer):
    c = cells(1)
    patch_layer["pts"] = gpd.GeoDataFrame(
        {"U": [10.0, 30.0, 20.0]},
        geometry=[Point(900, 1000), Point(1100, 1000), Point(1000, 1200)],
        crs=f"EPSG:{EPSG}",
    )
    assert F.point_stat(c, "pts", "U", 5000, "max")["value"].iloc[0] == 30.0
    assert F.point_stat(c, "pts", "U", 5000, "count")["value"].iloc[0] == 3.0


def test_point_year_reads_the_provincial_date_text(patch_layer):
    c = cells(1)
    patch_layer["holes"] = gpd.GeoDataFrame(
        {"DATE_DRILLED": ["1978-06-01  -->  1978-06-02", "1954-06-01  -->  1954-06-01", None]},
        geometry=[Point(1000, 1000), Point(1200, 1000), Point(1400, 1000)],
        crs=f"EPSG:{EPSG}",
    )
    out = F.point_year(c, "holes", "DATE_DRILLED", radius_m=5000, stat="min")
    assert out["value"].iloc[0] == 1954.0
    assert out["n_obs"].iloc[0] == 2  # the undated hole is not counted


# ---------------------------------------------------------------- interpolation


def test_idw_returns_the_observation_at_an_exact_hit(patch_layer):
    c = cells(1)
    patch_layer["obs"] = gpd.GeoDataFrame(
        {"d": [200.0, 400.0]}, geometry=[Point(1000, 1000), Point(9000, 1000)], crs=f"EPSG:{EPSG}"
    )
    out = F.idw(c, "obs", "d", k=2, max_m=25_000)
    assert out["value"].iloc[0] == pytest.approx(200.0), "a cell centred on a hole reports that hole"


def test_idw_weights_two_equidistant_observations_equally(patch_layer):
    c = cells(1)  # centre at (1000, 1000)
    patch_layer["obs"] = gpd.GeoDataFrame(
        {"d": [100.0, 300.0]}, geometry=[Point(0, 1000), Point(2000, 1000)], crs=f"EPSG:{EPSG}"
    )
    out = F.idw(c, "obs", "d", k=2, max_m=25_000)
    assert out["value"].iloc[0] == pytest.approx(200.0)
    assert out["n_obs"].iloc[0] == 2


def test_idw_refuses_to_extrapolate_beyond_its_range(patch_layer):
    c = cells(1)
    patch_layer["obs"] = gpd.GeoDataFrame(
        {"d": [200.0]}, geometry=[Point(400_000, 400_000)], crs=f"EPSG:{EPSG}"
    )
    out = F.idw(c, "obs", "d", k=1, max_m=25_000)
    assert np.isnan(out["value"].iloc[0]), "beyond the cap the answer is not known, not a guess"
    assert out["n_obs"].iloc[0] == 0


# ---------------------------------------------------------------- polygons


def test_polygon_class_takes_the_largest_overlap_and_leaves_unmapped_ground_null(patch_layer):
    c = cells(2)
    patch_layer["geo"] = gpd.GeoDataFrame(
        {"LITHOLOGY": ["graphitic pelitic gneiss", "sandstone"]},
        geometry=[Polygon([(0, 0), (1500, 0), (1500, 2000), (0, 2000)]),
                  Polygon([(1500, 0), (2000, 0), (2000, 2000), (1500, 2000)])],
        crs=f"EPSG:{EPSG}",
    )
    out = F.polygon_class(c, "geo", "LITHOLOGY")
    assert out["value_text"].iloc[0] == "graphitic pelitic gneiss"
    # the second cell only abuts the sandstone polygon, so nothing covers it: missing, stored as null
    assert pd.isna(out["value_text"].iloc[1]) and out["n_obs"].iloc[1] == 0


def test_graphitic_host_separates_unmapped_from_mapped_and_absent(patch_layer):
    c = cells(3)
    patch_layer["bedrock_250k"] = gpd.GeoDataFrame(
        {"LITHOLOGY": ["graphitic metapelite", "quartzite"]},
        geometry=[box(0, 0, 2000, 2000), box(2000, 0, 4000, 2000)],
        crs=f"EPSG:{EPSG}",
    )
    out = F.graphitic_host(c)
    assert out["value"].iloc[0] == 1.0     # a mapped graphitic host
    assert out["value"].iloc[1] == 0.0     # mapped, and not one
    assert np.isnan(out["value"].iloc[2])  # unmapped: not the same as absent


def test_graphitic_host_is_unknown_under_basin_cover_unless_a_host_is_mapped_there(patch_layer):
    """Inside the basin outline the 1:250k map shows the sandstone cover; a non-host polygon there says
    nothing about the basement (F7's verifier objection), while a host mapped inside still counts."""
    c = cells(4)
    c["in_basin"] = [True, True, False, True]
    patch_layer["bedrock_250k"] = gpd.GeoDataFrame(
        {"LITHOLOGY": ["conglomeratic quartz arenite", "graphitic metapelite", "quartz arenite"]},
        geometry=[box(0, 0, 2000, 2000), box(2000, 0, 4000, 2000), box(4000, 0, 6000, 2000)],
        crs=f"EPSG:{EPSG}",
    )
    out = F.graphitic_host(c)
    assert np.isnan(out["value"].iloc[0]) and out["n_obs"].iloc[0] == 1, "covered and not a host: unknown, still mapped"
    assert out["value"].iloc[1] == 1.0, "a host mapped inside the outline counts"
    assert out["value"].iloc[2] == 0.0, "outside the basin a non-host polygon is absent"
    assert np.isnan(out["value"].iloc[3]), "unmapped stays unknown"


def test_point_stat_can_read_an_exact_zero_as_a_missing_reading(patch_layer):
    """2,127 of the 6,591 boulder records carry 0.0 cps: a filled-in blank, which once read as a measured
    absence. With `zero_is_null` the zero is left out of the value and the count of readings."""
    c = cells(1)
    patch_layer["pts"] = gpd.GeoDataFrame(
        {"CPS": [0.0, 0.0, 650.0]},
        geometry=[Point(900, 1000), Point(1100, 1000), Point(1000, 1200)],
        crs=f"EPSG:{EPSG}",
    )
    plain = F.point_stat(c, "pts", "CPS", 5000, "max")
    assert plain["value"].iloc[0] == 650.0 and plain["n_obs"].iloc[0] == 3
    strict = F.point_stat(c, "pts", "CPS", 5000, "max", zero_is_null=True)
    assert strict["value"].iloc[0] == 650.0 and strict["n_obs"].iloc[0] == 1
    patch_layer["pts"] = gpd.GeoDataFrame({"CPS": [0.0]}, geometry=[Point(1000, 1000)], crs=f"EPSG:{EPSG}")
    only_zero = F.point_stat(c, "pts", "CPS", 5000, "max", zero_is_null=True)
    assert np.isnan(only_zero["value"].iloc[0]) and only_zero["n_obs"].iloc[0] == 0, "a zero alone is no reading"


def test_polygon_overlap_count_measures_survey_effort(patch_layer):
    c = cells(2)
    patch_layer["surveys"] = gpd.GeoDataFrame(
        geometry=[box(0, 0, 2000, 2000), box(0, 0, 1000, 1000)], crs=f"EPSG:{EPSG}"
    )
    out = F.polygon_overlap_count(c, "surveys")
    assert out["value"].iloc[0] == 2.0
    assert out["value"].iloc[1] == 0.0


def test_graphitic_host_surface_keeps_the_covered_cells_complete_for_the_learned_model(patch_layer):
    c = cells(3)
    c["in_basin"] = [True, True, False]
    patch_layer["bedrock_250k"] = gpd.GeoDataFrame(
        {"LITHOLOGY": ["conglomeratic quartz arenite", "graphitic metapelite"]},
        geometry=[box(0, 0, 2000, 2000), box(2000, 0, 4000, 2000)], crs=f"EPSG:{EPSG}",
    )
    surface = F.graphitic_host_surface(c)
    assert surface["value"].iloc[0] == 0.0 and surface["value"].iloc[1] == 1.0 and np.isnan(surface["value"].iloc[2])
    host = F.graphitic_host(c)
    assert np.isnan(host["value"].iloc[0]), "the cover-aware feature says unknown where the surface one says 0"


# ---------------------------------------------------------------- the extended evidence


def test_a_negative_result_is_below_detection_and_reads_as_zero(patch_layer):
    """The 1975-1978 survey writes a result under its detection limit as a negative number."""
    c = cells(1)
    patch_layer["pts"] = gpd.GeoDataFrame({"PB_PPM": [-2.0, 5.0]}, geometry=[Point(900, 1000), Point(1100, 1000)],
                                          crs=f"EPSG:{EPSG}")
    assert F.point_stat(c, "pts", "PB_PPM", 5000, "max", negative_is_zero=True)["value"].iloc[0] == 5.0
    patch_layer["pts"] = gpd.GeoDataFrame({"PB_PPM": [-26.0]}, geometry=[Point(1000, 1000)], crs=f"EPSG:{EPSG}")
    out = F.point_stat(c, "pts", "PB_PPM", 5000, "max", negative_is_zero=True)
    assert out["value"].iloc[0] == 0.0 and out["n_obs"].iloc[0] == 1, "below detection is a low reading, not a gap"


def test_a_text_filter_takes_the_statistic_over_one_kind_of_record(patch_layer):
    c = cells(1)
    patch_layer["boulders"] = gpd.GeoDataFrame(
        {"CPS": [700.0, 5000.0, 900.0], "LITHOLOGY": ["SANDSTONE", "Granite", "conglomeratic sandstone"]},
        geometry=[Point(900, 1000), Point(1100, 1000), Point(1000, 1200)], crs=f"EPSG:{EPSG}")
    out = F.point_stat(c, "boulders", "CPS", 5000, "max", text_field="LITHOLOGY", text_words=F.SANDSTONE_WORDS)
    assert out["value"].iloc[0] == 900.0 and out["n_obs"].iloc[0] == 2, "the granite boulder is not sandstone"


def test_a_ratio_is_taken_sample_by_sample_and_skips_a_near_zero_denominator(patch_layer):
    c = cells(1)
    patch_layer["sed"] = gpd.GeoDataFrame(
        {"U_INA": [10.0, 4.0, 50.0, -0.2], "TH_INA": [5.0, 1.0, 0.1, 3.0]},
        geometry=[Point(900 + 50 * i, 1000) for i in range(4)], crs=f"EPSG:{EPSG}")
    out = F.point_ratio(c, "sed", "U_INA", "TH_INA", 5000, den_floor=0.5)
    # 10/5 = 2 and 4/1 = 4 count; 50/0.1 is a division, not a measurement; -0.2 is below detection, so 0/3
    assert out["value"].iloc[0] == 4.0 and out["n_obs"].iloc[0] == 3


def test_a_share_above_the_layer_threshold_is_null_under_three_samples(patch_layer):
    c = cells(2, size=20_000)
    near = [Point(10_000 + 10 * i, 10_000) for i in range(4)]
    far = [Point(30_000, 10_000 + 10 * i) for i in range(2)]
    patch_layer["sed"] = gpd.GeoDataFrame({"U": [1.0, 1.0, 1.0, 50.0, 1.0, 60.0]}, geometry=near + far,
                                          crs=f"EPSG:{EPSG}")
    out = F.point_share_above(c, "sed", "U", 1000, quantile=0.6)
    assert out["value"].iloc[0] == 0.25, "one of four samples is above the line"
    assert np.isnan(out["value"].iloc[1]) and out["n_obs"].iloc[1] == 2, "two samples are not a share"


def test_the_landform_grain_becomes_a_compass_axis_and_a_down_ice_end():
    # the DEM measures from east, clockwise: a grain of 130.8 is the compass axis 40.8-220.8
    assert np.isclose(F.grain_bearing(130.83), 220.83 - 180.0)
    assert np.isclose(F.grain_bearing(0.0), 90.0), "an east-west grain is the compass axis 90"
    down = F.down_ice_bearing(np.array([40.83, 151.0, 90.0, 10.0]))
    # the end nearer the regional south-westward flow is down-ice
    assert np.allclose(down, [220.83, 151.0, 270.0, 190.0])


def test_a_cone_reads_boulders_down_ice_of_the_cell_and_leaves_up_ice_ones_to_the_mirror(patch_layer):
    c = cells(1)
    c["down_ice_deg"] = [180.0]   # ice moved south
    patch_layer["boulders"] = gpd.GeoDataFrame(
        {"CPS": [900.0, 5000.0, 99_999.0, 0.0]},
        geometry=[Point(1000, -4000), Point(1000, 6000), Point(1100, 1000), Point(900, -3000)],
        crs=f"EPSG:{EPSG}")
    down = F.point_cone(c, "boulders", "CPS", 10_000, sense="down")
    up = F.point_cone(c, "boulders", "CPS", 10_000, sense="up")
    assert down["value"].iloc[0] == 900.0 and down["n_obs"].iloc[0] == 1, "the zero is a blank, the cell's own is out"
    assert up["value"].iloc[0] == 5000.0
    c["down_ice_deg"] = [90.0]    # ice moved east: neither boulder is in either cone
    assert np.isnan(F.point_cone(c, "boulders", "CPS", 10_000)["value"].iloc[0])


def test_lineament_length_is_split_by_trend(patch_layer):
    c = cells(1)
    patch_layer["faults"] = gpd.GeoDataFrame(
        geometry=[LineString([(1000, 0), (1000, 2000)]), LineString([(-500, 1500), (2500, 1500)]),
                  LineString([(0, 0), (1000, 1000)])], crs=f"EPSG:{EPSG}")
    get = {t: F.line_length_by_trend(c, "faults", 5000, t)["value"].iloc[0] for t in F.TRENDS}
    assert np.isclose(get["ns"], 2.0) and np.isclose(get["ew"], 3.0) and np.isclose(get["ne"], np.sqrt(2))
    assert get["nw"] == 0.0


def test_crossings_count_where_lines_cross_not_where_one_lineament_is_drawn_in_two(patch_layer):
    c = cells(1)
    patch_layer["faults"] = gpd.GeoDataFrame(
        geometry=[LineString([(0, 1000), (2000, 1000)]), LineString([(1000, 0), (1000, 2000)]),
                  LineString([(2000, 1000), (3000, 1500)])], crs=f"EPSG:{EPSG}")
    patch_layer["conductors"] = gpd.GeoDataFrame(
        geometry=[LineString([(500, 0), (500, 2000)]), LineString([(5000, 0), (5000, 100)])], crs=f"EPSG:{EPSG}")
    faults = F.line_crossings(c, "faults", 5000)
    assert faults["value"].iloc[0] == 1.0, "the third line only continues the first from its end"
    both = F.line_crossings(c, "faults", 5000, other_key="conductors")
    assert both["value"].iloc[0] == 1.0, "one conductor crosses the east-west fault; the other crosses nothing"


def test_basin_edge_distance_is_positive_inside_and_negative_outside(monkeypatch):
    c = cells(3)
    monkeypatch.setattr(F, "_basin_outline", lambda: box(-10_000, -10_000, 3000, 10_000))
    out = F.basin_edge_distance(c)["value"].to_numpy()
    assert np.allclose(out, [2000.0, 0.0, -2000.0])


def test_a_domain_is_one_hot_and_unmapped_ground_is_null(patch_layer):
    c = cells(3)
    patch_layer["bedrock_250k"] = gpd.GeoDataFrame(
        {"DOMAIN": ["Wollaston", "Mudjatik"]}, geometry=[box(0, 0, 2000, 2000), box(2000, 0, 4000, 2000)],
        crs=f"EPSG:{EPSG}")
    out = F.polygon_is(c, "bedrock_250k", "DOMAIN", "Wollaston")["value"].to_numpy()
    assert out[0] == 1.0 and out[1] == 0.0 and np.isnan(out[2])


def test_domain_boundaries_are_between_basement_domains_and_not_at_the_cover_edge(patch_layer):
    c = cells(3)
    patch_layer["bedrock_250k"] = gpd.GeoDataFrame(
        {"DOMAIN": ["Wollaston", "Mudjatik", "Athabasca Basin"]},
        geometry=[box(-8000, 0, 2000, 2000), box(2000, 0, 4000, 2000), box(4000, 0, 20_000, 2000)],
        crs=f"EPSG:{EPSG}")
    out = F.distance_to_domain_boundary(c, "bedrock_250k", "DOMAIN")["value"].to_numpy()
    # the boundary is at x = 2000 (grown 50 m each side); the sandstone's edge at x = 4000 is not one
    assert np.allclose(out, [950.0, 950.0, 2950.0])


def test_lithology_groups_and_their_count_within_reach(patch_layer):
    assert F.lith_group("pelitic gneiss") == "graphitic_pelite"
    assert F.lith_group("Conglomeratic quartz arenite") == "sandstone"
    assert F.lith_group("granodiorite") == "granitoid"
    assert F.lith_group("") is None and F.lith_group("unknown rock") == "other"
    c = cells(2, size=20_000)
    patch_layer["bedrock_250k"] = gpd.GeoDataFrame(
        {"LITHOLOGY": ["granite", "pelitic gneiss", "granodiorite"]},
        geometry=[box(0, 0, 10_000, 20_000), box(10_000, 0, 12_000, 20_000), box(12_000, 0, 20_000, 20_000)],
        crs=f"EPSG:{EPSG}")
    out = F.lith_group_count(c, "bedrock_250k", "LITHOLOGY", 5000)
    assert out["value"].iloc[0] == 2.0 and out["n_obs"].iloc[0] == 3, "granite and granodiorite are one group"
    assert np.isnan(out["value"].iloc[1]), "no polygon within reach of the second cell"


def test_the_extended_evidence_is_geology_and_every_family_key_is_built():
    from uranium_explorer.prospect.extended import EXTENDED_FEATURES

    specs = {s.key: s for s in F.SPECS}
    for key in EXTENDED_FEATURES:
        assert key in specs, f"{key} is in a family but no spec builds it"
        assert not specs[key].is_effort, f"{key} is geology; the effort null must not read it"
        assert specs[key].op in F.BUILDERS

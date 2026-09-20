"""Feature maths, and the two rules that matter: labels never become features, and a gap is never a zero."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from legacy_reader.prospect import features as F
from legacy_reader.prospect.inventory import load as load_inventory

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

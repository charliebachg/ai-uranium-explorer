"""Raster statistics: pixels land in the right cell, cloud is not mistaken for ground, and a cell nobody
imaged holds null rather than zero."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from affine import Affine
from rasterio.crs import CRS

from legacy_reader.prospect import rasters as R

EPSG = 2957


def grid(n_cols: int = 3, n_rows: int = 2, cell_m: int = 2000, x0: float = 0.0, y0: float = 0.0) -> R.Grid:
    """A small grid whose cells are all present, built the way `Grid.from_cells` would."""
    cells = pd.DataFrame([
        {"cell_id": f"{c:04d}_{r:04d}", "col": c, "row": r,
         "cx": x0 + (c + 0.5) * cell_m, "cy": y0 + (r + 0.5) * cell_m, "cell_m": cell_m}
        for c in range(n_cols) for r in range(n_rows)
    ])
    return R.Grid.from_cells(cells)


def test_grid_origin_is_recovered_from_a_cell_centre():
    g = grid()
    assert (g.x0, g.y0) == (0.0, 0.0)
    assert (g.n_cols, g.n_rows) == (3, 2)


def test_a_coordinate_lands_in_the_cell_that_contains_it():
    g = grid()
    x = np.array([100.0, 2100.0, 5900.0, -50.0, 99_999.0])
    y = np.array([100.0, 100.0, 3900.0, 100.0, 100.0])
    pos = g.cell_of(x, y)
    assert g.cell_ids[pos[0]] == "0000_0000"
    assert g.cell_ids[pos[1]] == "0001_0000"
    assert g.cell_ids[pos[2]] == "0002_0001"
    assert pos[3] == -1 and pos[4] == -1, "coordinates outside the grid belong to no cell"


def test_pixels_are_assigned_by_their_centres():
    g = grid(n_cols=2, n_rows=1)
    # 4 x 2 pixels of 1000 m, covering the two cells, origin at the grid's top-left corner
    arr = np.arange(8).reshape(2, 4)
    transform = Affine(1000.0, 0.0, 0.0, 0.0, -1000.0, 2000.0)
    pos = R._pixel_cells(arr, transform, CRS.from_epsg(EPSG), g)
    assert [g.cell_ids[p] for p in pos] == ["0000_0000"] * 2 + ["0001_0000"] * 2 + ["0000_0000"] * 2 + [
        "0001_0000"
    ] * 2


def test_rfc3339_rejects_nothing_but_fixes_a_bare_date():
    assert R._rfc3339("2024-07-01") == "2024-07-01T00:00:00Z"
    assert R._rfc3339("2024-07-01T12:00:00Z") == "2024-07-01T12:00:00Z"


def test_pick_scenes_takes_the_least_cloudy_scene_per_tile(monkeypatch):
    feats = [
        {"id": "a", "properties": {"grid:code": "MGRS-13VCD", "eo:cloud_cover": 9.0}},
        {"id": "b", "properties": {"grid:code": "MGRS-13VCD", "eo:cloud_cover": 0.4}},
        {"id": "c", "properties": {"grid:code": "MGRS-13VCE", "eo:cloud_cover": 3.0}},
    ]
    monkeypatch.setattr(R, "_search", lambda *a, **k: feats)
    picked = R.pick_scenes((-110, 57, -103, 60), "2024-06-01", "2024-09-01")
    assert [f["id"] for f in picked] == ["b", "c"]


# ---------------------------------------------------------------- Sentinel-2 fractions


def _scene(stac_id: str = "S2_TEST") -> dict:
    return {
        "id": stac_id,
        "assets": {"scl": {"href": f"https://example.invalid/{stac_id}/SCL.tif"}},
        "properties": {"grid:code": "MGRS-13VCD", "eo:cloud_cover": 1.0, "datetime": "2024-08-01T00:00:00Z",
                       "proj:epsg": EPSG},
    }


@pytest.fixture
def fake_scl(monkeypatch):
    """Serve a synthetic SCL band instead of reading a cloud-optimised GeoTIFF over the network."""
    holder: dict[str, np.ndarray] = {}

    def fake_read(href: str, factor: int):
        transform = Affine(1000.0, 0.0, 0.0, 0.0, -1000.0, 2000.0)
        return holder["arr"], transform, CRS.from_epsg(EPSG)

    monkeypatch.setattr(R, "_read_decimated", fake_read)
    return holder


def test_water_fraction_counts_only_the_water_class(fake_scl):
    g = grid(n_cols=2, n_rows=1)
    # left cell: 2 water, 2 vegetation -> 0.5; right cell: 4 vegetation -> 0.0
    fake_scl["arr"] = np.array([[6, 6, 4, 4], [4, 4, 4, 4]], dtype=np.uint8)
    df, scenes = R.sentinel2_fractions([_scene()], g, log=lambda *_: None)
    left = df.set_index("cell_id").loc["0000_0000"]
    right = df.set_index("cell_id").loc["0001_0000"]
    assert left["water_fraction"] == pytest.approx(0.5)
    assert right["water_fraction"] == pytest.approx(0.0)
    assert right["vegetation_fraction"] == pytest.approx(1.0)
    assert len(scenes) == 1 and scenes[0]["stac_id"] == "S2_TEST"


def test_a_cell_under_cloud_is_recorded_as_unobserved(fake_scl):
    g = grid(n_cols=2, n_rows=1)
    # left cell is all cloud; right cell is clear water
    fake_scl["arr"] = np.array([[9, 9, 6, 6], [8, 10, 6, 6]], dtype=np.uint8)
    df = R.sentinel2_fractions([_scene()], g, log=lambda *_: None)[0].set_index("cell_id")
    assert np.isnan(df.loc["0000_0000", "water_fraction"]), "cloud must not read as dry land"
    assert df.loc["0000_0000", "cloud_share"] == pytest.approx(1.0)
    assert df.loc["0001_0000", "water_fraction"] == pytest.approx(1.0)


def test_a_cell_no_scene_covers_holds_null_and_no_pixels(fake_scl):
    g = grid(n_cols=3, n_rows=1)  # the third cell sits outside the synthetic scene
    fake_scl["arr"] = np.array([[6, 6, 4, 4], [4, 4, 4, 4]], dtype=np.uint8)
    df = R.sentinel2_fractions([_scene()], g, log=lambda *_: None)[0].set_index("cell_id")
    assert np.isnan(df.loc["0002_0000", "water_fraction"])
    assert df.loc["0002_0000", "pixels"] == 0


def test_an_unreadable_scene_does_not_lose_the_run(monkeypatch):
    g = grid(n_cols=1, n_rows=1)

    def boom(href: str, factor: int):
        raise OSError("connection reset")

    monkeypatch.setattr(R, "_read_decimated", boom)
    df, scenes = R.sentinel2_fractions([_scene()], g, log=lambda *_: None)
    assert scenes == []
    assert np.isnan(df["water_fraction"]).all() and (df["pixels"] == 0).all()


# ---------------------------------------------------------------- terrain


def test_dem_reports_mean_elevation_and_local_relief(monkeypatch):
    g = grid(n_cols=1, n_rows=1)
    monkeypatch.setattr(R, "_search", lambda *a, **k: [
        {"id": "DEM_TEST", "assets": {"data": {"href": "s3://copernicus-dem-30m/DEM_TEST/DEM_TEST.tif"}},
         "properties": {"datetime": "2021-01-01T00:00:00Z"}}
    ])
    arr = np.array([[400.0, 420.0], [380.0, 500.0]], dtype=np.float32)
    monkeypatch.setattr(
        R, "_read_decimated",
        lambda href, factor: (arr, Affine(1000.0, 0.0, 0.0, 0.0, -1000.0, 2000.0), CRS.from_epsg(EPSG)),
    )
    df, scenes = R.dem_stats((-110, 57, -103, 60), g, log=lambda *_: None)
    assert df["elevation_m"].iloc[0] == pytest.approx(425.0)
    assert df["relief_m"].iloc[0] == pytest.approx(120.0)
    assert scenes[0]["href"].startswith("https://"), "the s3:// href must be rewritten to the public mirror"


def test_raster_features_are_not_effort_and_name_their_source():
    keys = {k for k, *_ in R.RASTER_FEATURES}
    assert {"water_fraction", "elevation_m", "relief_m"} <= keys
    for _key, _col, _unit, bears_on, note in R.RASTER_FEATURES:
        assert bears_on in {"detection", "cover", "dispersal"}
        assert note, "a raster feature has to say what it is for; none of them detects ore"


# ---------------------------------------------------------------- landform grain


def test_landform_grain_finds_the_axis_of_parallel_ridges():
    """Ridges running east-west must report an axis near 0 or 180 degrees, not the gradient direction."""
    from affine import Affine as A

    rows, cols = 200, 200
    y = np.arange(rows)[:, None]
    z = 20.0 * np.sin(2 * np.pi * y / 8.0) * np.ones((1, cols))  # corrugations varying with northing only
    transform = A(0.005, 0.0, -106.0, 0.0, -0.005, 58.0)
    axis, coh = R._landform_grain(z, transform, 58.0)  # geographic transform
    mid = axis[60:140, 60:140]
    folded = np.minimum(mid % 180.0, 180.0 - (mid % 180.0))  # distance to the east-west axis
    assert np.nanmedian(folded) < 15.0, "ridges elongate east-west have an east-west axis"
    assert np.nanmedian(coh[60:140, 60:140]) > 0.5, "a clean corrugation is a coherent grain"


def test_flat_ground_has_no_grain_to_report():
    from affine import Affine as A

    z = np.full((200, 200), 300.0)
    _axis, coh = R._landform_grain(z, A(0.005, 0.0, -106.0, 0.0, -0.005, 58.0), 58.0)
    assert np.nanmax(coh) < 1e-6, "flat ground must not be given a direction"


def test_grain_is_an_axis_and_never_claims_a_direction():
    spec = next(s for s in R.RASTER_FEATURES if s[0] == "landform_grain_deg")
    note = spec[4].lower()
    assert "axis, not a direction" in note
    assert "geologist" in note


def test_a_regional_slope_alone_leaves_no_grain():
    """A broad tilt is not a landform: detrending must remove it, or every cell reports 'downhill'."""
    from affine import Affine as A

    rows, cols = 200, 200
    y = np.arange(rows)[:, None] * np.ones((1, cols))
    z = 300.0 + 0.5 * y  # a pure north-south ramp
    # 0.005 degree pixels: about 500 m, so the 15 km detrend window is 30 pixels of a 200 pixel tile
    _axis, coh = R._landform_grain(z, A(0.005, 0.0, -106.0, 0.0, -0.005, 58.0), 58.0)
    assert np.nanmedian(coh[60:140, 60:140]) < 0.2, "a regional slope must not read as a coherent grain"


def test_a_tile_too_small_to_detrend_reports_nothing():
    """Better no answer than an answer that is really the regional slope."""
    from affine import Affine as A

    z = 300.0 + np.arange(40)[:, None] * np.ones((1, 40))
    axis, coh = R._landform_grain(z, A(0.001, 0.0, -106.0, 0.0, -0.001, 58.0), 58.0)
    assert np.isnan(axis).all() and (coh == 0).all()


def test_ridges_on_a_slope_still_report_the_ridge_axis():
    """The real basin is tilted and corrugated at once; the axis must follow the landforms, not the tilt."""
    from affine import Affine as A

    rows, cols = 200, 200
    yy = np.arange(rows)[:, None] * np.ones((1, cols))
    xx = np.ones((rows, 1)) * np.arange(cols)[None, :]
    ridges = 15.0 * np.sin(2 * np.pi * (xx + yy) / 10.0)  # elongate along the 45 degree axis
    z = 300.0 + 0.8 * xx + ridges
    axis, coh = R._landform_grain(z, A(0.005, 0.0, -106.0, 0.0, -0.005, 58.0), 58.0)
    mid = axis[60:140, 60:140] % 180.0
    # the corrugation crests run northwest-southeast here; what matters is that it is not the slope's axis
    assert np.nanmedian(coh[60:140, 60:140]) > 0.3
    assert 100.0 < float(np.nanmedian(mid)) < 170.0

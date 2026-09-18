import json
import math

import pytest

from legacy_reader import crs
from legacy_reader.paths import PATHS

GRID = PATHS.grids / crs.GRID_NAME
needs_grid = pytest.mark.skipif(not GRID.is_file() or not PATHS.grids_lock.is_file(),
                                reason="run `lr crs fetch-grid` first")


def test_missing_grid_raises(tmp_path):
    with pytest.raises(crs.MissingGridError):
        crs.Nad27ToNad83(grid_dir=tmp_path, lock_path=tmp_path / "grids.lock")


@needs_grid
def test_grid_sha_mismatch_raises(tmp_path):
    lock = tmp_path / "grids.lock"
    lock.write_text(json.dumps({crs.GRID_NAME: {"sha256": "0" * 64}}))
    with pytest.raises(crs.GridMismatchError):
        crs.Nad27ToNad83(grid_dir=PATHS.grids, lock_path=lock)


@needs_grid
def test_unpinned_grid_raises(tmp_path):
    with pytest.raises(crs.GridMismatchError):
        crs.Nad27ToNad83(grid_dir=PATHS.grids, lock_path=tmp_path / "absent.lock")


@needs_grid
def test_noop_pipeline_is_caught_by_canary(monkeypatch):
    # A pipeline that silently applies no shift must never pass for a datum transformation.
    monkeypatch.setattr(crs, "NTV2_PIPELINE", "+proj=noop")
    with pytest.raises(crs.CanaryError):
        crs.Nad27ToNad83()


@needs_grid
def test_helmert_like_shift_is_caught_by_canary(monkeypatch):
    # The PROJ 9.2 silent fallback moved the canary 40.82 m; anything that far off must fail.
    monkeypatch.setattr(
        crs, "NTV2_PIPELINE",
        "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
        "+step +proj=cart +ellps=clrk66 +step +proj=helmert +x=-8 +y=160 +z=176 "
        "+step +inv +proj=cart +ellps=GRS80 +step +proj=unitconvert +xy_in=rad +xy_out=deg",
    )
    with pytest.raises(crs.CanaryError):
        crs.Nad27ToNad83()


@needs_grid
@pytest.mark.parametrize("ref", crs.REFERENCE_POINTS, ids=lambda r: r["label"])
def test_reference_shifts(ref):
    tr = crs.Nad27ToNad83()
    s = tr.shift(ref["lon"], ref["lat"])
    assert abs(s.dist_m - ref["shift_m"]) <= 0.3
    # Across the basin the NAD83 position lies west-northwest of the NAD27 one.
    assert 270 < s.bearing_deg < 320


@needs_grid
def test_transform_log_names_the_operation():
    tr = crs.Nad27ToNad83()
    assert tr.log.operation_name == "NAD27 to NAD83 (4)"
    assert tr.log.operation_code == "EPSG:1313"
    assert crs.GRID_NAME in tr.log.pipeline_definition
    assert len(tr.log.grid_sha256) == 64


@needs_grid
def test_outside_grid_raises():
    tr = crs.Nad27ToNad83()
    with pytest.raises(crs.OutsideGridError):
        tr.transform(10.0, 50.0)  # Europe: not covered by the Canadian grid


@needs_grid
def test_inverse_round_trip():
    tr = crs.Nad27ToNad83()
    x, y = tr.transform(-104.0, 58.2)
    lon, lat = tr.inverse(x, y)
    assert math.isclose(lon, -104.0, abs_tol=1e-7) and math.isclose(lat, 58.2, abs_tol=1e-7)


def test_utm_zone_for_lon():
    assert crs.utm_zone_for_lon(-109.5) == 12
    assert crs.utm_zone_for_lon(-105.0) == 13
    assert crs.utm_zone_for_lon(-108.0001) == 12
    assert crs.utm_zone_for_lon(-107.9999) == 13


def test_utm_same_datum_round_trip():
    e, n = crs.geographic_to_utm(-104.0, 58.2, 13, "NAD27")
    lon, lat = crs.utm_to_geographic(e, n, 13, "NAD 27")
    assert math.isclose(lon, -104.0, abs_tol=1e-8) and math.isclose(lat, 58.2, abs_tol=1e-8)


def test_utm_rejects_unknown_datum_and_zone():
    with pytest.raises(crs.CrsError):
        crs.utm_to_geographic(500000, 6450000, 13, "WGS72")
    with pytest.raises(crs.CrsError):
        crs.utm_to_geographic(500000, 6450000, 3, "NAD83")


@pytest.mark.parametrize("e,n,expected", [
    (575000.0, 6450000.0, False),   # plausible UTM
    (10000.0, 5000.0, True),        # mine grid
    (4000.0, 10200.0, True),        # station-style local grid
    (6450000.0, 575000.0, True),    # swapped easting and northing
    (None, 6450000.0, False),
])
def test_local_grid_heuristic(e, n, expected):
    assert crs.looks_like_local_grid(e, n) is expected


@needs_grid
def test_shift_grid_shape_and_range():
    g = crs.shift_grid(crs.Nad27ToNad83())
    n = g["nlat"] * g["nlon"]
    assert len(g["de_m"]) == n and len(g["dn_m"]) == n
    assert 20 < g["stats"]["min_m"] < g["stats"]["max_m"] < 100
    for c in g["checks"]:
        assert abs(c["computed_m"] - c["shift_m"]) <= 0.3

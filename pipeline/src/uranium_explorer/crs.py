"""Coordinate handling: NAD27 to NAD83 through the NRCan NTv2 grid, never a silent fallback.

Why this module is strict:
- `Transformer.from_crs(4267, 4269)` picks whatever operation PROJ can run. Without the grid it has
  either fallen back to a Helmert (PROJ 9.2: 40.82 m at 58.0 N 105.0 W instead of 34.2 m) or returned
  NaN (PROJ 9.8). Both hide the problem.
- So the datum step is an explicit pipeline naming the grid file, the grid's sha256 is pinned, PROJ's
  network is off, and a canary point with a known shift is checked every time the transformer is built.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path

import httpx
import pyproj
import pyproj.datadir
import pyproj.network
from pyproj import Geod, Transformer
from pyproj.exceptions import ProjError

from .ids import sha256_file
from .paths import PATHS

GRID_NAME = "ca_nrc_ntv2_0.tif"
GRID_URL = f"https://cdn.proj.org/{GRID_NAME}"
OPERATION_NAME = "NAD27 to NAD83 (4)"
OPERATION_CODE = "EPSG:1313"
ACCURACY_M = 1.5
NTV2_PIPELINE = (
    "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
    f"+step +proj=hgridshift +grids={GRID_NAME} "
    "+step +proj=unitconvert +xy_in=rad +xy_out=deg"
)

# Known shifts on this grid (computed 2026-09-17, PROJ 9.2 and 9.8 agree): used as canaries and tests.
CANARY = {"lon": -105.0, "lat": 58.0, "shift_m": 34.2, "tol_m": 0.5}
REFERENCE_POINTS = [
    {"label": "Eastern Athabasca Basin", "lon": -105.0, "lat": 58.0, "shift_m": 34.2},
    {"label": "Near Patterson Lake", "lon": -109.5, "lat": 57.9, "shift_m": 50.8},
]

# Shift-grid export extent (the web app's datum lens).
GRID_EXTENT = {"lat0": 56.0, "lon0": -112.0, "dlat": 0.1, "dlon": 0.1, "nlat": 41, "nlon": 101}

_GEOD = Geod(ellps="GRS80")

# UTM EPSG codes by datum and zone. Saskatchewan spans zones 12 and 13; 11 and 14 accepted for border files.
_UTM_EPSG = {
    "NAD27": {11: 26711, 12: 26712, 13: 26713, 14: 26714},
    "NAD83": {11: 26911, 12: 26912, 13: 26913, 14: 26914},
}
_GEOG_EPSG = {"NAD27": 4267, "NAD83": 4269}


class CrsError(Exception):
    """Base class for coordinate failures that must stop a transform."""


class MissingGridError(CrsError):
    pass


class GridMismatchError(CrsError):
    pass


class CanaryError(CrsError):
    pass


class OutsideGridError(CrsError):
    pass


@dataclass(frozen=True)
class Shift:
    de_m: float
    dn_m: float
    dist_m: float
    bearing_deg: float


@dataclass(frozen=True)
class TransformLog:
    operation_name: str
    operation_code: str
    pipeline_definition: str
    grid_file: str
    grid_sha256: str
    proj_version: str
    pyproj_version: str
    accuracy_m: float


def geodesic_shift(lon0: float, lat0: float, lon1: float, lat1: float) -> Shift:
    az, _, dist = _GEOD.inv(lon0, lat0, lon1, lat1)
    rad = math.radians(az)
    return Shift(de_m=dist * math.sin(rad), dn_m=dist * math.cos(rad), dist_m=dist, bearing_deg=az % 360.0)


def read_lock(lock_path: Path) -> dict:
    if not lock_path.is_file():
        return {}
    return json.loads(lock_path.read_text())


def fetch_grid(grid_dir: Path | None = None, lock_path: Path | None = None, timeout: float = 180.0) -> Path:
    """Download the NTv2 grid once and pin its sha256 in grids.lock. Never redistributed."""
    grid_dir = grid_dir or PATHS.grids
    lock_path = lock_path or PATHS.grids_lock
    grid_dir.mkdir(parents=True, exist_ok=True)
    target = grid_dir / GRID_NAME
    if not target.is_file():
        tmp = target.with_suffix(".part")
        with httpx.stream("GET", GRID_URL, follow_redirects=True, timeout=timeout) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        tmp.rename(target)
    digest = sha256_file(target)
    lock = read_lock(lock_path)
    pinned = lock.get(GRID_NAME, {}).get("sha256")
    if pinned and pinned != digest:
        raise GridMismatchError(f"{target} sha256 {digest} does not match grids.lock {pinned}")
    if not pinned:
        lock[GRID_NAME] = {"sha256": digest, "url": GRID_URL, "bytes": target.stat().st_size,
                           "pinned_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
        lock_path.write_text(json.dumps(lock, indent=2) + "\n")
    return target


class Nad27ToNad83:
    """The only way the pipeline converts NAD27 geographic coordinates to NAD83."""

    def __init__(self, grid_dir: Path | None = None, lock_path: Path | None = None, check_lock: bool = True):
        grid_dir = grid_dir or PATHS.grids
        lock_path = lock_path or PATHS.grids_lock
        grid = grid_dir / GRID_NAME
        if not grid.is_file():
            raise MissingGridError(f"NTv2 grid not found at {grid}; run `ue crs fetch-grid`")
        self.grid_sha256 = sha256_file(grid)
        if check_lock:
            pinned = read_lock(lock_path).get(GRID_NAME, {}).get("sha256")
            if not pinned:
                raise GridMismatchError(f"no sha256 pinned for {GRID_NAME} in {lock_path}")
            if pinned != self.grid_sha256:
                raise GridMismatchError(f"{grid} sha256 {self.grid_sha256} != pinned {pinned}")
        pyproj.network.set_network_enabled(False)
        pyproj.datadir.append_data_dir(str(grid_dir))
        try:
            self._t = Transformer.from_pipeline(NTV2_PIPELINE)
        except ProjError as e:  # PROJ cannot open the grid: never fall back
            raise MissingGridError(f"PROJ could not load {GRID_NAME} from {grid_dir}: {e}") from e
        self.log = TransformLog(
            operation_name=OPERATION_NAME,
            operation_code=OPERATION_CODE,
            pipeline_definition=NTV2_PIPELINE,
            grid_file=GRID_NAME,
            grid_sha256=self.grid_sha256,
            proj_version=pyproj.proj_version_str,
            pyproj_version=pyproj.__version__,
            accuracy_m=ACCURACY_M,
        )
        self._canary()

    def _canary(self) -> None:
        s = self.shift(CANARY["lon"], CANARY["lat"])
        if abs(s.dist_m - CANARY["shift_m"]) > CANARY["tol_m"]:
            raise CanaryError(
                f"canary shift {s.dist_m:.2f} m at {CANARY['lat']} N {abs(CANARY['lon'])} W, expected "
                f"{CANARY['shift_m']} +/- {CANARY['tol_m']} m: the grid is not being applied"
            )

    def transform(self, lon: float, lat: float) -> tuple[float, float]:
        try:
            x, y = self._t.transform(lon, lat, errcheck=True)
        except ProjError as e:
            raise OutsideGridError(f"{lat} N {lon} E is outside the NTv2 grid: {e}") from e
        if not (math.isfinite(x) and math.isfinite(y)):
            raise OutsideGridError(f"{lat} N {lon} E is outside the NTv2 grid")
        return x, y

    def shift(self, lon: float, lat: float) -> Shift:
        x, y = self.transform(lon, lat)
        return geodesic_shift(lon, lat, x, y)

    def inverse(self, lon: float, lat: float) -> tuple[float, float]:
        """NAD83 -> NAD27 (used to place 'misread' positions: printed NAD27 numbers read as NAD83)."""
        try:
            x, y = self._t.transform(lon, lat, direction="INVERSE", errcheck=True)
        except ProjError as e:
            raise OutsideGridError(str(e)) from e
        return x, y


def utm_zone_for_lon(lon: float) -> int:
    return int((lon + 180.0) // 6.0) + 1


def utm_to_geographic(easting: float, northing: float, zone: int, datum: str) -> tuple[float, float]:
    """Same-datum step only: UTM on datum X -> geographic on datum X. Never changes datum."""
    datum = datum.upper().replace(" ", "")
    if datum not in _UTM_EPSG:
        raise CrsError(f"unsupported datum {datum!r}")
    if zone not in _UTM_EPSG[datum]:
        raise CrsError(f"unsupported UTM zone {zone} for {datum}")
    t = Transformer.from_crs(_UTM_EPSG[datum][zone], _GEOG_EPSG[datum], always_xy=True)
    lon, lat = t.transform(easting, northing, errcheck=True)
    return lon, lat


def geographic_to_utm(lon: float, lat: float, zone: int, datum: str) -> tuple[float, float]:
    datum = datum.upper().replace(" ", "")
    t = Transformer.from_crs(_GEOG_EPSG[datum], _UTM_EPSG[datum][zone], always_xy=True)
    return t.transform(lon, lat, errcheck=True)


def looks_like_local_grid(easting: float | None, northing: float | None) -> bool:
    """Plausibility for UTM in Saskatchewan: northings roughly 5.4M to 6.7M, eastings 150k to 850k."""
    if easting is None or northing is None:
        return False
    return not (150_000 <= easting <= 850_000 and 5_400_000 <= northing <= 6_700_000)


def shift_grid(tr: Nad27ToNad83) -> dict:
    """Shift vectors (NAD27 -> NAD83) on a regular grid for the web app's datum lens."""
    e = GRID_EXTENT
    de: list[float] = []
    dn: list[float] = []
    mags: list[float] = []
    for i in range(e["nlat"]):
        lat = round(e["lat0"] + i * e["dlat"], 6)
        for j in range(e["nlon"]):
            lon = round(e["lon0"] + j * e["dlon"], 6)
            s = tr.shift(lon, lat)
            de.append(round(s.de_m, 2))
            dn.append(round(s.dn_m, 2))
            mags.append(s.dist_m)
    checks = []
    for p in REFERENCE_POINTS:
        s = tr.shift(p["lon"], p["lat"])
        checks.append({**p, "computed_m": round(s.dist_m, 2), "bearing_deg": round(s.bearing_deg, 1)})
    return {
        "from": "EPSG:4267",
        "to": "EPSG:4269",
        "operation": tr.log.operation_name,
        "operation_code": tr.log.operation_code,
        "grid_file": tr.log.grid_file,
        "grid_sha256": tr.log.grid_sha256,
        "proj_version": tr.log.proj_version,
        "pyproj_version": tr.log.pyproj_version,
        "accuracy_m": tr.log.accuracy_m,
        **e,
        "order": "row-major, south to north, west to east",
        "de_m": de,
        "dn_m": dn,
        "stats": {"min_m": round(min(mags), 2), "max_m": round(max(mags), 2),
                  "mean_m": round(sum(mags) / len(mags), 2)},
        "checks": checks,
        "computed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }

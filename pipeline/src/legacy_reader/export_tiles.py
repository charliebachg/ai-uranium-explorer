"""`lr export-tiles`: vector tiles for the map, one PMTiles archive per evidence source, built by tippecanoe.

A GeoJSON layer is fetched whole the first time it is switched on; 16 MB of evidence for a reader who wants
one conductor. A tile archive is fetched by view and zoom, and it scales to the whole province. The rules that
make a tile an honest picture of the layer: no feature is ever dropped to fit a tile (`-pf -pk`), so what is
on screen at any zoom is every feature the export holds, simplified in shape only; the layer inside the
archive is named after the map's source id, so the web needs no lookup table; and every archive is hashed
beside the hash of the GeoJSON it was built from, in `tiles/manifest.json`, so a stale archive is visible.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .paths import PATHS

TOOL = "export_tiles"
MANIFEST_VERSION = "tiles/v1"


@dataclass(frozen=True)
class TileExport:
    source_id: str      # the map's source id; also the layer name inside the archive
    geojson: str        # relative to web/public/data
    minzoom: int = 4
    maxzoom: int = 12


#: the sources worth tiling: the deferred evidence layers and the score cells. Small eager layers (basin
#: outline, NTS grid, deposits) stay GeoJSON.
EXPORTS: tuple[TileExport, ...] = (
    TileExport("conductors", "context/em_conductors.geojson"),
    TileExport("faults", "context/faults.geojson"),
    TileExport("host", "context/graphitic_host.geojson"),
    TileExport("lakesed", "context/lake_sediment_u.geojson"),
    TileExport("lakewater", "context/lake_water_u.geojson"),
    TileExport("boulders", "context/radioactive_boulders.geojson"),
    TileExport("surveyair", "context/surveys_airborne.geojson"),
    TileExport("surveyground", "context/surveys_ground.geojson"),
    TileExport("cells", "prospect/scores.geojson", minzoom=5),
)


def web_data_dir() -> Path:
    return PATHS.root / "web" / "public" / "data"


def tippecanoe_args(ex: TileExport, geojson: Path, out: Path) -> list[str]:
    """Every feature kept at every zoom; ids generated so feature state (hover, selected) works on tiles."""
    # -r1: tippecanoe's default drops points at a rate of 2.5 per zoom below the base zoom, which would leave
    # a handful of the 30,534 score cells at province scale; a drop rate of 1 keeps every point at every zoom
    return ["tippecanoe", "-q", "-o", str(out), "--force", f"-Z{ex.minzoom}", f"-z{ex.maxzoom}",
            "-pf", "-pk", "-r1", "--generate-ids", "-l", ex.source_id, str(geojson)]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _feature_count(geojson: Path) -> int:
    return len(json.loads(geojson.read_text()).get("features") or [])


def build(exports: tuple[TileExport, ...] = EXPORTS, data_dir: Path | None = None,
          log: Callable[[str], None] = print, run: Callable[[list[str]], Any] | None = None) -> dict[str, Any]:
    """Build every archive whose GeoJSON exists and write tiles/manifest.json. Missing inputs are named."""
    if shutil.which("tippecanoe") is None and run is None:
        raise RuntimeError("tippecanoe is not installed (brew install tippecanoe)")
    root = data_dir or web_data_dir()
    tiles_dir = root / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    runner = run or (lambda args: subprocess.run(args, check=True, capture_output=True, text=True))
    version = "unknown"
    if run is None:
        version = subprocess.run(["tippecanoe", "--version"], capture_output=True, text=True).stderr.strip() or \
            subprocess.run(["tippecanoe", "--version"], capture_output=True, text=True).stdout.strip()
    entries: dict[str, Any] = {}
    skipped: list[str] = []
    for ex in exports:
        src = root / ex.geojson
        if not src.is_file():
            skipped.append(f"{ex.source_id}: {ex.geojson} not exported yet")
            continue
        out = tiles_dir / f"{ex.source_id}.pmtiles"
        runner(tippecanoe_args(ex, src, out))
        entries[ex.source_id] = {
            "file": out.name, "bytes": out.stat().st_size, "sha256": _sha256(out),
            "minzoom": ex.minzoom, "maxzoom": ex.maxzoom, "layer": ex.source_id,
            "features": _feature_count(src), "source_geojson": ex.geojson, "source_sha256": _sha256(src),
        }
        log(f"  {ex.source_id:<13} {entries[ex.source_id]['features']:>7,} features  {out.stat().st_size / 1e6:6.2f} MB  z{ex.minzoom}-{ex.maxzoom}")
    manifest = {"version": MANIFEST_VERSION, "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "tippecanoe": version, "rules": "no feature dropped at any zoom (-pf -pk); ids generated; layer = source id",
                "tiles": entries, "skipped": skipped}
    (tiles_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    for s in skipped:
        log(f"  skipped {s}")
    log(f"  {len(entries)} archive(s), {sum(e['bytes'] for e in entries.values()) / 1e6:.1f} MB, manifest {tiles_dir / 'manifest.json'}")
    return manifest

"""`lr export-web`: write the web contract (see web/src/data/contract.ts) into web/public/data.

Phase 1 exports the provincial context: manifest (value-backed stats, sources, licences), bulk drillhole
layers, year histogram, basin outline, deposit footprints, uranium occurrences and the NTS grid. Report
artifacts are added by later phases.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from . import __version__, crs
from .ids import sha256_file, sha256_json, short
from .index import BANNED_URL_FRAGMENT, load_pull_log, read_features
from .paths import PATHS
from .values import PROJ_TOOL, derived, registry, stat

SCHEMA_VERSION = "1.0.0"
_YEAR = re.compile(r"(19[0-9]{2}|20[0-2][0-9])")


def _r6(c: list[float]) -> list[float]:
    return [round(c[0], 6), round(c[1], 6)]


def _year_from_text(s: str | None) -> int | None:
    if not s:
        return None
    m = _YEAR.search(s)
    y = int(m.group(1)) if m else None
    return y if y in PLAUSIBLE_YEARS else None


PLAUSIBLE_YEARS = range(1900, 2027)


def _year_from_epoch_ms(ms: int | float | None) -> int | None:
    """Some GeoDS start dates decode to impossible years (e.g. 192); those count as undated."""
    if ms is None:
        return None
    try:
        y = (dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(milliseconds=ms)).year
    except OverflowError:
        return None
    return y if y in PLAUSIBLE_YEARS else None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2)


def _dump(path: Path, obj: Any, compact: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(obj, separators=(",", ":")) if compact else json.dumps(obj, indent=2))
    tmp.rename(path)


# ---------- bulk layers ----------

def build_compilation(read_holes: set[int] | None = None) -> tuple[dict[str, Any], list[str], Counter, int]:
    feats = read_features("compilation")
    companies: dict[str, int] = {}
    years: Counter = Counter()
    undated = 0
    out = []
    for f in feats:
        p = f["properties"]
        oid = int(p["OBJECTID"])
        props: dict[str, Any] = {"n": (p.get("DRILLHOLE_NAME") or "").strip() or "(unnamed)"}
        co = (p.get("COMPANY") or "").strip()
        if co:
            props["co"] = companies.setdefault(co, len(companies))
        y = _year_from_text(p.get("DATE_DRILLED"))
        if y:
            props["y"] = y
            years[y] += 1
        else:
            undated += 1
        for key, field in (("az", "DH_AZIMUTH"), ("dip", "DH_INCLINATION"), ("len", "TOTAL_DH_LENGTH_M")):
            val = _num(p.get(field))
            if val is not None:
                props[key] = val
        props["u"] = 1 if "ranium" in (p.get("COMMODITY_OF_INTEREST") or "") else 0
        props["rd"] = 1 if read_holes and oid in read_holes else 0
        src = (p.get("SOURCE") or "").strip()
        if src:
            props["src"] = src
        out.append({"type": "Feature", "id": oid, "properties": props,
                    "geometry": {"type": "Point", "coordinates": _r6(f["geometry"]["coordinates"])}})
    return {"type": "FeatureCollection", "features": out}, list(companies), years, undated


def build_geods(read_holes: set[int] | None = None) -> tuple[dict[str, Any], Counter, int, int]:
    feats = read_features("geods_holes")
    years: Counter = Counter()
    undated = 0
    with_file = 0
    out = []
    for f in feats:
        p = f["properties"]
        oid = int(p.get("OBJECTID") or p.get("ObjectID"))
        props: dict[str, Any] = {"n": (p.get("HOLE_NAME") or "").strip() or "(unnamed)"}
        af = (p.get("TEMP_ASSMNT_FILE_NUM") or "").strip()
        if af:
            props["af"] = af
            with_file += 1
        y = _year_from_epoch_ms(p.get("DRILLNG_START_DATE"))
        if y:
            props["y"] = y
            years[y] += 1
        else:
            undated += 1
        for key, field in (("td", "TOTL_MSRD_DPTH_M"), ("inc", "INCLNTN_DEG"), ("az", "AZM_DEG")):
            val = _num(p.get(field))
            if val is not None:
                props[key] = val
        if p.get("UTM_DATUM_TYPE"):
            props["dt"] = p["UTM_DATUM_TYPE"]
        props["rd"] = 1 if read_holes and oid in read_holes else 0
        out.append({"type": "Feature", "id": oid, "properties": props,
                    "geometry": {"type": "Point", "coordinates": _r6(f["geometry"]["coordinates"])}})
    return {"type": "FeatureCollection", "features": out}, years, undated, with_file


def build_year_histogram(cmp_years: Counter, cmp_undated: int, gds_years: Counter, gds_undated: int) -> dict[str, Any]:
    all_years = sorted(set(cmp_years) | set(gds_years))
    vals = []
    bins = []
    for y in range(min(all_years), max(all_years) + 1):
        c, g = f"h:{y}:cmp", f"h:{y}:gds"
        vals += [stat(c, cmp_years.get(y, 0)), stat(g, gds_years.get(y, 0))]
        bins.append({"year": y, "cmp": c, "gds": g})
    vals += [stat("h:undated:cmp", cmp_undated, note="compilation collars with no parseable drilling date"),
             stat("h:undated:gds", gds_undated, note="GeoDS holes with no drilling start date")]
    return {"bins": bins, "undated_cmp": "h:undated:cmp", "undated_gds": "h:undated:gds", "values": registry(*vals)}


# ---------- context layers ----------

def _count_vertices(geom: Any) -> int:
    if geom.geom_type == "Polygon":
        return len(geom.exterior.coords) + sum(len(i.coords) for i in geom.interiors)
    if geom.geom_type == "MultiPolygon":
        return sum(_count_vertices(g) for g in geom.geoms)
    return 0


def build_basin(max_vertices: int = 2000) -> dict[str, Any]:
    polys = [shape(f["geometry"]).buffer(0) for f in read_features("basin_geology")]
    merged = unary_union(polys)
    # drop slivers and holes smaller than ~1 km^2 (degrees^2 at 58 N is about 6,000 km^2)
    parts = [merged] if merged.geom_type == "Polygon" else list(merged.geoms)
    parts = [p for p in parts if p.area > 2e-4]
    from shapely.geometry import MultiPolygon, Polygon
    cleaned = [Polygon(p.exterior, [i for i in p.interiors if Polygon(i).area > 2e-4]) for p in parts]
    geom = cleaned[0] if len(cleaned) == 1 else MultiPolygon(cleaned)
    tol = 0.0005
    simple = geom.simplify(tol, preserve_topology=True)
    while _count_vertices(simple) > max_vertices:
        tol *= 1.5
        simple = geom.simplify(tol, preserve_topology=True)
    polys_out = [simple] if simple.geom_type == "Polygon" else sorted(simple.geoms, key=lambda g: -g.area)
    # ordered outline for the draw-in animation: exterior of the largest part first
    lines = [[_r6(list(c)) for c in p.exterior.coords] for p in polys_out]

    def rounded(g: Any) -> Any:
        m = mapping(g)
        if m["type"] == "Polygon":
            m["coordinates"] = [[_r6(list(c)) for c in ring] for ring in m["coordinates"]]
        else:
            m["coordinates"] = [[[_r6(list(c)) for c in ring] for ring in poly] for poly in m["coordinates"]]
        return m

    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": 1, "properties": {"role": "area", "name": "Athabasca Basin (Athabasca Supergroup)"},
         "geometry": rounded(simple)},
        {"type": "Feature", "id": 2, "properties": {"role": "outline"},
         "geometry": {"type": "MultiLineString", "coordinates": lines}},
    ]}


def build_deposits() -> dict[str, Any]:
    out = []
    for f in read_features("uranium_deposit_footprints"):
        p = f["properties"]
        g = shape(f["geometry"]).simplify(0.00005, preserve_topology=True)
        m = mapping(g)
        out.append({"type": "Feature", "id": int(p["OBJECTID"]),
                    "properties": {"deposit": p.get("DEPOSIT") or "", "zone": p.get("NAME_ZONE") or ""},
                    "geometry": json.loads(json.dumps(m))})
    return {"type": "FeatureCollection", "features": out}


def build_occurrences() -> dict[str, Any]:
    out = []
    for f in read_features("mineral_deposits_uranium"):
        p = f["properties"]
        out.append({"type": "Feature", "id": int(p["OBJECTID"]),
                    "properties": {"name": p.get("NAME") or "", "smdi": str(p.get("SMDI") or ""),
                                   "status": p.get("SYMBOLOGY_STATUS") or p.get("STATUS") or "",
                                   "link": p.get("WEBLINK") or ""},
                    "geometry": {"type": "Point", "coordinates": _r6(f["geometry"]["coordinates"])}})
    return {"type": "FeatureCollection", "features": out}


def build_nts() -> dict[str, Any]:
    out = []
    i = 0
    for key, scale in (("nts_250k", "250k"), ("nts_50k", "50k")):
        for f in read_features(key):
            i += 1
            # NTS sheets are graticule rectangles; the service returns densified edges (about 90 vertices)
            x0, y0, x1, y1 = (round(v, 6) for v in shape(f["geometry"]).bounds)
            ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
            code = f["properties"]["IDENTIF"]
            out.append({"type": "Feature", "id": i, "properties": {"code": code, "s": scale, "role": "sheet"},
                        "geometry": {"type": "Polygon", "coordinates": [ring]}})
            # one label point per sheet: polygon labels would repeat once per vector tile
            i += 1
            out.append({"type": "Feature", "id": i, "properties": {"code": code, "s": scale, "role": "label"},
                        "geometry": {"type": "Point", "coordinates": [round((x0 + x1) / 2, 6), round((y0 + y1) / 2, 6)]}})
    return {"type": "FeatureCollection", "features": out}


# ---------- manifest ----------

def build_manifest(pull: dict[str, dict[str, Any]], companies: list[str], stats: dict[str, dict[str, Any]],
                   artifacts: dict[str, dict[str, Any]], datum_grid: dict[str, Any], files_read: int,
                   public_safe: bool, page_images: bool) -> dict[str, Any]:
    sources = []
    for key in ("compilation", "geods_holes", "file_index_uranium", "assessment_info_1", "basin_geology",
                "uranium_deposit_footprints", "mineral_deposits_uranium", "nts_50k"):
        r = pull[key]
        if BANNED_URL_FRAGMENT in r["url"]:
            raise ValueError("banned service in manifest sources")
        attribution = f"{r['publisher']}. {r['licence']}."
        src = {"id": key, "title": r["title"], "publisher": r["publisher"], "licence": r["licence"],
               "attribution_html": attribution, "url": r["url"], "retrieved_at": r["retrieved_at"],
               "redistributable": r["redistributable"]}
        if r.get("licence_url"):
            src["licence_url"] = r["licence_url"]
        sources.append(src)
    sources.append({
        "id": "ntv2_grid", "title": f"NTv2_0 NAD27 to NAD83 grid ({crs.GRID_NAME}), used to compute shift vectors",
        "publisher": "Natural Resources Canada, distributed by OSGeo PROJ-data",
        "licence": "Grid licence not recorded; the grid itself is not redistributed, only derived shift vectors",
        "attribution_html": "Datum shifts computed with PROJ and the NRCan NTv2_0 grid.",
        "url": crs.GRID_URL, "retrieved_at": datum_grid["computed_at"], "redistributable": False,
    })

    ref_points = []
    for c in datum_grid["checks"]:
        key = "east" if c["label"].startswith("Eastern") else "patterson"
        vid = f"m:shift_ref_{key}"
        stats[vid] = derived(vid, round(c["computed_m"], 1), "m1", "ntv2_shift", tool=PROJ_TOOL, unit="m",
                             params={"lon": c["lon"], "lat": c["lat"], "operation": datum_grid["operation"]},
                             note=f"NAD27 to NAD83 shift at {c['lat']} N {abs(c['lon'])} W")
        ref_points.append({"label": c["label"], "lonlat": [c["lon"], c["lat"]], "shift_m": vid})

    created = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    return {
        "schema_version": SCHEMA_VERSION,
        "build": {"id": short(sha256_json([created, list(artifacts)])), "created_at": created,
                  "pipeline_version": __version__, "page_images": page_images, "public_safe": public_safe,
                  "fixture": False},
        "bbox": [-110.1, 49.0, -101.3, 60.0],
        "stats": stats,
        "ref_points": ref_points,
        "sources": sources,
        "dictionaries": {"companies": companies},
        "artifacts": artifacts,
    }


def export(public_safe: bool = False, log: Any = print) -> Path:
    target = PATHS.web_data
    pull = load_pull_log()
    missing = [k for k in ("compilation", "geods_holes", "file_index_uranium", "basin_geology") if k not in pull]
    if missing:
        raise RuntimeError(f"missing pulls {missing}; run `lr index pull`")
    grid_path = target / "context" / "datum_grid.json"
    if not grid_path.is_file():
        raise RuntimeError("datum grid missing; run `lr crs shift-grid`")
    datum_grid = json.loads(grid_path.read_text())

    stage = Path(tempfile.mkdtemp(prefix="lr_export_", dir=PATHS.data))
    artifacts: dict[str, dict[str, Any]] = {}

    def write(rel: str, obj: Any, count: int | None = None) -> None:
        p = stage / rel
        _dump(p, obj)
        a: dict[str, Any] = {"path": rel, "bytes": p.stat().st_size, "sha256": sha256_file(p)}
        if count is not None:
            a["count"] = count
        artifacts[rel] = a
        log(f"  {rel}: {a['bytes'] / 1024:.0f} KB" + (f", {count} features" if count is not None else ""))

    cmp_fc, companies, cmp_years, cmp_undated = build_compilation()
    write("bulk/compilation_collars.geojson", cmp_fc, len(cmp_fc["features"]))
    gds_fc, gds_years, gds_undated, gds_with_file = build_geods()
    if public_safe:
        gds_fc = {"type": "FeatureCollection", "features": [f for f in gds_fc["features"] if f["properties"]["rd"]]}
    write("bulk/geods_holes.geojson", gds_fc, len(gds_fc["features"]))
    write("bulk/year_histogram.json", build_year_histogram(cmp_years, cmp_undated, gds_years, gds_undated))
    basin = build_basin()
    write("context/basin.geojson", basin, 2)
    deposits = build_deposits()
    write("context/uranium_deposits.geojson", deposits, len(deposits["features"]))
    occ = build_occurrences()
    write("context/mineral_deposits_u.geojson", occ, len(occ["features"]))
    nts = build_nts()
    write("context/nts_grid.geojson", nts, len(nts["features"]))
    shutil.copyfile(grid_path, stage / "context" / "datum_grid.json")
    artifacts["context/datum_grid.json"] = {"path": "context/datum_grid.json", "bytes": grid_path.stat().st_size,
                                            "sha256": sha256_file(grid_path)}

    # the files the dashboard can open: the reports index this export sits beside, else what has been assembled
    index_path = PATHS.web_data / "reports" / "index.json"
    if index_path.is_file():
        files_read = len(json.loads(index_path.read_text()).get("reports") or [])
    else:
        from .assemble import assembled_files
        files_read = len(assembled_files())
    cmp_u = sum(1 for f in cmp_fc["features"] if f["properties"]["u"])
    stats = registry(
        stat("m:compilation_collars", pull["compilation"]["count"],
             note="Minerals and Quaternary Drillhole Compilation: all features, province-wide"),
        stat("m:compilation_uranium", cmp_u, note="compilation collars with uranium as commodity of interest"),
        stat("m:geods_holes", pull["geods_holes"]["count"], note="GeoDS subsurface drilling layer, SYS_SRC='DH'"),
        stat("m:geods_holes_with_file", gds_with_file, note="GeoDS holes carrying an assessment file number"),
        stat("m:uranium_files", pull["file_index_uranium"]["count"],
             note="GeoDS assessment file index records with CMMDTY LIKE '%ranium%'"),
        stat("m:files_read", files_read, note="uranium-tagged assessment files read by this pipeline"),
        stat("m:uranium_deposit_footprints", len(deposits["features"])),
        stat("m:uranium_occurrences", len(occ["features"]),
             note="Saskatchewan Mineral Deposit Index records with uranium as a primary commodity"),
        stat("m:undated_compilation", cmp_undated),
    )
    manifest = build_manifest(pull, companies, stats, artifacts, datum_grid, files_read, public_safe,
                              page_images=False)
    _dump(stage / "manifest.json", manifest, compact=False)

    # atomic-ish swap of the exported files (pages/ and reports/ from later phases are left untouched)
    for rel in [*artifacts, "manifest.json"]:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        (stage / rel).replace(dst)
    shutil.rmtree(stage, ignore_errors=True)
    log(f"exported to {target}")
    return target

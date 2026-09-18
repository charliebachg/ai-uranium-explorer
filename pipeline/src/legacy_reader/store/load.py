"""`lr store rebuild`: flatten every stage into the tiered DuckDB store, plus the two GeoParquet layers.

The JSON artifacts each stage writes stay the source of truth; this puts them in SQL so the run can be
questioned: which values carry no box, which check fired where, how far each collar sits from its provincial
twin. Rebuilt from scratch every time, so it is never half-updated.

Which tier each table lands in, and why:

  native.layer, native.file_index   what the provincial services published (a pull's own metadata, and the
                                    file index's own facts about each assessment file)
  read.*                            everything that exists because OCR and the vision model read a page:
                                    the pages themselves, the tables found on them, the values, the checks
  derived.collar_position           a position computed from read coordinates through a named grid operation
  derived.crosscheck                a match computed between a read collar and a native record

`read.v_report` is the one place the file's native metadata and our read counts appear side by side, and it
carries a `tiers` column saying so.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pandas as pd

from .. import __version__
from ..assemble import assembled_files, read_assembled
from ..crosscheck import read_crosscheck
from ..export_reports import file_metadata
from ..index import load_pull_log
from ..paths import PATHS
from ..position import read_positions
from . import connect, insert_frame, tier_audit, write_meta

#: frame name -> (schema, table). The mapping is the tiering decision, in one place.
PLACEMENT: dict[str, tuple[str, str]] = {
    "file_index": ("native", "file_index"),
    "layer": ("native", "layer"),
    "report": ("read", "report"),
    "pdf": ("read", "pdf"),
    "page": ("read", "page"),
    "table": ("read", "table"),
    "record": ("read", "record"),
    "field_value": ("read", "field_value"),
    "validator_outcome": ("read", "validator_outcome"),
    "collar_position": ("derived", "collar_position"),
    "crosscheck": ("derived", "crosscheck"),
}


def _native_layers() -> pd.DataFrame:
    """The pull log, as the native layer registry. Empty until `lr index pull` has run."""
    rows = []
    for key, r in load_pull_log().items():
        rows.append({
            "layer_key": key, "title": r.get("title") or key, "service_url": r.get("url") or "",
            "layer_id": None, "where_clause": r.get("where"), "out_sr": 4326,
            "record_count": int(r.get("count") or 0), "payload_sha256": None,
            "licence": r.get("licence") or "not stated", "licence_url": r.get("licence_url"),
            "redistributable": bool(r.get("redistributable")), "retrieved_at": r.get("retrieved_at") or "",
            "bears_on": None, "role": "label" if "deposit" in key else "context", "notes": r.get("path"),
        })
    return pd.DataFrame(rows)


def _native_file_index(file_nums: list[str]) -> pd.DataFrame:
    """What the provincial file index says about each file we read. Native facts, kept out of `read.report`."""
    rows = []
    for file_num in file_nums:
        meta = file_metadata(file_num)
        rows.append({
            "file_num": file_num, "company": meta["company"], "property": meta["property"],
            "era": meta["era"], "year": meta["year"], "nts_sheets": ",".join(meta["nts_sheets"]),
            "source_url": meta["source_url"], "pages": meta["pages"],
        })
    return pd.DataFrame(rows)


def _frames(log: Callable[[str], None] = print) -> dict[str, pd.DataFrame]:
    reports, pdfs, pages, tables, records, field_values = [], [], [], [], [], []
    validator_results, crs_results, crosschecks = [], [], []
    files = list(assembled_files())
    for file_num in files:
        doc = read_assembled(file_num)
        pos = read_positions(file_num)
        cross = read_crosscheck(file_num)
        meta = file_metadata(file_num)
        # our own counts only; the file's provincial metadata lives in native.file_index
        reports.append({
            "file_num": file_num, "split": meta["split"], "file_sha256": doc.get("file_sha256"),
            "holes": len(doc.get("holes", [])), "tables": len(doc.get("tables", [])),
            "values": len(doc.get("values", {})), "assembled_at": doc.get("assembled_at"),
        })
        for sha in sorted({p["pdf_sha256"] for p in doc.get("pages", [])}):
            pdfs.append({"file_num": file_num, "pdf_sha256": sha,
                         "pages_called": sum(1 for p in doc["pages"] if p["pdf_sha256"] == sha)})
        for p in doc.get("pages", []):
            pages.append({"file_num": file_num, "page_id": p["page_id"], "pdf_sha256": p["pdf_sha256"],
                          "page": p["page"], "page_kind": p.get("page_kind"),
                          "route_class": p.get("route_class"),
                          "coordinate_kind": p.get("coordinate_kind"),
                          "text_layer_words": p.get("text_layer_words"),
                          "legibility_notes": p.get("legibility_notes")})
        for t in doc.get("tables", []):
            tables.append({"file_num": file_num, "table_id": t["table_id"], "page": t["page"],
                           "kind": t.get("kind"), "rows_stored": t.get("rows_stored"),
                           "rows_printed": t.get("rows_printed"), "truncated": t.get("truncated"),
                           "continues_from": t.get("continues_from"),
                           "continued_from_prev": t.get("continued_from_prev"),
                           "continues_on_next": t.get("continues_on_next"),
                           "chain_id": t.get("chain_id"), "route_class": t.get("route_class"),
                           "bbox": json.dumps(t.get("bbox"))})
        for hole in doc.get("holes", []):
            records.append({"file_num": file_num, "hole_id": hole["hole_id"],
                            "name_as_printed": hole.get("name_as_printed"),
                            "hole_id_source": hole.get("hole_id_source"), "status": hole.get("status"),
                            "pages": ",".join(str(p) for p in hole.get("pages", [])),
                            "kind": "collar", "n_lith": len(hole.get("lith", [])),
                            "n_assays": len(hole.get("assays", []))})
            for group in ("lith", "assays"):
                for i in hole.get(group, []):
                    records.append({
                        "file_num": file_num, "hole_id": hole["hole_id"], "name_as_printed": None,
                        "hole_id_source": None, "status": i.get("status"), "pages": str(i.get("page")),
                        "kind": "lith_interval" if group == "lith" else "assay_interval",
                        "n_lith": None, "n_assays": None,
                        "interval_id": i.get("id"), "table_id": i.get("table_id"), "row": i.get("row"),
                        "from_value": i.get("from_value"), "to_value": i.get("to_value"),
                        "from_m": i.get("from_m_value"), "to_m": i.get("to_m_value"),
                        "depth_unit": i.get("depth_unit"),
                        "depth_unit_as_printed": i.get("depth_unit_as_printed"),
                        "n_grades": len(i.get("grades", [])) if group == "assays" else None,
                    })
        meta_all = doc.get("value_meta", {})
        for vid, v in doc.get("values", {}).items():
            m = meta_all.get(vid, {})
            lin = v.get("lineage") or {}
            locate = m.get("locate") or {}
            field_values.append({
                "file_num": file_num, "value_id": vid, "kind": v.get("kind"), "field": m.get("field"),
                "scope": m.get("scope"), "hole_id": m.get("hole_id"), "table_id": m.get("table_id"),
                "row": m.get("row"), "as_printed": v.get("as_printed"),
                "value_num": v["value"] if isinstance(v.get("value"), (int, float)) else None,
                "value_text": v["value"] if isinstance(v.get("value"), str) else None,
                "unit_as_printed": v.get("unit_as_printed"), "unit_norm": m.get("unit_norm"),
                "unit_source": lin.get("unit_source"), "printed": m.get("printed"),
                "uncertain": m.get("uncertain"), "status": v.get("status"),
                "page": lin.get("page"), "quote": lin.get("quote"),
                "quote_located": lin.get("quote_located"),
                "bbox": json.dumps(lin.get("bbox")) if lin.get("bbox") else None,
                "locate_method": locate.get("locate_method"), "locate_score": locate.get("locate_score"),
                "digit_agreement": m.get("digit_agreement"),
                "locate_ambiguous": locate.get("locate_ambiguous"),
                "qualifier": m.get("qualifier"), "below_detection": m.get("below_detection"),
                "model": lin.get("model"), "prompt_version": lin.get("prompt_version"),
                "run_id": lin.get("run_id"),
                "n_validators": len(lin.get("validators") or []),
            })
        for f in (doc.get("validators", {}) or {}).get("findings", []):
            validator_results.append({
                "file_num": file_num, "validator": f["validator"], "version": f["version"],
                "outcome": f["outcome"], "severity": f["severity"], "class_a": f["class_a"],
                "scope": f["scope"], "scope_id": f["scope_id"], "message": f["message"],
                "n_values": len(f["value_ids"]), "value_ids": ",".join(f["value_ids"][:20]),
                "details": json.dumps(f["details"])[:4000]})
        for h in pos.get("holes", []):
            crs_results.append({
                "file_num": file_num, "hole_id": h["hole_id"], "status": h["status"],
                "position_source": h["position_source"], "datum_printed": h.get("datum_printed"),
                "datum": h.get("datum"), "zone": h.get("zone"), "zone_inferred": h.get("zone_inferred"),
                "lon": (h.get("lonlat") or [None, None])[0], "lat": (h.get("lonlat") or [None, None])[1],
                "misread_lon": (h.get("misread_lonlat") or [None, None])[0],
                "misread_lat": (h.get("misread_lonlat") or [None, None])[1],
                "alt_lon": (h.get("alt_lonlat") or [None, None])[0],
                "alt_lat": (h.get("alt_lonlat") or [None, None])[1],
                "shift_m": h.get("shift_m"), "bearing_deg": h.get("bearing_deg"),
                "operation": (h.get("transform") or {}).get("name"),
                "grid_sha256": (h.get("transform") or {}).get("grid_sha256"),
                "inside_nts": (h.get("checks") or {}).get("inside_nts"),
                "distance_to_file_holes_km": (h.get("checks") or {}).get("distance_to_file_holes_km"),
                "notes": " | ".join(h.get("notes", []))})
        for m in cross.get("matches", []):
            crosschecks.append({
                "file_num": file_num, "hole_id": m["hole_id"], "dataset": m["dataset"],
                "feature_id": m["feature_id"], "provincial_name": m.get("provincial_name"),
                "lon": (m.get("lonlat") or [None, None])[0], "lat": (m.get("lonlat") or [None, None])[1],
                "offset_m": m.get("offset_m_value"), "offset_independent": m.get("offset_independent"),
                "position_source": m.get("position_source"), "name_match": m.get("name_match"),
                "name_score": m.get("name_score"),
                "datum_shift_signature": m.get("datum_shift_signature"),
                "adjudication": m.get("adjudication"),
                "d_total_depth_m": (m.get("differences") or {}).get("total_depth_m"),
                "d_dip_deg": (m.get("differences") or {}).get("dip_deg"),
                "d_azimuth_deg": (m.get("differences") or {}).get("azimuth_deg")})
    return {
        "layer": _native_layers(), "file_index": _native_file_index(files),
        "report": pd.DataFrame(reports), "pdf": pd.DataFrame(pdfs), "page": pd.DataFrame(pages),
        "table": pd.DataFrame(tables), "record": pd.DataFrame(records),
        "field_value": pd.DataFrame(field_values),
        "validator_outcome": pd.DataFrame(validator_results),
        "collar_position": pd.DataFrame(crs_results), "crosscheck": pd.DataFrame(crosschecks),
    }


CROSS_TIER_VIEWS = """
create or replace view read.v_report as
  select 'native+read' as tiers, r.file_num, i.company, i.property, i.era, i.year, i.nts_sheets,
         i.source_url, i.pages, r.split, r.file_sha256, r.holes, r.tables, r.values, r.assembled_at
  from read.report r left join native.file_index i using (file_num);
"""


def rebuild(log: Callable[[str], None] = print) -> dict[str, Any]:
    """Drop and rewrite every tiered table from the stage outputs, then audit the tiers."""
    frames = _frames(log=log)
    con = connect()
    try:
        counts: dict[str, int] = {}
        for name, df in frames.items():
            schema, table = PLACEMENT[name]
            tier = schema
            if df.empty:
                con.execute(f"create schema if not exists {schema}")
                # an empty stage is not an error: keep the shape addressable, with the tier still declared
                con.execute(f"create or replace table {schema}.{table} (tier text)")
                counts[f"{schema}.{table}"] = 0
                continue
            counts[f"{schema}.{table}"] = insert_frame(con, schema, table, df, tier)
        con.execute(CROSS_TIER_VIEWS)
        write_meta(con, __version__)
        problems = tier_audit(con)
    finally:
        con.close()
    if problems:
        raise RuntimeError("store refused: tier audit failed\n  " + "\n  ".join(problems))
    out: dict[str, Any] = {"db": str(db_relative()), "tables": counts}
    out.update(_write_geoparquet(frames, log=log))
    log(f"  {db_relative()}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    log(f"  tier audit clean across {len(counts)} tables")
    return out


def db_relative() -> str:
    from . import db_path

    return str(db_path().relative_to(PATHS.pipeline))


def _write_geoparquet(frames: dict[str, pd.DataFrame], log: Callable[[str], None] = print) -> dict[str, Any]:
    import geopandas as gpd

    out: dict[str, Any] = {}
    collars = frames["collar_position"]
    if not collars.empty:
        plottable = collars.dropna(subset=["lon", "lat"])
        gdf = gpd.GeoDataFrame(plottable.copy(),
                               geometry=gpd.points_from_xy(plottable["lon"], plottable["lat"]),
                               crs="EPSG:4326")
        p = PATHS.out / "collars.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_parquet(p, index=False)
        out["collars_parquet"] = {"path": str(p.relative_to(PATHS.pipeline)), "rows": len(gdf)}
        log(f"  collars.parquet: {len(gdf)} plottable collars of {len(collars)}")
    cc = frames["crosscheck"]
    if not cc.empty:
        valid = cc.dropna(subset=["lon", "lat"])
        gdf = gpd.GeoDataFrame(valid.copy(), geometry=gpd.points_from_xy(valid["lon"], valid["lat"]),
                               crs="EPSG:4326")
        p = PATHS.out / "crosscheck.parquet"
        gdf.to_parquet(p, index=False)
        out["crosscheck_parquet"] = {"path": str(p.relative_to(PATHS.pipeline)), "rows": len(gdf)}
        log(f"  crosscheck.parquet: {len(gdf)} matched provincial records")
    return out


#: kept so `lr run phase2` and older call sites do not change meaning
stage_store = rebuild

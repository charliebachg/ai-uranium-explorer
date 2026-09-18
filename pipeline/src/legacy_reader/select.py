"""`lr select shortlist | probe | final`: choose the 12 assessment files, split them, pick the Phase 1 four.

Why it works this way (research/05 sections 5 and 6, plan "Selection"):
- Era comes from WORK_DATE (first year) or the linked GeoDS holes' drilling dates, never from layer 20's
  WRK_PERIOD (free text, 1,262 of 5,822 unparseable).
- Only facts already on disk in data/index are used to shortlist; the file-link table (GeoDS table 23) is
  queried for the shortlist only, paced and cached.
- Datum and scan kind cannot be known before download, so they are checked after fetch and reported as
  shortfalls with suggested swaps, instead of downloading every candidate.
- Every step is deterministic: ties break on a published hash of the file number, and selection.json has
  no timestamps, so a re-run on the same inputs writes the same bytes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .filenum import display_file_num, norm_file_num, nts_from_file_num, parse_file_nums
from .ids import canonical_json, sha256_bytes, sha256_file
from .nts import parse_nts_list, point_in_sheet
from .paths import PATHS
from .worktext import first_year, join_work_fields, parse_work_text

SELECTION_VERSION = "selection/v1"
SPLIT_SEED = "legacy-reader-split-v1:"
TABLE23 = ("https://geoscience-data-system.saskatchewan.ca/arcgis/rest/services/"
           "P_GeoDS_AssessmentPage_EM/FeatureServer/23")
TABLE23_FIELDS = "ASSMNT_FILE_NUM,FILE_NAME,FILE_SIZE,FILE_LNK,FILE_LCATN,SYSTEM_ASSMNT_FILE_NUM"

ERAS = ("1970s", "1980s-1990s", "2000s+")
ZONE12_WEST_OF = -108.0  # UTM zone 12 spans 114 W to 108 W

PARAMS: dict[str, Any] = {
    "per_era_final": 4,
    "max_per_company": 2,
    "min_zone12": 2,
    "min_scanned": 4,
    "min_nad27_or_no_datum": 3,
    "min_provincial_holes": 5,
    "hole_count_range": [5, 20],
    "max_folder_mb": 400.0,
    "max_report_total_mb": 150.0,
    "max_report_single_mb": 60.0,
    "shortlist_per_era": 10,
    "shortlist_min_zone12_per_era": 2,
    "probe_min_pass_per_era": 6,
    "probe_extend_step": 5,
    "probe_max_per_era": 20,
    "heldout_per_era": 1,
    "heldout_extra_for_datum": 1,
    "datum_likely_threshold": 2.0,
}

SCORE_WEIGHTS = {
    "xls_assays": 3.0,
    "certificates": 1.0,
    "provincial_lithology": 2.0,
    "nad27_or_no_datum": 2.0,
    "probed": 1.0,
}


def selection_path() -> Path:
    return PATHS.index / "selection.json"


def split_hash(file_num: str) -> str:
    return sha256_bytes((SPLIT_SEED + norm_file_num(file_num)).encode("utf-8"))


def era_for_year(year: int | None) -> tuple[str | None, bool]:
    """(era bucket, pre1968). Files before 1968 fall into the 1970s bucket but rank after 1968+ files."""
    if year is None:
        return None, False
    if year < 1968:
        return "1970s", True
    if year < 1980:
        return "1970s", False
    if year < 2000:
        return "1980s-1990s", False
    return "2000s+", False


def company_key(name: str | None) -> str:
    """A coarse company family key for the per-company cap.

    "ASAMERA OIL CORPORATION LTD" and "ASAMERA INC" -> "ASAMERA"; "AMOK LTD-MOKTA CANADA LTD" -> "AMOK".
    Joint ventures count against the first-named company."""
    s = (name or "").upper()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.split(r"[,;/]| - |-(?=[A-Z])", s)[0]
    words = [w for w in re.findall(r"[A-Z0-9&]+", s)
             if w not in {"LTD", "LIMITED", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "THE",
                          "OF", "LTEE", "LIMITEE"}]
    if not words:
        return "UNKNOWN"
    # Take words until the key has at least four letters, so initials stay together ("E & B").
    key: list[str] = []
    for w in words[:3]:
        key.append(w)
        if sum(len(k) for k in key if k.isalnum()) >= 4:
            break
    return " ".join(key)


def _hole_key(name: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def _clean_company(name: str | None) -> str:
    return re.sub(r"\s*\(Tenure Holder\)\s*", "", name or "").strip()


# ---------------------------------------------------------------- features from the index


@dataclass
class IndexData:
    files: list[dict[str, Any]]
    work: dict[str, list[dict[str, Any]]]
    geods: dict[str, list[dict[str, Any]]]
    compilation: dict[str, list[dict[str, Any]]]
    sources: dict[str, str]


def load_index(index_dir: Path | None = None) -> IndexData:
    index_dir = index_dir or PATHS.index

    def feats(name: str) -> list[dict[str, Any]]:
        return json.loads((index_dir / name).read_text())["features"]

    files = feats("file_index_uranium.json")
    work: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for layer in (0, 1, 2):
        for rec in feats(f"assessment_info_{layer}.json"):
            if rec.get("FILENUMBER"):
                work[norm_file_num(rec["FILENUMBER"])].append({**rec, "_layer": layer})
    geods: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in feats("geods_holes.geojson"):
        p = f["properties"]
        if p.get("TEMP_ASSMNT_FILE_NUM"):
            geods[norm_file_num(p["TEMP_ASSMNT_FILE_NUM"])].append(p)
    compilation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in feats("compilation.geojson"):
        p = f["properties"]
        lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])[:2]
        for n in parse_file_nums(p.get("SOURCE")):
            compilation[n].append({**p, "_lon": lon, "_lat": lat})
    names = ["file_index_uranium.json", "assessment_info_0.json", "assessment_info_1.json",
             "assessment_info_2.json", "geods_holes.geojson", "compilation.geojson"]
    sources = {n: sha256_file(index_dir / n) for n in names}
    return IndexData(files=files, work=dict(work), geods=dict(geods), compilation=dict(compilation), sources=sources)


def _year_from_epoch_ms(ms: float | None) -> int | None:
    if ms is None:
        return None
    return (dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(milliseconds=ms)).year


def file_features(rec: dict[str, Any], idx: IndexData) -> dict[str, Any]:
    """Every index-derived feature of one uranium-tagged file (pure given the index)."""
    sys_num = norm_file_num(rec.get("SYSTEM_ASSMNT_FILE_NUM") or rec["ASSMNT_FILE_NUM"])
    work_recs = sorted(idx.work.get(sys_num, []), key=lambda r: (r["_layer"], r.get("OBJECTID") or 0))
    work_items = []
    for r in work_recs:
        text = join_work_fields([r.get("WORK_1"), r.get("WORK_2"), r.get("WORK_3"), r.get("WORK_4")])
        wt = parse_work_text(text)
        work_items.append({"layer": r["_layer"], "work_date": r.get("WORK_DATE"), "year": first_year(r.get("WORK_DATE")),
                           "company": r.get("COMPANY"), "area": r.get("AREA_"), "text": text, "parsed": wt})
    desc = parse_work_text(rec.get("HSTRC_ASSMNT_WRK_DESCRPTN"))

    # era: WORK_DATE first year (records that mention drilling first), else median GeoDS drilling year
    drill_years = [w["year"] for w in work_items if w["year"] and w["parsed"].drilling]
    any_years = [w["year"] for w in work_items if w["year"]]
    holes = idx.geods.get(sys_num, [])
    hole_years = sorted(y for y in (_year_from_epoch_ms(h.get("DRILLNG_START_DATE")) for h in holes) if y)
    if drill_years:
        year, era_source = min(drill_years), "WORK_DATE (drilling record)"
    elif any_years:
        year, era_source = min(any_years), "WORK_DATE"
    elif hole_years:
        year, era_source = int(statistics.median_low(hole_years)), "median GeoDS DRILLNG_START_DATE"
    else:
        year, era_source = None, None
    era, pre1968 = era_for_year(year)

    counts = [w["parsed"].hole_count for w in work_items if w["parsed"].hole_count]
    work_count = max(counts) if counts else None
    count_source = "WORK text" if work_count else None
    if work_count is None and desc.hole_count:
        work_count, count_source = desc.hole_count, "layer 20 description"
    work_names: list[str] = []
    for w in [*(wi["parsed"] for wi in work_items), desc]:
        for n in w.hole_names:
            if n not in work_names:
                work_names.append(n)
    metres = next((w["parsed"].metres for w in work_items if w["parsed"].metres), None) or desc.metres
    feet = next((w["parsed"].feet for w in work_items if w["parsed"].feet), None) or desc.feet

    geods_names = sorted({_hole_key(h.get("HOLE_NAME")) for h in holes if h.get("HOLE_NAME")})
    lons = [h["LON_DEG"] for h in holes if h.get("LON_DEG") is not None]
    lats = [h["LAT_DEG"] for h in holes if h.get("LAT_DEG") is not None]
    comp = idx.compilation.get(sys_num, [])
    comp_names = sorted({_hole_key(h.get("DRILLHOLE_NAME")) for h in comp if h.get("DRILLHOLE_NAME")})
    comp_lons = [h["_lon"] for h in comp if h.get("_lon") is not None]
    work_keys = {_hole_key(n) for n in work_names}
    nts_sheets = parse_nts_list(rec.get("NTS_SHEET"))
    own_sheet = nts_from_file_num(sys_num)
    in_sheets = None
    if nts_sheets and lons:
        inside = sum(1 for h in holes if h.get("LON_DEG") is not None and
                     any(point_in_sheet(h["LON_DEG"], h["LAT_DEG"], s, tol_deg=0.02) for s in nts_sheets))
        in_sheets = round(inside / len(lons), 3)
    all_text = " ".join([*(w["text"] for w in work_items), rec.get("HSTRC_ASSMNT_WRK_DESCRPTN") or ""])

    parsed_all = [wi["parsed"] for wi in work_items] + [desc]
    return {
        "file_num": sys_num,
        "file_num_geods": rec["ASSMNT_FILE_NUM"],
        "display_file_num": display_file_num(sys_num),
        "split_hash": split_hash(sys_num),
        "company": _clean_company(rec.get("CMPNY")),
        "company_key": company_key(_clean_company(rec.get("CMPNY"))),
        "property": rec.get("PROPRTY_PROJCT"),
        "commodity": rec.get("CMMDTY"),
        "uranium_tagged": "ranium" in (rec.get("CMMDTY") or ""),
        "folder_size_mb": rec.get("FILE_SIZE"),
        "nts_sheets": nts_sheets,
        "nts_from_file_num": own_sheet,
        "wrk_period_ignored": rec.get("WRK_PERIOD"),
        "year": year,
        "era": era,
        "era_source": era_source,
        "pre1968": pre1968,
        "work_records": [{"layer": w["layer"], "work_date": w["work_date"], "company": w["company"],
                          "area": w["area"], "text": w["text"]} for w in work_items],
        "description": rec.get("HSTRC_ASSMNT_WRK_DESCRPTN"),
        "drilling_evidence": any(p.drilling for p in parsed_all) or bool(re.search(r"rill", all_text, re.I)),
        "work_hole_count": work_count,
        "work_hole_count_source": count_source,
        "work_percussion_count": next((p.percussion_count for p in parsed_all if p.percussion_count), None),
        "work_metres": metres,
        "work_feet": feet,
        "work_hole_names": work_names,
        "probed": any(p.probed for p in parsed_all),
        "assay_mentioned": any(p.assay for p in parsed_all),
        "u3o8_mentioned": any(p.u3o8 for p in parsed_all),
        "geochem_mentioned": any(p.geochem for p in parsed_all),
        "grid_mentioned": bool(re.search(r"\bgrids?\b", all_text, re.I)),
        "geods_hole_records": len(holes),
        "geods_holes": len(geods_names),
        "geods_hole_names": geods_names,
        "geods_litho_obs": sum(1 for h in holes if h.get("LITHO_OBS") == "Yes"),
        "geods_original_latlon": sum(1 for h in holes if h.get("ORIGNL_LAT_DEG") is not None
                                     and h.get("ORIGNL_LON_DEG") is not None),
        "geods_years": sorted(set(hole_years)),
        "geods_bbox": [round(min(lons), 5), round(min(lats), 5), round(max(lons), 5), round(max(lats), 5)] if lons else None,
        "geods_in_nts_share": in_sheets,
        "compilation_holes": len(comp_names),
        "compilation_hole_names": comp_names,
        "work_names_matched_geods": len(work_keys & set(geods_names)),
        "zone12": any(lon < ZONE12_WEST_OF for lon in [*lons, *comp_lons]),
        "provincial_holes": max(len(geods_names), len(comp_names)),
        "hole_count_for_filter": work_count if work_count else len(geods_names),
    }


def prefetch_filters(f: dict[str, Any], params: dict[str, Any] = PARAMS) -> dict[str, bool]:
    lo, hi = params["hole_count_range"]
    return {
        "uranium_tagged": bool(f["uranium_tagged"]),
        "era_known": f["era"] is not None,
        "drilling_evidence": bool(f["drilling_evidence"]),
        "provincial_holes_min": f["provincial_holes"] >= params["min_provincial_holes"],
        "hole_count_range": lo <= (f["hole_count_for_filter"] or 0) <= hi,
        "folder_size_max": (f["folder_size_mb"] or 0) <= params["max_folder_mb"],
    }


def score_components(f: dict[str, Any], probe: dict[str, Any] | None = None,
                     datum: dict[str, Any] | None = None) -> dict[str, float]:
    """The plan's soft score. Components that are not known yet are omitted, not zeroed."""
    comp: dict[str, float] = {
        "provincial_lithology": SCORE_WEIGHTS["provincial_lithology"] if f["geods_litho_obs"] > 0 else 0.0,
        "probed": SCORE_WEIGHTS["probed"] if f["probed"] else 0.0,
    }
    if probe is not None:
        comp["xls_assays"] = SCORE_WEIGHTS["xls_assays"] if probe["assay_xls"] else 0.0
        comp["certificates"] = SCORE_WEIGHTS["certificates"] if probe["certificate_pdfs"] else 0.0
    if datum is not None and datum.get("confirmed"):
        comp["nad27_or_no_datum"] = SCORE_WEIGHTS["nad27_or_no_datum"] if datum["likely"] else 0.0
    return comp


def tiebreak_points(f: dict[str, Any]) -> int:
    """Secondary ranking inside equal scores: things that make a file a better test, not a better target."""
    pts = 0
    pts += 1 if (f["u3o8_mentioned"] or f["assay_mentioned"]) else 0
    wc, gh = f["work_hole_count"], f["geods_holes"]
    pts += 1 if (wc and abs(wc - gh) <= 2) else 0
    pts += 1 if (f["folder_size_mb"] or 0) <= PARAMS["max_report_total_mb"] else 0
    return pts


def rank_key(row: dict[str, Any]) -> tuple:
    return (-row["score"], row["features"]["pre1968"], -row["tiebreak"], row["features"]["folder_size_mb"] or 0,
            row["features"]["split_hash"])


def build_shortlist(idx: IndexData, params: dict[str, Any] = PARAMS) -> dict[str, Any]:
    feats = [file_features(r, idx) for r in idx.files]
    feats.sort(key=lambda f: f["file_num"])
    funnel: Counter[str] = Counter()
    passing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in feats:
        flt = prefetch_filters(f, params)
        funnel["uranium_tagged_files"] += 1
        ok = True
        for name, passed in flt.items():
            ok = ok and passed
            if ok:
                funnel[f"pass_{name}"] += 1
        if not ok:
            continue
        comps = score_components(f)
        row = {"file_num": f["file_num"], "era": f["era"], "features": f, "prefetch_filters": flt,
               "score_components": comps, "score": sum(comps.values()), "tiebreak": tiebreak_points(f)}
        passing[f["era"]].append(row)
    ranked: dict[str, list[dict[str, Any]]] = {}
    for era in ERAS:
        rows = sorted(passing.get(era, []), key=rank_key)
        for i, r in enumerate(rows, 1):
            r["era_rank"] = i
        ranked[era] = rows
    shortlist = []
    for era in ERAS:
        rows = ranked[era]
        chosen = rows[: params["shortlist_per_era"]]
        z12 = [r for r in chosen if r["features"]["zone12"]]
        for r in rows[params["shortlist_per_era"]:]:
            if len(z12) >= params["shortlist_min_zone12_per_era"]:
                break
            if r["features"]["zone12"]:
                chosen.append(r)
                z12.append(r)
                r["shortlist_reason"] = "added to reach the zone 12 minimum"
        for r in chosen:
            r.setdefault("shortlist_reason", f"top {params['shortlist_per_era']} in era by score")
        shortlist.extend(chosen)
    return {
        "funnel": dict(funnel),
        "passing_by_era": {era: len(ranked[era]) for era in ERAS},
        "pre1968_in_1970s_bucket": sum(1 for r in ranked["1970s"] if r["features"]["pre1968"]),
        "reserve": {era: [r["file_num"] for r in ranked[era]] for era in ERAS},
        "candidates": shortlist,
        "_ranked": ranked,
    }


# ---------------------------------------------------------------- probe (table 23)


_ADMIN_PDF = re.compile(r"declaration|cover\s*letter|statement of (?:costs|expend)|cert(?:ificate)?\.? of expend", re.I)
_APPENDIX_KEEP = re.compile(r"drill|ddh|\blogs?\b|geotic|core\s*log|assay|geochem|lithogeo|analys[ie]s|\bicp\b|u3o8|"
                            r"certif|collar|results", re.I)
_APPENDIX_SKIP = re.compile(r"strip\s*plot|figure|plan|section|spectr|pima|physical propert|calibration|manual|"
                            r"magnetic susc|resistivity|gravity|legend|key\b|code description", re.I)
_ASSAY_TABLE = re.compile(r"assay|geochem|lithogeo|\bicp\b|u3o8|appendix b", re.I)


def classify_listing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise a file's table 23 folder listing into what fetch needs and what scoring uses.

    Deviation from the plan's `FILE_LCATN LIKE '%Reports'` rule, found on the live listings (2026-09-18):
    historic scanned files keep their PDFs in a `Reports` folder, but most 2000s+ digital submissions have
    no `Reports` folder. Their main report sits directly in `Digital Submissions`, and drill logs and assay
    results sit in `Digital Submissions/Appendix ...` subfolders. So:
    - report_pdf: a PDF in a folder ending `Reports`, or directly in `Digital Submissions` (admin letters skipped);
    - appendix_pdf: a PDF in a `Digital Submissions` subfolder whose folder or name mentions drill logs,
      assays, geochemistry or results, and not figures, plans, sections, strip plots or instrument data;
    - certificate_pdf: any other PDF naming a certificate, or in an assay / Appendix B folder;
    - assay_xls: XLS/XLSX whose folder or name mentions assays, geochemistry or Appendix B.
    Report and appendix PDFs together are the "document PDFs" that the size filters and later stages use.
    """
    reports, appendices, assays, certs = [], [], [], []
    ext_counts: Counter[str] = Counter()
    total_mb = 0.0
    for r in sorted(rows, key=lambda r: ((r.get("FILE_LCATN") or ""), (r.get("FILE_NAME") or ""))):
        name = r.get("FILE_NAME") or ""
        folder = (r.get("FILE_LCATN") or "").rstrip("/")
        ext = Path(name).suffix.lower()
        ext_counts[ext or "(none)"] += 1
        size = float(r.get("FILE_SIZE") or 0.0)
        total_mb += size
        item = {"name": name, "folder": folder, "size_mb": round(size, 5), "url": r.get("FILE_LNK")}
        low_folder = folder.lower()
        tail = low_folder.split("digital submissions", 1)[1] if "digital submissions" in low_folder else None
        if ext == ".pdf" and low_folder.endswith("reports"):
            reports.append({**item, "kind": "report_pdf"})
        elif ext == ".pdf" and tail == "" and not _ADMIN_PDF.search(name):
            reports.append({**item, "kind": "report_pdf"})
        elif (ext == ".pdf" and tail and "appendi" in tail and _APPENDIX_KEEP.search(tail + " " + name)
              and not _APPENDIX_SKIP.search(tail + " " + name) and "certif" not in (tail + name.lower())):
            appendices.append({**item, "kind": "appendix_pdf"})
        elif ext in (".xls", ".xlsx") and _ASSAY_TABLE.search(low_folder + " " + name):
            assays.append({**item, "kind": "assay_xls"})
        elif ext == ".pdf" and ("certif" in (low_folder + name.lower()) or "appendix b" in low_folder
                                or "assay" in low_folder):
            certs.append({**item, "kind": "certificate_pdf"})
    docs = reports + appendices
    sizes = [d["size_mb"] for d in docs]
    return {
        "n_files": len(rows),
        "folder_total_mb": round(total_mb, 3),
        "extensions": dict(sorted(ext_counts.items())),
        "report_pdfs": reports,
        "appendix_pdfs": appendices,
        "n_report_pdfs": len(reports),
        "n_document_pdfs": len(docs),
        "report_total_mb": round(sum(sizes), 3),
        "report_max_mb": round(max(sizes), 3) if sizes else 0.0,
        "assay_xls": assays,
        "certificate_pdfs": certs,
    }


def postprobe_filters(probe: dict[str, Any], params: dict[str, Any] = PARAMS) -> dict[str, bool]:
    return {
        "has_report_pdf": probe["n_report_pdfs"] > 0,
        "report_total_max": probe["report_total_mb"] <= params["max_report_total_mb"],
        "report_single_max": probe["report_max_mb"] <= params["max_report_single_mb"],
    }


def probe_listing(client: Any, file_num_geods: str) -> list[dict[str, Any]]:
    # single-clause where (the WAF blocks some compound filters); cached by the client
    feats = client.fetch_all(TABLE23, where=f"ASSMNT_FILE_NUM='{file_num_geods}'", out_fields=TABLE23_FIELDS,
                             geometry=False, out_sr=None)
    return [f.get("attributes", {}) for f in feats]


def run_probe(sel: dict[str, Any], lister: Callable[[str], list[dict[str, Any]]], idx: IndexData | None = None,
              params: dict[str, Any] = PARAMS, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Probe shortlisted files; extend an era down its ranked reserve if too few pass the size filters."""
    shortlist = sel["shortlist"]
    # Recomputed from the (cached) listings every time, so a rule change reclassifies everything.
    probes: dict[str, Any] = {}
    extra: dict[str, dict[str, Any]] = {}
    extended: dict[str, list[str]] = defaultdict(list)

    def probe_one(row: dict[str, Any]) -> dict[str, Any]:
        f = row["features"]
        rows = lister(f["file_num_geods"])
        summary = classify_listing(rows)
        flt = postprobe_filters(summary, params)
        comps = score_components(f, probe=summary)
        rec = {**summary, "postprobe_filters": flt, "passes": all(flt.values()),
               "score_components": comps, "score": sum(comps.values())}
        log(f"  {f['file_num']:<12} {row['era']:<12} reports {summary['n_report_pdfs']:>2}+{len(summary['appendix_pdfs']):<2} "
            f"{summary['report_total_mb']:>7.1f} MB (max {summary['report_max_mb']:.1f}) xls {len(summary['assay_xls'])} "
            f"certs {len(summary['certificate_pdfs'])} -> {'pass' if rec['passes'] else 'fail'}")
        return rec

    for era in ERAS:
        era_rows = [c for c in shortlist["candidates"] if c["era"] == era]
        for row in era_rows:
            if row["file_num"] not in probes:
                probes[row["file_num"]] = probe_one(row)
        reserve = shortlist["reserve"][era]
        probed_in_era = [n for n in reserve if n in probes]
        passing = [n for n in probed_in_era if probes[n]["passes"]]
        while len(passing) < params["probe_min_pass_per_era"] and len(probed_in_era) < params["probe_max_per_era"]:
            nxt = [n for n in reserve if n not in probes][: params["probe_extend_step"]]
            if not nxt:
                break
            if idx is None:
                raise RuntimeError("extending the shortlist needs the index")
            for n in nxt:
                if n not in extra:
                    rec = next(r for r in idx.files
                               if norm_file_num(r.get("SYSTEM_ASSMNT_FILE_NUM") or r["ASSMNT_FILE_NUM"]) == n)
                    f = file_features(rec, idx)
                    comps = score_components(f)
                    extra[n] = {"file_num": n, "era": era, "features": f, "prefetch_filters": prefetch_filters(f, params),
                                "score_components": comps, "score": sum(comps.values()),
                                "tiebreak": tiebreak_points(f), "era_rank": reserve.index(n) + 1,
                                "shortlist_reason": "probe extension: too few earlier candidates passed the size filters"}
                probes[n] = probe_one(extra[n])
                extended[era].append(n)
            probed_in_era = [n for n in reserve if n in probes]
            passing = [n for n in probed_in_era if probes[n]["passes"]]
    cands = [*shortlist["candidates"], *extra.values()]
    return {"files": dict(sorted(probes.items())), "extended": dict(extended),
            "extra_candidates": sorted(extra.values(), key=lambda c: (ERAS.index(c["era"]), c["era_rank"])),
            "passing_by_era": {era: sum(1 for c in cands if c["era"] == era
                                        and probes.get(c["file_num"], {}).get("passes")) for era in ERAS}}


def all_candidates(sel: dict[str, Any]) -> list[dict[str, Any]]:
    """Shortlist rows plus rows the probe added when an era ran short."""
    return [*sel["shortlist"]["candidates"], *sel.get("probe", {}).get("extra_candidates", [])]


# ---------------------------------------------------------------- datum likelihood


def datum_signal(f: dict[str, Any], text_hits: dict[str, int] | None = None,
                 ocr_hits: dict[str, int] | None = None) -> dict[str, Any]:
    """How likely a file prints NAD27, a local grid, or no datum. Best available signal, with reasons.

    Before download the only evidence is the era and the work text; after download the text layer (and
    later OCR) can be searched for datum strings. Scores are a ranking device, not a measurement."""
    score = 0.0
    reasons: list[str] = []
    year = f.get("year")
    if year is not None and year < 1980:
        score += 2.0
        reasons.append("pre-1980 work: local grids or no printed datum are common")
    elif year is not None and year < 1990:
        score += 1.0
        reasons.append("1980s work: NAD27 UTM or local grids are common")
    elif year is not None and year < 2000:
        score += 0.5
        reasons.append("1990s work: NAD27 still common")
    if f.get("grid_mentioned"):
        score += 0.5
        reasons.append("work text mentions a grid")
    confirmed = False
    for label, hits in (("text layer", text_hits), ("OCR", ocr_hits)):
        if not hits:
            continue
        if hits.get("nad27", 0):
            score += 3.0
            confirmed = True
            reasons.append(f"{label}: {hits['nad27']} NAD27 mentions")
        if hits.get("local_grid", 0) >= 3:
            score += 2.0
            confirmed = True
            reasons.append(f"{label}: {hits['local_grid']} local-grid station coordinates")
        if hits.get("nad83", 0) and not hits.get("nad27", 0):
            score -= 2.0
            confirmed = True
            reasons.append(f"{label}: NAD83 printed and NAD27 not")
    likely = score >= PARAMS["datum_likely_threshold"]
    return {"score": round(score, 2), "likely": likely, "confirmed": confirmed, "reasons": reasons}


# ---------------------------------------------------------------- final pick (pure)


@dataclass
class ConstraintResult:
    name: str
    required: str
    achieved: Any
    met: bool | None  # None: cannot be checked yet
    note: str = ""


def solve_final(candidates: list[dict[str, Any]], params: dict[str, Any] = PARAMS) -> dict[str, Any]:
    """Greedy pick: 4 per era, at most 2 per company, at least 2 with holes in UTM zone 12.

    `candidates` rows need: file_num, era, score, company_key, zone12, split_hash, pre1968, tiebreak.
    Deterministic for a given candidate set (order of input rows does not matter)."""
    per_era = params["per_era_final"]
    cap = params["max_per_company"]
    rows = sorted(candidates, key=lambda r: (-r["score"], r.get("pre1968", False), -r.get("tiebreak", 0),
                                            r["split_hash"]))
    chosen: list[dict[str, Any]] = []
    reasons: dict[str, list[str]] = defaultdict(list)
    skipped: dict[str, str] = {}
    era_n: Counter[str] = Counter()
    comp_n: Counter[str] = Counter()
    for r in rows:
        if r["era"] not in ERAS:
            continue
        if era_n[r["era"]] >= per_era:
            skipped.setdefault(r["file_num"], f"era {r['era']} already has {per_era}")
            continue
        if comp_n[r["company_key"]] >= cap:
            skipped[r["file_num"]] = f"company cap: {r['company_key']} already has {cap}"
            continue
        chosen.append(r)
        era_n[r["era"]] += 1
        comp_n[r["company_key"]] += 1
        reasons[r["file_num"]].append(
            f"rank {era_n[r['era']]} in {r['era']} by score {r['score']:g} (ties: pre-1968 last, tiebreak, hash)")

    # zone 12 repair: swap the lowest-ranked non-zone-12 pick of the same era for the best zone-12 reserve
    swaps: list[dict[str, str]] = []
    order = {r["file_num"]: i for i, r in enumerate(rows)}
    for r in rows:
        if sum(1 for c in chosen if c["zone12"]) >= params["min_zone12"]:
            break
        if not r["zone12"] or r in chosen or r["era"] not in ERAS:
            continue
        victims = sorted((c for c in chosen if c["era"] == r["era"] and not c["zone12"]),
                         key=lambda c: -order[c["file_num"]])
        for v in victims:
            comp_after = comp_n[r["company_key"]] - (1 if v["company_key"] == r["company_key"] else 0)
            if comp_after >= cap:
                continue
            chosen.remove(v)
            chosen.append(r)
            comp_n[v["company_key"]] -= 1
            comp_n[r["company_key"]] += 1
            reasons.pop(v["file_num"], None)
            skipped[v["file_num"]] = f"swapped out for {r['file_num']} to reach {params['min_zone12']} zone 12 files"
            reasons[r["file_num"]].append(f"swapped in for {v['file_num']}: holes in UTM zone 12")
            swaps.append({"in": r["file_num"], "out": v["file_num"], "why": "zone 12 minimum"})
            break

    for c in chosen:
        if c["zone12"]:
            reasons[c["file_num"]].append("holes in UTM zone 12 (west of 108 W)")
    chosen.sort(key=lambda r: (ERAS.index(r["era"]), order[r["file_num"]]))
    era_counts = {e: sum(1 for c in chosen if c["era"] == e) for e in ERAS}
    comp_counts = Counter(c["company_key"] for c in chosen)
    z12 = sum(1 for c in chosen if c["zone12"])
    constraints = [
        ConstraintResult("per_era", f"{per_era} per era", era_counts, all(v == per_era for v in era_counts.values())),
        ConstraintResult("max_per_company", f"at most {cap} per company", dict(sorted(comp_counts.items())),
                         max(comp_counts.values(), default=0) <= cap),
        ConstraintResult("min_zone12", f"at least {params['min_zone12']} with holes in UTM zone 12", z12,
                         z12 >= params["min_zone12"],
                         "" if z12 >= params["min_zone12"] else "not enough zone 12 candidates passed the filters"),
        ConstraintResult("total", f"{per_era * len(ERAS)} files", len(chosen), len(chosen) == per_era * len(ERAS)),
        ConstraintResult("min_scanned", f"at least {params['min_scanned']} scanned", None, None,
                         "checked after fetch (pdfprobe)"),
        ConstraintResult("min_nad27_or_no_datum", f"at least {params['min_nad27_or_no_datum']} NAD27 or no printed datum",
                         None, None, "checked after fetch (text layer), then OCR"),
    ]
    return {
        "selected": [c["file_num"] for c in chosen],
        "reasons": {k: reasons[k] for k in sorted(reasons) if k in {c["file_num"] for c in chosen}},
        "skipped": dict(sorted(skipped.items())),
        "swaps": swaps,
        "constraints": [asdict(c) for c in constraints],
        "unmet": [c.name for c in constraints if c.met is False],
    }


def split_files(selected: list[dict[str, Any]], likely_datum: dict[str, bool],
                params: dict[str, Any] = PARAMS) -> dict[str, Any]:
    """Held-out rule: lowest split hash per era, plus the lowest remaining hash that keeps at least one
    likely NAD27-or-no-datum file held out. `selected` rows need file_num, era, split_hash."""
    rows = sorted(selected, key=lambda r: r["split_hash"])
    held: list[str] = []
    why: dict[str, str] = {}
    for era in ERAS:
        era_rows = [r for r in rows if r["era"] == era]
        for r in era_rows[: params["heldout_per_era"]]:
            held.append(r["file_num"])
            why[r["file_num"]] = f"lowest split hash in {era}"
    remaining = [r for r in rows if r["file_num"] not in held]
    extra_note = ""
    for _ in range(params["heldout_extra_for_datum"]):
        if any(likely_datum.get(n) for n in held):
            pick = remaining[0] if remaining else None
            reason = "lowest remaining split hash (held-out already has a likely NAD27/no-datum file)"
        else:
            pick = next((r for r in remaining if likely_datum.get(r["file_num"])), None)
            reason = "lowest remaining split hash among likely NAD27/no-datum files"
            if pick is None and remaining:
                pick = remaining[0]
                reason = "lowest remaining split hash (no likely NAD27/no-datum file available)"
                extra_note = "no likely NAD27-or-no-datum file could be held out"
        if pick is not None:
            held.append(pick["file_num"])
            why[pick["file_num"]] = reason
            remaining = [r for r in remaining if r["file_num"] != pick["file_num"]]
    return {
        "seed": SPLIT_SEED,
        "heldout": sorted(held, key=lambda n: next(r["split_hash"] for r in rows if r["file_num"] == n)),
        "dev": [r["file_num"] for r in rows if r["file_num"] not in held],
        "reasons": why,
        "heldout_has_likely_datum": any(likely_datum.get(n) for n in held),
        "note": extra_note,
    }


def choose_phase1(selected: list[dict[str, Any]], dev: list[str], datum: dict[str, dict[str, Any]],
                  scanned: dict[str, bool] | None = None) -> dict[str, Any]:
    """One dev file per era (best score, then smaller reports, then hash), plus the dev file with the
    strongest NAD27/local-grid/no-datum signal among the rest. Rows need file_num, era, score,
    report_total_mb, split_hash."""
    scanned = scanned or {}
    pool = [r for r in selected if r["file_num"] in set(dev)]
    picks: list[str] = []
    why: dict[str, str] = {}
    for era in ERAS:
        era_rows = sorted((r for r in pool if r["era"] == era),
                          key=lambda r: (-r["score"], r.get("report_total_mb") or 0, r["split_hash"]))
        if era_rows:
            picks.append(era_rows[0]["file_num"])
            why[era_rows[0]["file_num"]] = f"best dev file in {era} (score, then smaller reports, then hash)"
    rest = sorted((r for r in pool if r["file_num"] not in picks),
                  key=lambda r: (-datum.get(r["file_num"], {}).get("score", 0), not scanned.get(r["file_num"], False),
                                 r.get("report_total_mb") or 0, r["split_hash"]))
    if rest:
        n = rest[0]["file_num"]
        picks.append(n)
        why[n] = f"strongest NAD27/local-grid/no-datum signal among remaining dev files (score {datum.get(n, {}).get('score', 0)})"
    return {"files": picks, "reasons": why}


# ---------------------------------------------------------------- stage drivers


def read_selection(path: Path | None = None) -> dict[str, Any]:
    path = path or selection_path()
    if not path.is_file():
        raise FileNotFoundError(f"{path} missing; run `lr select shortlist` first")
    return json.loads(path.read_text())


def write_selection(sel: dict[str, Any], path: Path | None = None) -> Path:
    path = path or selection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in sel.items() if not k.startswith("_")}
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(body, indent=1, sort_keys=False, ensure_ascii=False) + "\n")
    tmp.rename(path)
    return path


def stage_shortlist(log: Callable[[str], None] = print, index_dir: Path | None = None) -> dict[str, Any]:
    idx = load_index(index_dir)
    sl = build_shortlist(idx)
    sl.pop("_ranked")
    sel = {"version": SELECTION_VERSION, "params": PARAMS, "score_weights": SCORE_WEIGHTS,
           "index_sources_sha256": idx.sources, "shortlist": sl}
    old = selection_path()
    if old.is_file():
        prev = json.loads(old.read_text())
        # keep downstream stages only if the shortlist is unchanged
        if canonical_json(prev.get("shortlist")) == canonical_json(json.loads(json.dumps(sl))):
            for k in ("probe", "final", "post_fetch", "locked"):
                if k in prev:
                    sel[k] = prev[k]
    write_selection(sel)
    log(f"funnel: {json.dumps(sl['funnel'])}")
    log(f"passing by era: {sl['passing_by_era']} (pre-1968 in 1970s bucket: {sl['pre1968_in_1970s_bucket']})")
    for c in sl["candidates"]:
        f = c["features"]
        log(f"  {c['file_num']:<12} {c['era']:<12} {f['year']} score {c['score']:g} tb {c['tiebreak']} "
            f"holes wt={f['work_hole_count']} geods={f['geods_holes']} z12={int(f['zone12'])} {f['company'][:40]}")
    return sel


def stage_probe(log: Callable[[str], None] = print, client: Any = None) -> dict[str, Any]:
    from .arcgis import ArcGisClient

    sel = read_selection()
    own = client is None
    client = client or ArcGisClient(cache_dir=PATHS.cache / "arcgis")
    idx = load_index()
    try:
        sel["probe"] = run_probe(sel, lambda num: probe_listing(client, num), idx=idx, log=log)
    finally:
        if own:
            client.close()
    write_selection(sel)
    log(f"passing by era after probe: {sel['probe']['passing_by_era']}; extended: {sel['probe']['extended']}")
    return sel


def _candidate_rows(sel: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for c in all_candidates(sel):
        p = sel.get("probe", {}).get("files", {}).get(c["file_num"])
        if not p or not p["passes"]:
            continue
        f = c["features"]
        rows.append({"file_num": c["file_num"], "era": c["era"], "score": p["score"], "tiebreak": c["tiebreak"],
                     "pre1968": f["pre1968"], "company_key": f["company_key"], "zone12": f["zone12"],
                     "split_hash": f["split_hash"], "report_total_mb": p["report_total_mb"]})
    return rows


def stage_final(log: Callable[[str], None] = print) -> dict[str, Any]:
    sel = read_selection()
    if "probe" not in sel:
        raise RuntimeError("run `lr select probe` first")
    rows = _candidate_rows(sel)
    result = solve_final(rows)
    by_num = {c["file_num"]: c for c in all_candidates(sel)}
    picked = [r for r in rows if r["file_num"] in result["selected"]]
    datum = {r["file_num"]: datum_signal(by_num[r["file_num"]]["features"]) for r in picked}
    split = split_files(picked, {n: d["likely"] for n, d in datum.items()})
    phase1 = choose_phase1(picked, split["dev"], datum)
    result["datum_prior"] = datum
    result["split_provisional"] = split
    result["phase1_provisional"] = phase1
    result["summary"] = [
        {"file_num": n, "era": by_num[n]["era"], "year": by_num[n]["features"]["year"],
         "company": by_num[n]["features"]["company"],
         "score": next(r["score"] for r in rows if r["file_num"] == n),
         "score_components": sel["probe"]["files"][n]["score_components"],
         "report_pdfs": sel["probe"]["files"][n]["n_report_pdfs"],
         "report_total_mb": sel["probe"]["files"][n]["report_total_mb"],
         "geods_holes": by_num[n]["features"]["geods_holes"],
         "compilation_holes": by_num[n]["features"]["compilation_holes"],
         "work_hole_count": by_num[n]["features"]["work_hole_count"],
         "zone12": by_num[n]["features"]["zone12"],
         "split": "heldout" if n in split["heldout"] else "dev",
         "phase1": n in phase1["files"],
         "reasons": result["reasons"].get(n, []) + [split["reasons"].get(n, "")] + [phase1["reasons"].get(n, "")]}
        for n in result["selected"]
    ]
    for s in result["summary"]:
        s["reasons"] = [x for x in s["reasons"] if x]
    sel["final"] = result
    sel.pop("post_fetch", None)
    write_selection(sel)
    for s in result["summary"]:
        log(f"  {s['file_num']:<12} {s['era']:<12} {s['year']} {s['split']:<7} {'P1' if s['phase1'] else '  '} "
            f"score {s['score']:g} reports {s['report_pdfs']} {s['report_total_mb']:.1f} MB holes {s['geods_holes']} "
            f"{s['company'][:36]}")
    log(f"unmet constraints: {result['unmet'] or 'none'}")
    return sel


def selected_files(sel: dict[str, Any], phase1_only: bool = False) -> list[str]:
    final = sel.get("final")
    if not final:
        raise RuntimeError("run `lr select final` first")
    if phase1_only:
        locked = sel.get("locked")
        return list(locked["phase1"]["files"] if locked else final["phase1_provisional"]["files"])
    return list(final["selected"])


def fetch_items(sel: dict[str, Any], file_nums: Iterable[str]) -> list[dict[str, Any]]:
    items = []
    for n in file_nums:
        p = sel["probe"]["files"][n]
        for kind in ("report_pdfs", "appendix_pdfs", "assay_xls", "certificate_pdfs"):
            for it in p[kind]:
                items.append({**it, "file_num": n})
    return items


# ---------------------------------------------------------------- post-fetch check and split lock


def post_fetch_check(sel: dict[str, Any], pdf_records: dict[str, dict[str, Any]],
                     ocr_hits: dict[str, dict[str, int]] | None = None,
                     params: dict[str, Any] = PARAMS) -> dict[str, Any]:
    """Scanned and datum constraints once PDFs are on disk. `pdf_records` maps file_num to
    {"scanned": bool, "text_layer_kinds": [...], "text_hits": {...}}. Reports shortfalls and swaps."""
    final = sel["final"]
    by_num = {c["file_num"]: c for c in all_candidates(sel)}
    per_file = {}
    for n in final["selected"]:
        rec = pdf_records.get(n, {})
        d = datum_signal(by_num[n]["features"], rec.get("text_hits"), (ocr_hits or {}).get(n))
        per_file[n] = {"fetched": bool(rec), "scanned": rec.get("scanned"), "text_layer_kinds": rec.get("text_layer_kinds"),
                       "text_hits": rec.get("text_hits"), "ocr_hits": (ocr_hits or {}).get(n), "datum": d}
    scanned_n = sum(1 for v in per_file.values() if v["scanned"])
    likely_n = sum(1 for v in per_file.values() if v["datum"]["likely"])
    unfetched = [n for n, v in per_file.items() if not v["fetched"]]
    constraints = [
        asdict(ConstraintResult("min_scanned", f"at least {params['min_scanned']} scanned", scanned_n,
                                None if unfetched else scanned_n >= params["min_scanned"],
                                f"not fetched: {unfetched}" if unfetched else "")),
        asdict(ConstraintResult("min_nad27_or_no_datum",
                                f"at least {params['min_nad27_or_no_datum']} likely NAD27 or no printed datum", likely_n,
                                likely_n >= params["min_nad27_or_no_datum"],
                                "likelihood from era prior plus text-layer hits; OCR hits refine it after `lr route`")),
    ]
    suggestions = []
    selected = set(final["selected"])
    reserve = [c for c in all_candidates(sel)
               if c["file_num"] not in selected and sel["probe"]["files"].get(c["file_num"], {}).get("passes")]
    if likely_n < params["min_nad27_or_no_datum"] or (not unfetched and scanned_n < params["min_scanned"]):
        for n in final["selected"]:
            v = per_file[n]
            weak = (not v["datum"]["likely"]) or (v["scanned"] is False)
            if not weak:
                continue
            era = by_num[n]["era"]
            alts = sorted((c for c in reserve if c["era"] == era),
                          key=lambda c: (-datum_signal(c["features"])["score"], c["features"]["split_hash"]))
            if alts:
                a = alts[0]
                suggestions.append({"out": n, "in": a["file_num"], "era": era,
                                    "why": f"{a['file_num']} has datum prior {datum_signal(a['features'])['score']} "
                                           f"vs {v['datum']['score']} and is in the same era"})
    return {"files": per_file, "constraints": constraints, "shortfalls": [c["name"] for c in constraints if c["met"] is False],
            "swap_suggestions": suggestions}


def lock_split(sel: dict[str, Any], pdf_hashes: dict[str, list[dict[str, Any]]], lock_path: Path,
               now: dt.datetime | None = None) -> dict[str, Any]:
    """Final split with the best available datum signal, written once to gold/heldout.lock."""
    if lock_path.exists():
        raise FileExistsError(f"{lock_path} exists; the held-out split is locked and is never overwritten")
    final = sel["final"]
    pf = sel.get("post_fetch", {}).get("files", {})
    by_num = {c["file_num"]: c for c in all_candidates(sel)}
    rows = [r for r in _candidate_rows(sel) if r["file_num"] in final["selected"]]
    datum = {n: (pf.get(n, {}).get("datum") or datum_signal(by_num[n]["features"])) for n in final["selected"]}
    split = split_files(rows, {n: d["likely"] for n, d in datum.items()})
    missing = [n for n in split["heldout"] if not pdf_hashes.get(n)]
    if missing:
        raise RuntimeError(f"held-out files not fetched yet (no report PDF hashes): {missing}; run `lr fetch`")
    scanned = {n: bool(pf.get(n, {}).get("scanned")) for n in final["selected"]}
    phase1 = choose_phase1(rows, split["dev"], datum, scanned)
    now = now or dt.datetime.now(dt.UTC)
    lock = {
        "version": "heldout-lock/v1",
        "seed": SPLIT_SEED,
        "rule": "lowest sha256(seed + file_num) per era, plus the lowest remaining hash that keeps at least one "
                "likely NAD27-or-no-datum file held out",
        "heldout": [
            {"file_num": n, "era": by_num[n]["era"], "split_hash": by_num[n]["features"]["split_hash"],
             "reason": split["reasons"][n], "report_pdfs": pdf_hashes[n]}
            for n in split["heldout"]
        ],
        "file_nums": split["heldout"],
        "dev": split["dev"],
        "selection_sha256": sha256_bytes(canonical_json({k: sel[k] for k in ("shortlist", "probe", "final")}).encode()),
        "locked_at": now.isoformat(timespec="seconds"),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("x") as fh:  # exclusive create: never overwrite
        fh.write(json.dumps(lock, indent=2) + "\n")
    changed = split["heldout"] != final["split_provisional"]["heldout"]
    return {"split": split, "phase1": phase1, "datum": datum, "changed_from_provisional": changed,
            "lock_path": str(lock_path)}


def read_heldout(lock_path: Path | None = None) -> set[str]:
    lock_path = lock_path or (PATHS.gold / "heldout.lock")
    if not lock_path.is_file():
        raise FileNotFoundError(f"{lock_path} missing; run `lr lock-heldout` before rendering anything")
    return set(json.loads(lock_path.read_text())["file_nums"])


def heldout_pdf_hashes(lock_path: Path | None = None) -> set[str]:
    lock_path = lock_path or (PATHS.gold / "heldout.lock")
    lock = json.loads(lock_path.read_text())
    return {p["sha256"] for h in lock["heldout"] for p in h["report_pdfs"]}


__all__ = [
    "ERAS", "PARAMS", "SCORE_WEIGHTS", "build_shortlist", "choose_phase1", "classify_listing", "company_key",
    "datum_signal", "era_for_year", "file_features", "load_index", "lock_split", "post_fetch_check",
    "read_heldout", "run_probe", "selected_files", "solve_final", "split_files", "split_hash", "stage_final",
    "stage_probe", "stage_shortlist",
]


# ---------------------------------------------------------------- stage drivers after download


def probe_fetched(file_nums: Iterable[str], log: Callable[[str], None] = print) -> dict[str, dict[str, Any]]:
    """pdfprobe every fetched document PDF of these files; per-file scanned flag and text-layer datum hits."""
    from .datum import merge_counts
    from .fetch import documents_by_file
    from .pdfprobe import probe_cached

    docs = documents_by_file(PATHS.raw)
    out: dict[str, dict[str, Any]] = {}
    for n in file_nums:
        recs = docs.get(n, [])
        if not recs:
            continue
        probes = [probe_cached(PATHS.raw / d["path"], d["sha256"], PATHS.out / "pdfprobe") for d in recs]
        pages = sum(p["n_pages"] for p in probes)
        scanned_pages = sum(p["scanned_pages"] for p in probes)
        out[n] = {
            "documents": [{"path": d["path"], "sha256": d["sha256"], "kind": d["kind"], "pages": p["n_pages"],
                           "text_layer_kind": p["text_layer_kind"], "scanned_pages": p["scanned_pages"],
                           "large_format_pages": len(p["large_format_pages"])} for d, p in zip(recs, probes)],
            "pages": pages,
            "scanned_pages": scanned_pages,
            "scanned": scanned_pages >= 0.5 * max(pages, 1),
            "text_layer_kinds": sorted({p["text_layer_kind"] for p in probes}),
            "text_hits": merge_counts(*(p["text_datum_hits"] for p in probes)),
        }
        log(f"  {n:<12} {len(recs)} PDFs {pages} pages, scanned {scanned_pages}, kinds {out[n]['text_layer_kinds']}, "
            f"text-layer datum hits {{{', '.join(f'{k}: {v}' for k, v in out[n]['text_hits'].items() if v)}}}")
    return out


def stage_post_fetch(log: Callable[[str], None] = print, ocr_hits: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    sel = read_selection()
    recs = probe_fetched(sel["final"]["selected"], log=log)
    if ocr_hits is None and sel.get("post_fetch", {}).get("files"):
        ocr_hits = {n: v["ocr_hits"] for n, v in sel["post_fetch"]["files"].items() if v.get("ocr_hits")}
    check = post_fetch_check(sel, recs, ocr_hits=ocr_hits)
    for n, r in recs.items():
        check["files"][n]["documents"] = r["documents"]
        check["files"][n]["pages"] = r["pages"]
    sel["post_fetch"] = check
    write_selection(sel)
    for c in check["constraints"]:
        log(f"  constraint {c['name']}: achieved {c['achieved']} -> {'met' if c['met'] else 'NOT met' if c['met'] is False else 'unknown'}")
    for s in check["swap_suggestions"]:
        log(f"  suggestion: swap {s['out']} -> {s['in']} ({s['why']})")
    return sel


def stage_lock(log: Callable[[str], None] = print, lock_path: Path | None = None) -> dict[str, Any]:
    from .fetch import documents_by_file

    lock_path = lock_path or (PATHS.gold / "heldout.lock")
    sel = read_selection()
    docs = documents_by_file(PATHS.raw)
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        if not sel.get("locked") or sel["locked"]["heldout"] != lock["file_nums"]:
            raise FileExistsError(f"{lock_path} exists and selection.json does not match it; refusing to change the split")
        log(f"{lock_path} already locked: held out {lock['file_nums']}")
        return sel
    hashes = {n: [{"name": d["name"], "path": d["path"], "kind": d["kind"], "bytes": d["bytes"], "sha256": d["sha256"]}
                  for d in docs.get(n, [])] for n in sel["final"]["selected"]}
    res = lock_split(sel, hashes, lock_path)
    sel["locked"] = {"heldout": res["split"]["heldout"], "dev": res["split"]["dev"], "reasons": res["split"]["reasons"],
                     "heldout_has_likely_datum": res["split"]["heldout_has_likely_datum"],
                     "changed_from_provisional": res["changed_from_provisional"], "phase1": res["phase1"],
                     "lock_path": str(lock_path.relative_to(PATHS.root)) if lock_path.is_relative_to(PATHS.root) else str(lock_path)}
    write_selection(sel)
    log(f"locked {lock_path}: held out {res['split']['heldout']} (changed from provisional: {res['changed_from_provisional']})")
    log(f"phase 1 dev files: {res['phase1']['files']}")
    return sel

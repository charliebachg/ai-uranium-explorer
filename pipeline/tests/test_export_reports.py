"""The export gate: the contract, the unbacked-number rule, public-safe mode and the banned service."""

from __future__ import annotations

import json

import pytest

from uranium_explorer import contract_check, export_reports
from uranium_explorer.assemble import assemble_file
from uranium_explorer.crosscheck import build_provincial_lith, crosscheck_file
from uranium_explorer.ocr import read_words
from uranium_explorer.position import transform_hole
from uranium_explorer.validators import apply_findings, build_context, run_validators
from uranium_explorer.validators.checks import geometric_row_counts

from fake_page import FILE_NUM, PAGE_NO, PDF_SHA, extract_row

VALUE_ID_KEYS = {
    "year", "page_count", "rows_stored", "rows_printed", "from", "to", "from_m", "to_m", "code",
    "description", "sample_id", "value", "name", "offset_m", "bearing_deg", "shift_m", "lon", "lat",
    "easting", "northing", "grid_x", "grid_y", "utm_zone", "datum_printed", "elevation", "azimuth",
    "dip", "total_depth", "pass", "flag", "miss",
}


@pytest.fixture(scope="module")
def pipeline_doc():
    """One real page through assemble -> validate -> crs -> crosscheck, all in memory."""
    if not [w for w in read_words(PDF_SHA) if w["page_no"] == PAGE_NO]:
        pytest.skip("OCR words missing; run `ue ocr`")
    row = extract_row()
    doc = assemble_file(FILE_NUM, [row], {row["page_id"]: {"width_px": 1700, "height_px": 2200}},
                        log=lambda *a: None)
    ctx = build_context(FILE_NUM)
    ctx["geometric_rows"] = geometric_row_counts(doc)
    apply_findings(doc, run_validators(doc, ctx))

    from uranium_explorer import crs

    try:
        tr = crs.Nad27ToNad83()
    except crs.CrsError:
        tr = None
    holes = [transform_hole(doc, h, ctx, tr) for h in doc["holes"]]
    positions = {"holes": holes, "values": {}}
    for h in holes:
        if h.get("lonlat"):
            h["lon_vid"] = f"d:{FILE_NUM}:test_lon"
            h["lat_vid"] = f"d:{FILE_NUM}:test_lat"
            from uranium_explorer.values import derived

            positions["values"][h["lon_vid"]] = derived(h["lon_vid"], h["lonlat"][0], "ratio3",
                                                        "provincial_position", [], unit="deg")
            positions["values"][h["lat_vid"]] = derived(h["lat_vid"], h["lonlat"][1], "ratio3",
                                                        "provincial_position", [], unit="deg")
    cross = crosscheck_file(FILE_NUM, doc, positions, ctx, tr)
    lith, lith_values = build_provincial_lith(
        FILE_NUM, {m["provincial_name"]: [{"record_id": 1, "hole_name": m["provincial_name"],
                                           "from_m": 0.0, "to_m": 18.4, "code": "SST",
                                           "description": "Sandstone"}]
                   for m in cross["matches"] if m["dataset"] == "geods" and m.get("provincial_name")},
        "2026-09-18T00:00:00Z")
    cross["provincial_lith"] = lith
    cross["provincial_lith_values"] = lith_values
    return doc, positions, cross


@pytest.fixture
def exported(pipeline_doc, tmp_path, monkeypatch):
    doc, positions, cross = pipeline_doc
    monkeypatch.setattr(export_reports, "assembled_files", lambda: [FILE_NUM])
    monkeypatch.setattr(export_reports, "read_assembled", lambda fn: doc)
    monkeypatch.setattr(export_reports, "read_positions", lambda fn: positions)
    monkeypatch.setattr(export_reports, "read_crosscheck", lambda fn: cross)

    def run(public_safe: bool = False):
        target = tmp_path / ("safe" if public_safe else "full")
        summary = export_reports.stage_export_reports(files=[FILE_NUM], public_safe=public_safe,
                                                      target=target, log=lambda *a: None)
        return target, summary

    return run


def walk_value_ids(node, path="$"):
    """Every ValueId-shaped leaf in an exported document, with where it was found."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in VALUE_ID_KEYS and isinstance(v, str) and contract_check.VALUE_ID.match(v):
                yield f"{path}.{k}", v
            else:
                yield from walk_value_ids(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_value_ids(v, f"{path}[{i}]")


def test_the_export_validates_against_the_contract(exported):
    target, summary = exported()
    assert summary["contract_errors"] == []
    assert contract_check.check_export(target) == []
    assert (target / "reports" / "index.json").is_file()
    assert (target / "reports" / FILE_NUM / "report.json").is_file()
    assert (target / "reports" / FILE_NUM / "pages.json").is_file()


def test_every_numeric_leaf_resolves_to_a_stored_value(exported):
    """The unbacked-number gate: no number reaches a screen without a quote, a derivation or a source."""
    target, _ = exported()
    report = json.loads((target / "reports" / FILE_NUM / "report.json").read_text())
    registry = report["values"]
    refs = list(walk_value_ids({k: report[k] for k in ("summary", "tables", "holes")}))
    assert refs, "the report references at least one value"
    unbacked = [(path, vid) for path, vid in refs if vid not in registry]
    assert unbacked == [], f"unbacked value ids: {unbacked[:5]}"
    for _path, vid in refs:
        val = registry[vid]
        if val["kind"] == "extracted":
            assert val["lineage"]["quote"] is not None
            assert "quote_located" in val["lineage"]
        elif val["kind"] == "derived":
            assert val["derivation"]["op"]
        elif val["kind"] == "source":
            assert val["source"]["dataset"]


def test_an_extracted_value_that_could_not_be_located_says_so(exported):
    target, _ = exported()
    report = json.loads((target / "reports" / FILE_NUM / "report.json").read_text())
    extracted = [v for v in report["values"].values() if v["kind"] == "extracted"]
    assert extracted
    for v in extracted:
        lineage = v["lineage"]
        # either the quote was located, or the value is flagged and the box is honest about it
        assert lineage["quote_located"] in (True, False)
        if not lineage["quote_located"]:
            assert v["status"] in ("flag", "miss") or lineage["bbox"] is not None
    located = sum(1 for v in extracted if v["lineage"]["quote_located"])
    assert located / len(extracted) > 0.5


def test_page_images_are_written_and_referenced(exported):
    target, summary = exported()
    pages = json.loads((target / "reports" / FILE_NUM / "pages.json").read_text())
    with_images = [p for p in pages["pages"] if p["image"]]
    assert with_images and summary["page_images"] == len(with_images)
    for p in with_images:
        assert (target / p["image"]).is_file() and (target / p["thumb"]).is_file()
        assert p["image"].endswith(".webp") and p["thumb"].startswith(f"pages/{FILE_NUM}/t")
        assert p["width_px"] > 0 and p["height_px"] > 0
        assert p["n_values"] > 0


def test_public_safe_omits_every_page_image(exported):
    target, summary = exported(public_safe=True)
    assert summary["public_safe"] is True and summary["page_images"] == 0
    pages = json.loads((target / "reports" / FILE_NUM / "pages.json").read_text())
    assert all(p["image"] is None and p["thumb"] is None for p in pages["pages"])
    assert not (target / "pages").exists()
    assert contract_check.check_export(target, public_safe=True) == []
    # and the values, quotes and boxes are still there: the page proxy needs them
    report = json.loads((target / "reports" / FILE_NUM / "report.json").read_text())
    assert any(v.get("lineage", {}).get("bbox") for v in report["values"].values())


def test_the_export_never_mentions_the_banned_service(exported):
    target, _ = exported()
    for path in sorted(target.rglob("*.json")):
        text = path.read_text()
        assert "Mining/MapServer" not in text, path
        assert "MapServer/6" not in text, path


def test_a_footprint_falls_back_to_the_nts_sheets_and_says_so(exported):
    """74H09-0039's collar is on a local grid, so no hole is plottable from the page alone."""
    target, _ = exported()
    index = json.loads((target / "reports" / "index.json").read_text())
    summary = index["reports"][0]
    assert summary["footprint"]["type"] == "Polygon"
    note = summary.get("note", "")
    basis = index["values"][f"d:{FILE_NUM}:footprint_basis"]
    if summary["holes"] and any(h["lonlat"] for h in summary["holes"]):
        assert "hull of" in note and basis["value"] > 0
    else:
        assert "NTS sheets" in note and basis["value"] == 0
        assert "not a measured extent" in note


def test_a_local_grid_hole_is_marked_as_such_in_the_stub(exported):
    target, _ = exported()
    index = json.loads((target / "reports" / "index.json").read_text())
    stubs = index["reports"][0]["holes"]
    assert stubs
    assert stubs[0]["datum_basis"] in ("local_grid", "none")
    report = json.loads((target / "reports" / FILE_NUM / "report.json").read_text())
    assert report["holes"][0]["collar"]["coord_kind"] in ("local_grid", "not_printed")


def test_tables_carry_both_row_counts(exported):
    target, _ = exported()
    report = json.loads((target / "reports" / FILE_NUM / "report.json").read_text())
    assert report["tables"]
    for t in report["tables"]:
        assert t["kind"] in contract_check.TABLE_KINDS
        assert report["values"][t["rows_stored"]]["value"] >= 0
        if t["rows_printed"]:
            assert report["values"][t["rows_printed"]]["value"] >= 0


def test_a_broken_export_is_refused_before_anything_is_written(pipeline_doc, tmp_path, monkeypatch):
    doc, positions, cross = pipeline_doc
    broken = json.loads(json.dumps(doc))
    # a hole that points at a value id nothing stores: exactly the unbacked number the gate exists for
    broken["holes"][0]["collar"]["easting"] = "x:74H09-0039:doesnotexist"
    monkeypatch.setattr(export_reports, "assembled_files", lambda: [FILE_NUM])
    monkeypatch.setattr(export_reports, "read_assembled", lambda fn: broken)
    monkeypatch.setattr(export_reports, "read_positions", lambda fn: positions)
    monkeypatch.setattr(export_reports, "read_crosscheck", lambda fn: cross)
    target = tmp_path / "refused"
    with pytest.raises(ValueError, match="export refused"):
        export_reports.stage_export_reports(files=[FILE_NUM], target=target, log=lambda *a: None)
    assert not (target / "reports" / FILE_NUM / "report.json").exists()


def test_the_manifest_patch_touches_only_files_read_and_the_artifacts(exported, tmp_path):
    target, _ = exported()
    manifest_src = {
        "schema_version": "1.0.0",
        "build": {"id": "b", "created_at": "2026-09-18T00:00:00Z", "pipeline_version": "0.1.0",
                  "page_images": False, "public_safe": False, "fixture": False},
        "bbox": [-110.1, 49.0, -101.3, 60.0],
        "stats": {"m:compilation_collars": {"id": "m:compilation_collars", "kind": "stat",
                                            "as_printed": None, "value": 33490, "unit_as_printed": None,
                                            "fmt": "int", "note": "keep me"},
                  "m:files_read": {"id": "m:files_read", "kind": "stat", "as_printed": None, "value": 0,
                                   "unit_as_printed": None, "fmt": "int",
                                   "note": "uranium-tagged assessment files read by this pipeline"}},
        "ref_points": [], "sources": [], "dictionaries": {"companies": ["A"]},
        "artifacts": {"bulk/x.geojson": {"path": "bulk/x.geojson", "bytes": 1, "sha256": "0" * 64}},
    }
    (target / "manifest.json").write_text(json.dumps(manifest_src))
    assert export_reports.patch_manifest(target, 4, {"reports/index.json": {
        "path": "reports/index.json", "bytes": 10, "sha256": "1" * 64}}, page_images=True,
        log=lambda *a: None)
    after = json.loads((target / "manifest.json").read_text())
    assert after["stats"]["m:files_read"]["value"] == 4
    assert after["stats"]["m:compilation_collars"] == manifest_src["stats"]["m:compilation_collars"]
    assert after["artifacts"]["bulk/x.geojson"] == manifest_src["artifacts"]["bulk/x.geojson"]
    assert "reports/index.json" in after["artifacts"]
    assert after["build"]["page_images"] is True
    assert after["bbox"] == manifest_src["bbox"] and after["dictionaries"] == manifest_src["dictionaries"]
    assert contract_check.check_manifest(after) == []


def test_read_holes_are_marked_in_the_bulk_layers(exported, tmp_path):
    target, _ = exported()
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": 11, "properties": {"n": "A", "u": 1, "rd": 0},
         "geometry": {"type": "Point", "coordinates": [-104.0, 57.0]}},
        {"type": "Feature", "id": 12, "properties": {"n": "B", "u": 1, "rd": 0},
         "geometry": {"type": "Point", "coordinates": [-104.1, 57.1]}}]}
    path = target / "bulk" / "compilation_collars.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fc))
    arts = export_reports.mark_read_holes(target, {"compilation": {11}}, log=lambda *a: None)
    after = json.loads(path.read_text())
    assert [f["properties"]["rd"] for f in after["features"]] == [1, 0]
    assert arts["bulk/compilation_collars.geojson"]["count"] == 2


def test_the_python_contract_mirror_rejects_what_zod_would():
    bad_val = {"id": "x:f:1", "kind": "extracted", "as_printed": "1", "value": 1, "unit_as_printed": None}
    e = contract_check.Errors()
    contract_check.check_val(e, "$", bad_val)
    assert any("no lineage" in m for m in e)

    e = contract_check.Errors()
    contract_check.check_val(e, "$", {**bad_val, "id": "nope:f:1"})
    assert any("namespaced" in m for m in e)

    e = contract_check.Errors()
    contract_check.check_bbox(e, "$", [0.5, 0.5, 0.2, 0.9])
    assert any("normalised" in m for m in e)

    e = contract_check.Errors()
    contract_check.check_registry(e, "$", {"x:f:1": {**bad_val, "id": "x:f:2"}})
    assert any("registry key" in m for m in e)

"""The readiness gate: five columns per dataset, and the rule that every one of them must be green.

The assessors are pure, so each column's semantics is pinned with synthetic inputs; one test runs the whole
check against the real store and asserts only that it produces rows and writes its JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacy_reader.prospect import gate as G

SK = "Government of Saskatchewan Standard Unrestricted Use Data Licence v2.0"


def layer(**over) -> dict:
    base = {"key": "em_conductors", "title": "EM conductors", "kind": "layer", "role": "feature",
            "record_count": 24216, "licence": SK, "redistributable": True, "hashed": "payload 2f4ba2a9c1c9",
            "pulled": True, "snapshot": "4821a4017ef6",
            "exports": ["context/em_conductors.geojson", "tiles:conductors"],
            "features": [{"feature_key": "d_conductor_m", "coverage": 1.0, "thin": False},
                         {"feature_key": "conductor_density", "coverage": 0.241, "thin": True}]}
    base.update(over)
    return G.assess_layer(**base)


def item(name: str, on_disk: bool = True, sha: str | None = "abc") -> dict:
    return {"name": name, "kind": "report_pdfs", "on_disk": on_disk, "sha256": sha if on_disk else None}


def greens(row: dict) -> dict[str, bool]:
    return {c: row[c]["ok"] for c in G.COLUMNS}


# ---------------------------------------------------------------- layers


def test_a_redistributable_layer_that_is_present_hashed_and_exported_is_green_everywhere() -> None:
    row = layer()
    assert greens(row) == dict.fromkeys(G.COLUMNS, True)
    assert row["present"]["note"] == "24,216 rows"
    assert "redistributable" in row["licensed"]["note"]
    assert "max 100% (d_conductor_m)" in row["covers"]["note"]
    assert "thin: conductor_density 24%" in row["covers"]["note"]      # information, not a failure
    assert row["servable"]["note"] == "context/em_conductors.geojson; tiles:conductors"
    assert row["versioned"]["note"] == "payload 2f4ba2a9c1c9; in the pull log; snapshot 4821a4017ef6"


def test_a_layer_with_no_payload_hash_is_red_on_versioned_only() -> None:
    row = layer(hashed=None)
    assert greens(row) == {**dict.fromkeys(G.COLUMNS, True), "versioned": False}
    assert row["versioned"]["note"] == "no payload hash"


def test_the_snapshot_condition_folds_into_versioned() -> None:
    row = layer(snapshot=None)
    assert not row["versioned"]["ok"]
    assert "no snapshot" in row["versioned"]["note"]
    assert not layer(pulled=False)["versioned"]["ok"]


def test_a_non_redistributable_label_is_licensed_and_servable_as_local_only() -> None:
    row = layer(key="geods_holes", kind="label", role="label", licence="No licence stated on the service",
                redistributable=False, exports=[], features=[])
    assert not row["licensed"]["ok"]                     # a licence nobody stated is not a licence
    row = layer(key="smdi", kind="label", role="label", licence="Crown copyright, internal use",
                redistributable=False, exports=[], features=[])
    assert row["licensed"]["ok"] and "local only" in row["licensed"]["note"]
    assert row["servable"]["ok"] and "local only" in row["servable"]["note"]
    assert row["covers"] == {"ok": True, "note": "label"}


def test_a_non_redistributable_layer_that_is_exported_is_red_on_licensed() -> None:
    row = layer(licence="Crown copyright, internal use", redistributable=False)
    assert not row["licensed"]["ok"] and "exported" in row["licensed"]["note"]


def test_a_redistributable_feature_source_with_no_export_path_is_red_on_servable() -> None:
    row = layer(exports=[])
    assert not row["servable"]["ok"]
    assert layer(exports=["coverage export (2 feature(s))"])["servable"]["ok"]


def test_covers_is_red_when_a_feature_has_no_coverage_stated() -> None:
    row = layer(features=[{"feature_key": "d_conductor_m", "coverage": None, "thin": False}])
    assert not row["covers"]["ok"] and "d_conductor_m" in row["covers"]["note"]
    assert not layer(features=[{"feature_key": "x", "coverage": float("nan"), "thin": False}])["covers"]["ok"]
    assert not layer(features=[])["covers"]["ok"]


def test_present_needs_a_store_row_with_rows_in_it() -> None:
    assert layer(record_count=None)["present"] == {"ok": False, "note": "no store row"}
    assert layer(record_count=0)["present"] == {"ok": False, "note": "0 rows"}
    assert layer(kind="scene", record_count=24)["present"]["note"] == "24 scenes"


# ---------------------------------------------------------------- files


def test_a_file_with_two_of_three_objects_on_disk_is_red_on_present() -> None:
    row = G.assess_file(file_num="MAW00131", items=[item("a.pdf"), item("b.pdf"), item("c.pdf", on_disk=False)],
                        pages_rendered=113, pages_with_text=100, parts=1)
    assert not row["present"]["ok"]
    assert row["present"]["note"] == "2 of 3 objects on disk; 1 .part in progress"
    assert not row["versioned"]["ok"] and row["versioned"]["note"].startswith("2 of 3 objects hashed")
    assert row["kind"] == "file"


def test_a_file_with_ten_of_twelve_pages_with_text_is_green_on_covers() -> None:
    row = G.assess_file(file_num="74H04-0094", items=[item("a.pdf")], pages_rendered=12, pages_with_text=10)
    assert greens(row) == dict.fromkeys(G.COLUMNS, True)
    assert row["covers"]["note"].startswith("10 of 12")
    assert row["licensed"]["note"].startswith("local only, never served")
    assert row["servable"] == {"ok": True, "note": "not served (local only); no page images exported"}
    assert row["versioned"]["note"] == "1 of 1 objects hashed in data/raw/manifest.json"


def test_a_file_with_nothing_rendered_or_no_text_is_red_on_covers_and_thin_is_only_flagged() -> None:
    assert G.assess_file(file_num="f", items=[item("a")], pages_rendered=0, pages_with_text=0)["covers"] == \
        {"ok": False, "note": "no pages rendered"}
    assert G.assess_file(file_num="f", items=[item("a")], pages_rendered=157, pages_with_text=0)["covers"] == \
        {"ok": False, "note": "0 of 157 rendered pages have text"}
    thin = G.assess_file(file_num="f", items=[item("a")], pages_rendered=100, pages_with_text=30)["covers"]
    assert thin["ok"] and thin["note"].endswith("; thin")


def test_exported_page_images_are_reported_not_required() -> None:
    row = G.assess_file(file_num="f", items=[item("a")], pages_rendered=5, pages_with_text=5, served_pages=3)
    assert row["servable"] == {"ok": True, "note": "not served (local only); 3 page image(s) exported"}


# ---------------------------------------------------------------- verdict and helpers


def test_the_verdict_names_every_red_cell() -> None:
    rows = [layer(), layer(key="faults_250k", hashed=None),
            G.assess_file(file_num="f", items=[item("a", on_disk=False)], pages_rendered=0, pages_with_text=0)]
    green, failures = G.verdict(rows)
    assert not green
    assert failures == ["faults_250k: versioned: no payload hash",
                        "f: present: 0 of 1 objects on disk",
                        "f: covers: no pages rendered",
                        "f: versioned: 0 of 1 objects hashed in data/raw/manifest.json"]
    assert G.verdict([layer()]) == (True, [])


def test_licence_stated_rejects_the_ways_a_licence_is_not_stated() -> None:
    assert G.licence_stated(SK)
    assert not G.licence_stated(None) and not G.licence_stated("  ")
    assert not G.licence_stated("No licence stated on the service")
    assert not G.licence_stated("no licence stated")
    assert not G.licence_stated("not_stated")


def test_layer_exports_finds_context_files_tiles_and_hand_written_exports(tmp_path: Path) -> None:
    from legacy_reader.export_layers import EXPORTS

    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "em_conductors.geojson").write_text("{}")
    (tmp_path / "context" / "surveys_airborne.geojson").write_text("{}")
    (tmp_path / "context" / "uranium_deposits.geojson").write_text("{}")
    tiles = {"conductors": {"source_geojson": "context/em_conductors.geojson"}}
    assert G.layer_exports("em_conductors", tmp_path, tiles, EXPORTS) == \
        ["context/em_conductors.geojson", "tiles:conductors"]
    # the airborne footprints are registered as assessment_surveys but pulled under their own key
    assert G.layer_exports("survey_footprints_airborne", tmp_path, tiles, EXPORTS) == ["context/surveys_airborne.geojson"]
    assert G.layer_exports("uranium_deposit_footprints", tmp_path, tiles, EXPORTS) == ["context/uranium_deposits.geojson"]
    assert G.layer_exports("faults_250k", tmp_path, tiles, EXPORTS) == []     # not on disk


def test_resolve_source_by_key_collection_or_pull_url() -> None:
    from legacy_reader.prospect.inventory import Licence, Source

    lic = Licence("sk", SK, "", "terms", True)
    mk = lambda key, url, collection=None: Source(
        key=key, title=key, tier="native", role="feature", bears_on="trap", access="arcgis_rest", url=url,
        licence=lic, verified=True, verified_at="2026", fields_verified=True, collection=collection)
    sources = [mk("assessment_surveys", "https://x/Mineral_Assessment_File_Information/FeatureServer/2"),
               mk("sentinel2_l2a", "https://stac", collection="sentinel-2-l2a")]
    pulls = {"survey_footprints_airborne": {"url": "https://x/Mineral_Assessment_File_Information/FeatureServer/2"}}
    assert G.resolve_source("sentinel2_l2a", sources, pulls).key == "sentinel2_l2a"
    assert G.resolve_source("sentinel-2-l2a", sources, pulls).key == "sentinel2_l2a"
    assert G.resolve_source("survey_footprints_airborne", sources, pulls).key == "assessment_surveys"
    assert G.resolve_source("nothing", sources, pulls) is None


def test_the_table_prints_one_mark_line_and_one_note_line_per_row() -> None:
    lines: list[str] = []
    G.render_table([layer(), layer(key="faults_250k", hashed=None)], log=lines.append)
    assert len(lines) == 5
    assert lines[1].split() == ["em_conductors", "layer", "ok", "ok", "ok", "ok", "ok"]
    assert lines[3].split() == ["faults_250k", "layer", "ok", "ok", "ok", "ok", "FAIL"]
    assert "no payload hash" in lines[4]


# ---------------------------------------------------------------- the real thing


def test_check_runs_against_the_real_store_and_writes_gate_json(tmp_path: Path, prospect_sandbox) -> None:
    """Read-only over the analytics store; the hash and snapshots come from the sandbox, never the real file."""
    from legacy_reader.store import db_path

    if not db_path().is_file():
        pytest.skip("no analytics store at data/lr.duckdb")
    prospect_sandbox.make_store()
    lines: list[str] = []
    out = G.check(log=lines.append, out_path=tmp_path / "gate.json")
    assert out["rows"] and all(set(G.COLUMNS) <= set(r) for r in out["rows"])
    assert {r["kind"] for r in out["rows"]} <= set(G.KINDS)
    assert isinstance(out["green"], bool) and out["green"] == (not out["failures"])
    assert lines[-2] in ("  gate green",) or lines[-2].startswith("  gate red: ")
    on_disk = json.loads((tmp_path / "gate.json").read_text())
    assert on_disk["rows"] == out["rows"] and on_disk["snapshot"] is None

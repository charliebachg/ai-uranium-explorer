"""Placing collars, and comparing them with the province.

The rule these tests defend: a coordinate is only transformed when the page prints the datum. A local
grid and a missing datum are recorded as such and placed, if at all, by the province, labelled as a
provincial position.
"""

from __future__ import annotations

import pytest

from uranium_explorer import crs
from uranium_explorer.crosscheck import (
    NEAREST_CANDIDATE_M,
    _shift_signature,
    build_provincial_lith,
    crosscheck_file,
    name_match_kind,
)
from uranium_explorer.position import parse_datum, transform_hole

from doc_builder import Doc

# Eastern Athabasca Basin, UTM zone 13: the region the demo's reference shift points sit in.
# the centre of NTS sheet 74H09 (-104.25, 57.625) expressed as NAD27 UTM zone 13
EASTING, NORTHING, ZONE = 544_796.0, 6_386_998.0, 13
CTX = {"nts_sheets": ["74H09"], "geods_holes": [], "compilation_holes": [], "work": {}}


@pytest.fixture(scope="module")
def tr() -> crs.Nad27ToNad83:
    try:
        return crs.Nad27ToNad83()
    except crs.CrsError as e:
        pytest.skip(f"NTv2 grid unavailable: {e}")


def collar(**fields) -> tuple[dict, dict]:
    d = Doc()
    hole = d.hole()
    out = {}
    for name, (printed, value) in fields.items():
        out[name] = d.value(name, printed, value, hole_id=hole["hole_id"])
    hole["collar"] = out
    return d.doc, hole


def test_parse_datum_only_accepts_printed_datums():
    assert parse_datum("NAD 27")[0] == "NAD27"
    assert parse_datum("nad83")[0] == "NAD83"
    assert parse_datum("UTM NAD 27 Zone 13")[0] == "NAD27"
    assert parse_datum(None)[0] is None
    assert parse_datum("Grid Q-9")[0] is None


def test_a_printed_nad27_collar_is_transformed_with_the_pinned_grid(tr):
    doc, hole = collar(datum=("NAD 27", "NAD 27"), easting=("544796", EASTING),
                       northing=("6386998", NORTHING), utm_zone=("13", 13.0))
    out = transform_hole(doc, hole, CTX, tr)
    assert out["status"] == "transformed"
    assert out["position_source"] == "extracted_transformed"
    assert out["transform"]["name"] == "NAD27 to NAD83 (4)"
    assert out["transform"]["code"] == "EPSG:1313"
    assert out["transform"]["grid_sha256"] == tr.grid_sha256
    assert 20.0 < out["shift_m"] < 60.0, "the eastern basin shift is tens of metres"
    assert out["misread_lonlat"] is not None, "where the same numbers plot if the datum is ignored"
    moved = crs.geodesic_shift(out["lonlat"][0], out["lonlat"][1],
                               out["misread_lonlat"][0], out["misread_lonlat"][1])
    assert moved.dist_m == pytest.approx(out["misread_offset_m"], abs=0.5)
    # printed UTM read on the wrong datum moves much further than the 34 m geographic datum shift,
    # because the two ellipsoids measure the meridian differently
    assert out["misread_kind"] == "utm_read_on_the_wrong_datum"
    assert out["misread_offset_m"] > 5 * out["shift_m"]


def test_a_printed_nad83_collar_is_not_datum_shifted(tr):
    doc, hole = collar(datum=("NAD83", "NAD83"), easting=("544796", EASTING),
                       northing=("6386998", NORTHING), utm_zone=("13", 13.0))
    out = transform_hole(doc, hole, CTX, tr)
    assert out["position_source"] == "extracted_nad83"
    assert out["transform"] is None and out["shift_m"] == 0.0
    assert out["misread_lonlat"] is None


def test_a_local_grid_collar_is_never_transformed(tr):
    """74H09-0039 prints "GRID LATITUDE 20,960,600 N; 1,753,200 E" and no datum."""
    doc, hole = collar(grid_x=("1,753,200 E", 1753200.0), grid_y=("20,960,600 N", 20960600.0))
    out = transform_hole(doc, hole, CTX, tr)
    assert out["status"] == "not_transformable_local_grid"
    assert out["transform"] is None
    assert out["lonlat"] is None, "nothing places it: there is no provincial record in this context"
    assert out["position_source"] == "none"
    assert any("local exploration grid" in n for n in out["notes"])


def test_an_implausible_utm_pair_is_treated_as_a_local_grid(tr):
    doc, hole = collar(datum=("NAD 27", "NAD 27"), easting=("1753200", 1753200.0),
                       northing=("20960600", 20960600.0))
    out = transform_hole(doc, hole, CTX, tr)
    assert out["status"] == "not_transformable_local_grid"
    assert out["lonlat"] is None


def test_a_local_grid_collar_is_placed_by_the_province_and_labelled(tr):
    doc, hole = collar(grid_x=("1,753,200 E", 1753200.0))
    ctx = {**CTX, "geods_holes": [{"id": 77, "name": "R-78-027", "lonlat": [-104.4, 57.45],
                                   "total_depth_m": 60.0}]}
    out = transform_hole(doc, hole, ctx, tr)
    assert out["status"] == "not_transformable_local_grid"
    assert out["position_source"] == "provincial_geods"
    assert out["lonlat"] == [-104.4, 57.45]
    assert out["provincial"]["record_id"] == 77
    assert any("provincial name match" in n for n in out["notes"])


def test_a_collar_with_no_printed_datum_keeps_both_candidates(tr):
    doc, hole = collar(easting=("544796", EASTING), northing=("6386998", NORTHING),
                       utm_zone=("13", 13.0))
    ctx = {**CTX, "compilation_holes": [{"id": 5, "name": "R-78-27", "lonlat": [-102.0, 57.7]}]}
    out = transform_hole(doc, hole, ctx, tr)
    assert out["status"] == "not_transformable_no_datum"
    assert out["transform"] is None
    assert out["position_source"] == "provincial_compilation"
    assert out["alt_lonlat"] is not None
    assert out["candidates"]["separation_m"] > 20.0
    assert any("does not choose between them" in n for n in out["notes"])


def test_a_missing_utm_zone_is_inferred_from_the_files_sheets_and_marked(tr):
    doc, hole = collar(datum=("NAD 27", "NAD 27"), easting=("544796", EASTING),
                       northing=("6386998", NORTHING))
    out = transform_hole(doc, hole, CTX, tr)
    assert out["status"] == "transformed"
    assert out["zone"] == 13 and out["zone_inferred"] is True
    assert any("no UTM zone is printed" in n for n in out["notes"])
    assert out["checks"]["inside_nts"] is True


def test_a_collar_with_no_coordinates_is_recorded_not_guessed(tr):
    doc, hole = collar(dip=("-70", -70.0))
    out = transform_hole(doc, hole, CTX, tr)
    assert out["status"] == "no_coordinates" and out["lonlat"] is None
    assert any("no collar coordinates are printed" in n for n in out["notes"])


def test_without_a_grid_nothing_is_transformed():
    doc, hole = collar(datum=("NAD 27", "NAD 27"), easting=("544796", EASTING),
                       northing=("6386998", NORTHING), utm_zone=("13", 13.0))
    out = transform_hole(doc, hole, CTX, None)
    assert out["status"] == "transform_failed"
    assert out["lonlat"] is None and out["transform"] is None


# ------------------------------------------------------------------ crosscheck

def test_name_matching_goes_exact_then_normalised_then_fuzzy():
    assert name_match_kind("R-78-27", "R-78-27") == ("exact", 100.0)
    assert name_match_kind("R-78-27", "R-78-027")[0] == "normalised"
    assert name_match_kind("R-78-27", "R 78 27")[0] == "normalised"
    assert name_match_kind("KL-5", "KL005")[0] == "normalised"
    kind, score = name_match_kind("R-78-27", "R-78-271")
    assert kind == "fuzzy" and score >= 90.0
    # two holes one number apart are different holes: a fuzzy match here would invent a match
    assert name_match_kind("R-78-27", "R-78-28")[0] is None
    assert name_match_kind("R-78-27", "MAC-14")[0] is None
    assert name_match_kind("R-78-27", "")[0] is None


def test_the_datum_shift_signature_fires_on_a_missed_transformation(tr):
    """The provincial record sits exactly where the printed NAD27 numbers plot if read as NAD83."""
    lon, lat = -105.0, 58.0
    misread = tr.inverse(lon, lat)  # NAD83 -> NAD27: the direction a missed shift moves a point
    signature, details = _shift_signature([lon, lat], [misread[0], misread[1]], tr)
    assert signature is True
    assert details["offset_m"] == pytest.approx(34.2, abs=1.0)
    assert details["local_datum_shift_m"] == pytest.approx(34.2, abs=1.0)
    assert details["bearing_difference_deg"] < 20.0


def test_an_offset_of_the_wrong_size_is_not_a_datum_signature(tr):
    lon, lat = -105.0, 58.0
    far = (lon + 0.01, lat + 0.01)  # about 1.2 km away
    signature, details = _shift_signature([lon, lat], [far[0], far[1]], tr)
    assert signature is False
    assert details["offset_m"] > 500.0


def test_an_offset_in_the_wrong_direction_is_not_a_datum_signature(tr):
    lon, lat = -105.0, 58.0
    shift = tr.shift(lon, lat)
    # same distance, perpendicular bearing
    import math

    bearing = math.radians((shift.bearing_deg + 90.0) % 360.0)
    dlat = shift.dist_m * math.cos(bearing) / 111_320.0
    dlon = shift.dist_m * math.sin(bearing) / (111_320.0 * math.cos(math.radians(lat)))
    signature, details = _shift_signature([lon, lat], [lon + dlon, lat + dlat], tr)
    assert signature is False
    assert details["bearing_difference_deg"] > 20.0


def test_crosscheck_builds_matches_offsets_and_an_adjudication_queue(tr):
    doc, hole = collar(datum=("NAD 27", "NAD 27"), easting=("544796", EASTING),
                       northing=("6386998", NORTHING), utm_zone=("13", 13.0))
    positions = {"holes": [{"hole_id": "R7827", "status": "transformed",
                            "position_source": "extracted_transformed", "lonlat": [-105.0, 58.0],
                            "lon_vid": "d:x:lon", "lat_vid": "d:x:lat", "value_ids": []}]}
    misread = tr.inverse(-105.0, 58.0)
    ctx = {**CTX,
           "geods_holes": [{"id": 1, "name": "R-78-27", "lonlat": [misread[0], misread[1]],
                            "total_depth_m": 60.0, "inclination_deg": -70.0, "azimuth_deg": 45.0}],
           "compilation_holes": [{"id": 9, "name": "SOMETHING ELSE", "lonlat": [-104.0, 57.0]}]}
    out = crosscheck_file("74H09-0039", doc, positions, ctx, tr)
    assert len(out["matches"]) == 1
    match = out["matches"][0]
    assert match["dataset"] == "geods" and match["name_match"] == "exact"
    assert match["datum_shift_signature"] is True
    assert match["adjudication"] == "needed"
    assert out["values"][match["offset_m"]]["derivation"]["op"] == "geodesic_offset"
    assert out["values"][match["offset_m"]]["value"] == pytest.approx(34.2, abs=1.0)
    reasons = " ".join(q["reason"] for q in out["adjudication_queue"])
    assert "matches the local NAD27 to NAD83 shift" in reasons
    assert "no collar was extracted" in reasons, "the unmatched compilation hole is an omission signal"


def test_an_extracted_hole_with_no_provincial_record_lands_in_the_queue(tr):
    doc, hole = collar(dip=("-70", -70.0))
    out = crosscheck_file("74H09-0039", doc, {"holes": []}, CTX, tr)
    assert out["matches"] == []
    assert out["adjudication_queue"][0]["reason"] == "extracted hole with no provincial match"


def test_a_nearest_only_candidate_stays_a_candidate(tr):
    doc, hole = collar(datum=("NAD 27", "NAD 27"), easting=("544796", EASTING),
                       northing=("6386998", NORTHING), utm_zone=("13", 13.0))
    positions = {"holes": [{"hole_id": "R7827", "status": "transformed",
                            "position_source": "extracted_transformed", "lonlat": [-105.0, 58.0],
                            "lon_vid": "d:x:lon", "lat_vid": "d:x:lat", "value_ids": []}]}
    close = (-105.0 + 0.0005, 58.0)  # about 30 m away, a completely different name
    ctx = {**CTX, "geods_holes": [{"id": 3, "name": "ZZZ-1", "lonlat": [close[0], close[1]]}]}
    out = crosscheck_file("74H09-0039", doc, positions, ctx, tr)
    assert out["matches"][0]["name_match"] == "nearest"
    assert out["matches"][0]["offset_m_value"] < NEAREST_CANDIDATE_M


def test_provincial_lithology_becomes_p_namespaced_source_values():
    raw = {"R-78-27": [{"record_id": 11, "hole_name": "R-78-27", "from_m": 0.0, "to_m": 11.3,
                        "code": "SST", "description": "Sandstone"},
                       {"record_id": 12, "hole_name": "R-78-27", "from_m": 11.3, "to_m": 60.4,
                        "code": "GNS", "description": "Gneiss"}]}
    intervals, values = build_provincial_lith("74H09-0039", raw, "2026-09-18T00:00:00Z")
    rows = intervals["R-78-27"]
    assert len(rows) == 2
    assert all(v.startswith("p:74H09-0039:") for v in values)
    assert all(values[v]["kind"] == "source" for v in values)
    assert values[rows[0]["from"]]["source"]["dataset"] == "geods_lith"
    assert values[rows[0]["from"]]["source"]["record_id"] == 11
    assert rows[0]["from_m"] == rows[0]["from"], "the province publishes metres, so no conversion happens"
    assert rows[0]["depth_unit_as_printed"] is None


def test_provincial_lithology_without_depths_is_dropped_not_guessed():
    raw = {"X": [{"record_id": 1, "hole_name": "X", "from_m": None, "to_m": None, "code": "SST",
                  "description": "Sandstone"}]}
    intervals, values = build_provincial_lith("74H09-0039", raw, "2026-09-18T00:00:00Z")
    assert intervals == {} and values == {}

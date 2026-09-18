"""The readiness export: every number backed, and the label layers kept out of the feature list."""

from __future__ import annotations

import json

import pytest

from legacy_reader.contract_check import check_readiness
from legacy_reader.paths import PATHS

READINESS = PATHS.web_data / "prospect" / "readiness.json"


@pytest.fixture(scope="module")
def doc():
    if not READINESS.is_file():
        pytest.skip("no prospect export yet; run `lr prospect export`")
    return json.loads(READINESS.read_text())


def test_the_real_export_satisfies_the_contract(doc):
    assert list(check_readiness(doc)) == []


def test_every_feature_says_whether_it_measures_effort_or_geology(doc):
    assert doc["features"], "no features exported"
    for f in doc["features"]:
        assert isinstance(f["is_effort"], bool)
        assert isinstance(f["thin"], bool)
    assert any(f["is_effort"] for f in doc["features"]), "the null model needs effort features"
    assert any(not f["is_effort"] for f in doc["features"])


def test_thin_coverage_is_published_rather_than_hidden(doc):
    thin = [f for f in doc["features"] if f["thin"] and not f["is_effort"]]
    assert thin, "the point of the page is that several features cover little of the basin"


def test_the_page_cannot_be_published_without_saying_what_it_is_not(doc):
    text = " ".join(doc["caveats"]).lower()
    assert "not a prospectivity map" in text


def test_the_missing_geophysics_is_recorded(doc):
    gaps = {g["key"] for g in doc["gaps"]}
    assert "aeromagnetic_grids" in gaps
    assert all(g["evidence"] for g in doc["gaps"])


def test_a_label_used_as_a_feature_is_refused():
    bad = {
        "schema_version": "1.0.0",
        "grid": {"cells": "c:grid:cells", "cell_m": "c:grid:cell_m", "in_basin": "c:grid:in_basin",
                 "area_km2": "c:grid:area_km2", "buffer_km": "c:grid:buffer_km"},
        "totals": {},
        "caveats": ["x"],
        "features": [{"feature_key": "leaky", "coverage": "c:cov:leaky", "covered_cells": "c:covn:leaky",
                      "median_obs": None, "median_value": None, "is_effort": False, "thin": False,
                      "sources": ["uranium_deposit_footprints"]}],
        "sources": [{"key": "uranium_deposit_footprints", "role": "label", "tier": "native",
                     "licence": "SK", "verified_at": "2026-01-01"}],
        "gaps": [],
        "values": {
            vid: {"id": vid, "kind": "stat", "as_printed": None, "value": 1, "unit_as_printed": None,
                  "fmt": "int"}
            for vid in ("c:grid:cells", "c:grid:cell_m", "c:grid:in_basin", "c:grid:area_km2",
                        "c:grid:buffer_km", "c:cov:leaky", "c:covn:leaky")
        },
    }
    errors = [str(e) for e in check_readiness(bad)]
    assert any("labels are never features" in e for e in errors), errors


def test_an_unbacked_number_is_refused():
    bad = {
        "schema_version": "1.0.0",
        "grid": {"cells": "c:grid:missing", "cell_m": "c:grid:cell_m", "in_basin": "c:grid:in_basin",
                 "area_km2": "c:grid:area_km2", "buffer_km": "c:grid:buffer_km"},
        "totals": {}, "caveats": ["x"], "features": [], "sources": [], "gaps": [],
        "values": {},
    }
    errors = [str(e) for e in check_readiness(bad)]
    assert any("unbacked number" in e for e in errors), errors

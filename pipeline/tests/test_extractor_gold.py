"""Gold pages: the skeleton is empty and never overwritten, a skeleton scores nothing, a keyed page scores
precision and recall with denominators, and no gold page at all is reported as zero rather than as a number."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from uranium_explorer import wire
from uranium_explorer.extractor import gold as G

from fake_page import FILE_NUM, PAGE_ID, PAGE_NO, PDF_SHA, page_result

PAGE_ROW = {"page_id": PAGE_ID, "file_num": FILE_NUM, "pdf_sha256": PDF_SHA, "page_no": PAGE_NO,
            "image_path": f"pages/{PDF_SHA}/p{PAGE_NO:04d}.png", "route_class": "lith_log"}


@pytest.fixture
def gold_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "gold" / "pages"
    monkeypatch.setattr(G, "gold_dir", lambda: root)
    monkeypatch.setattr(G, "read_pages", lambda: [PAGE_ROW])
    monkeypatch.setattr(G, "run_dir", lambda rid: tmp_path / "runs" / rid)
    monkeypatch.setattr(G, "read_results", lambda: {})
    return root


def test_the_skeleton_carries_the_schema_vocabulary_and_no_value(gold_root: Path) -> None:
    path = G.write_skeleton(FILE_NUM, PAGE_NO, log=lambda *a: None)
    doc = json.loads(path.read_text())
    assert doc["status"] == "skeleton" and doc["keyed_by"] is None and doc["schema_version"] == wire.SCHEMA_VERSION
    assert set(doc["fields"]) == set(wire.CellField.__args__)  # type: ignore[attr-defined]
    assert set(doc["page_level"]) == set(wire.PageLevel.model_fields) - {"page_kind", "coordinate_kind"}
    assert all(f["value_as_printed"] is None for f in doc["page_level"].values())
    cell = doc["tables"][0]["rows"][0]["cells"][0]
    assert cell == {"field": None, "value_as_printed": None, "unit_as_printed": None, "analyte_as_printed": None}
    assert "Do not copy anything from a model" in doc["instructions"]
    with pytest.raises(FileExistsError):
        G.write_skeleton(FILE_NUM, PAGE_NO, log=lambda *a: None)


def test_an_unrendered_page_is_refused(gold_root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        G.write_skeleton(FILE_NUM, 999, log=lambda *a: None)


def test_with_no_keyed_page_the_score_is_zero_pages_and_says_so(gold_root: Path, tmp_path: Path) -> None:
    G.write_skeleton(FILE_NUM, PAGE_NO, log=lambda *a: None)   # a skeleton waiting is not gold
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    out = G.score_run("r1", log=lambda *a: None)
    assert out["gold_pages"] == 0 and out["skeletons"] == 1 and out["pages_scored"] == 0
    assert out["precision"] is None and out["recall"] is None and "no hand-keyed gold page" in out["note"]
    assert json.loads((tmp_path / "runs" / "r1" / "gold_score.json").read_text())["gold_pages"] == 0


def keyed_page(doc: dict) -> dict:
    """A gold page keyed from the fake page's own values (a test convenience, not a gold set), with one
    depth different and one grade the reading does not have."""
    g = copy.deepcopy(doc)
    g["status"], g["keyed_by"] = "keyed", "a person"
    result = page_result()
    g["page_level"]["hole_id"]["value_as_printed"] = "R-78-27"
    g["page_level"]["depth_unit"]["value_as_printed"] = "ft"
    tables = []
    for t in result["tables"]:
        rows = []
        for r in t["rows"]:
            cells = [{"field": c["field"], "value_as_printed": c["value_as_printed"], "unit_as_printed": c.get("unit_as_printed"),
                      "analyte_as_printed": c.get("analyte_as_printed")} for c in r["cells"] if c["printed"] == "printed"]
            rows.append({"row_index": r["row_index"], "hole_id_as_printed": None, "cells": cells})
        tables.append({"table_index": t["table_index"], "kind": t["kind"], "rows": rows})
    tables[0]["rows"][1]["cells"][1]["value_as_printed"] = "42.5"          # the reading says 42.0
    tables[1]["rows"][0]["cells"].append({"field": "grade", "value_as_printed": "15", "unit_as_printed": "ppm",
                                          "analyte_as_printed": "U"})       # the reading has no U here
    g["tables"] = tables
    return g


def test_a_keyed_page_scores_precision_and_recall_per_field_type(gold_root: Path, tmp_path: Path) -> None:
    path = G.write_skeleton(FILE_NUM, PAGE_NO, log=lambda *a: None)
    path.write_text(json.dumps(keyed_page(json.loads(path.read_text()))))
    rd = tmp_path / "runs" / "r2"
    (rd / "readings").mkdir(parents=True)
    (rd / "pages.jsonl").write_text(json.dumps({"page_id": PAGE_ID, "file_num": FILE_NUM, "page_no": PAGE_NO, "stage": "done"}) + "\n")
    (rd / "readings" / f"{PAGE_ID.replace(':', '_')}.a.json").write_text(json.dumps(page_result()))
    out = G.score_run("r2", log=lambda *a: None)
    assert out["gold_pages"] == 1 and out["pages_scored"] == 1 and out["gold_pages_not_in_run"] == []
    n_read = len([c for t in page_result()["tables"] for r in t["rows"] for c in r["cells"] if c["printed"] == "printed"]) + 2
    assert out["n_run"] == n_read, "every printed value the reading carries is in the denominator"
    assert out["n_gold"] == n_read + 1, "the keyed page has one value more"
    assert out["fp"] == 1 and out["fn"] == 2 and out["tp"] == n_read - 1
    assert out["precision"] == pytest.approx((n_read - 1) / n_read, abs=1e-4)
    assert out["recall"] == pytest.approx((n_read - 1) / (n_read + 1), abs=1e-4)
    depth = out["by_field_type"]["depth"]
    assert depth["fp"] == 1 and depth["fn"] == 1, "42.0 against 42.5 is one wrong value and one missed value"
    assert out["by_field_type"]["grade"] == {"tp": 2, "fp": 0, "fn": 1, "n_run": 2, "n_gold": 3, "precision": 1.0,
                                             "recall": pytest.approx(2 / 3, abs=1e-4)}


def test_a_keyed_page_the_run_did_not_read_is_listed_not_scored(gold_root: Path, tmp_path: Path) -> None:
    path = G.write_skeleton(FILE_NUM, PAGE_NO, log=lambda *a: None)
    path.write_text(json.dumps(keyed_page(json.loads(path.read_text()))))
    (tmp_path / "runs" / "r3").mkdir(parents=True)
    out = G.score_run("r3", log=lambda *a: None)
    assert out["gold_pages"] == 1 and out["pages_scored"] == 0 and out["gold_pages_not_in_run"] == [f"{FILE_NUM}:{PAGE_NO}"]

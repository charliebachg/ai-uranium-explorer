"""Hand-keyed gold pages and the score against them (PRD §8.2 "scored by Tier 4", §D.3.1 role 1).

A gold page is a file under `gold/pages/<file>/p<NNNN>.json` in the shape of the wire schema, keyed by a
person from the page image. `lr gold key` writes the empty skeleton with the schema's own vocabulary, so the
person fills fields the pipeline knows; every value starts null and nothing is ever prefilled from a model,
because a gold set that started as model output would score the model against itself. A skeleton counts for
nothing until the person sets `status` to `keyed` and puts their name in `keyed_by`.

The score is precision and recall of a run's first-family readings against every keyed gold page the run
read, per field type, with the denominators printed beside every rate: a bag of printed values per page,
field and analyte, equal under the agreement rule (`agree.values_equal`). Row position is not scored. With
no keyed page the score says so and reports zero, which is the honest number today.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from .. import wire
from ..extract import read_results
from ..paths import PATHS
from ..render import read_pages
from ..runtime.runs import run_dir
from . import agree as AG

GOLD_VERSION = "gold-page/v1"
INSTRUCTIONS = (
    "Key every printed value on the page image into this file, exactly as printed (same digits, spacing and "
    "symbols; qualifiers such as < and tr kept). One row object per printed data row, one cell per printed "
    "cell, `field` from the vocabulary in `fields`, `analyte_as_printed` for a grade cell. A blank cell is "
    "left out. Set `status` to `keyed` and `keyed_by` to your name when the page is complete. Do not copy "
    "anything from a model's reading of this page."
)


def gold_dir() -> Path:
    return PATHS.gold / "pages"


def gold_path(file_num: str, page_no: int) -> Path:
    return gold_dir() / file_num / f"p{int(page_no):04d}.json"


def _page_row(file_num: str, page_no: int) -> dict[str, Any]:
    for r in read_pages():
        if r.get("file_num") == file_num and int(r.get("page_no") or 0) == int(page_no):
            return r
    raise FileNotFoundError(f"{file_num} p{page_no} is not a rendered page (run `lr render`, or check the number)")


def skeleton(file_num: str, page_no: int, page_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """The empty gold page for one rendered page: the schema's vocabulary, every value null."""
    r = page_row or _page_row(file_num, page_no)
    page_level = {key: {"value_as_printed": None, "unit_as_printed": None, "quote": None}
                  for key in wire.PageLevel.model_fields if key not in ("page_kind", "coordinate_kind")}
    cell = {"field": None, "value_as_printed": None, "unit_as_printed": None, "analyte_as_printed": None}
    return {
        "version": GOLD_VERSION, "file_num": file_num, "page": int(page_no), "page_id": r.get("page_id"),
        "pdf_sha256": r.get("pdf_sha256"), "image_path": r.get("image_path"), "route_class": r.get("route_class"),
        "schema_version": wire.SCHEMA_VERSION,
        "status": "skeleton", "keyed_by": None, "keyed_at": None, "notes": "",
        "instructions": INSTRUCTIONS,
        "fields": list(wire.CellField.__args__),  # type: ignore[attr-defined]
        "table_kinds": list(wire.TableKind.__args__),  # type: ignore[attr-defined]
        "page_kind": None, "coordinate_kind": None,
        "page_level": page_level,
        "tables": [{"table_index": 0, "kind": None, "title_as_printed": None, "column_headers_as_printed": [],
                    "printed_row_count": None,
                    "rows": [{"row_index": 0, "hole_id_as_printed": None, "cells": [dict(cell)]}]}],
    }


def write_skeleton(file_num: str, page_no: int, log: Callable[[str], None] = print) -> Path:
    """Write the skeleton, refusing to touch a file that exists: a keyed page is never overwritten."""
    path = gold_path(file_num, page_no)
    if path.exists():
        raise FileExistsError(f"{path} exists; a gold page is keyed once and never overwritten by this command")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(skeleton(file_num, page_no), indent=1) + "\n")
    log(f"  wrote {path}: fill it from the page image, then set status to keyed and keyed_by to your name")
    return path


# --------------------------------------------------------------------------------- reading gold

def load_gold(root: Path | None = None) -> tuple[list[dict[str, Any]], int]:
    """Every keyed gold page (status `keyed` with a name), and how many skeletons are still waiting."""
    base = root or gold_dir()
    keyed: list[dict[str, Any]] = []
    skeletons = 0
    for p in sorted(base.glob("*/p*.json")) if base.is_dir() else []:
        try:
            doc = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if doc.get("version") != GOLD_VERSION:
            continue
        if doc.get("status") == "keyed" and str(doc.get("keyed_by") or "").strip():
            keyed.append(doc)
        else:
            skeletons += 1
    return keyed, skeletons


def candidates_from_gold(doc: dict[str, Any]) -> list[AG.Candidate]:
    """A keyed page's values as candidates: no boxes (a person keys text, not coordinates), so alignment
    against a reading falls to the printed text, which is what a bag-of-values score compares."""
    out: list[AG.Candidate] = []
    for key, f in (doc.get("page_level") or {}).items():
        if isinstance(f, dict) and f.get("value_as_printed"):
            out.append(AG.Candidate(field=key, scope="page_level", as_printed=str(f["value_as_printed"]),
                                    unit_as_printed=f.get("unit_as_printed")))
    for table in doc.get("tables") or []:
        t_index = table.get("table_index")
        for row in table.get("rows") or []:
            name = row.get("hole_id_as_printed")
            cells = row.get("cells") or []
            if name and not any(c.get("field") == "hole_id" and c.get("value_as_printed") for c in cells):
                out.append(AG.Candidate(field="hole_id", scope="hole", as_printed=str(name).strip(),
                                        table_index=t_index, row_index=row.get("row_index")))
            for c in cells:
                if c.get("field") and c.get("value_as_printed"):
                    out.append(AG.Candidate(field=str(c["field"]), scope="cell", as_printed=str(c["value_as_printed"]),
                                            unit_as_printed=c.get("unit_as_printed"), analyte=c.get("analyte_as_printed"),
                                            table_index=t_index, row_index=row.get("row_index")))
    return out


# --------------------------------------------------------------------------------- scoring

def _pr(tp: int, fp: int, fn: int) -> dict[str, Any]:
    return {"tp": tp, "fp": fp, "fn": fn, "n_run": tp + fp, "n_gold": tp + fn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None}


def score_page(reading: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    """One page: the reading's values against the keyed values, matched by field, analyte and printed text
    (the agreement rule's equality, boxes aside). A matched pair is a true positive; a reading value the gold
    does not print is a false positive; a keyed value the reading missed is a false negative."""
    from ..locate import PageLocator

    run_values = AG.candidates_from_wire(reading, PageLocator([]))
    gold_values = candidates_from_gold(gold)
    pairs = AG.align(run_values, gold_values)
    by_type: dict[str, dict[str, int]] = {}
    total = {"tp": 0, "fp": 0, "fn": 0}
    for p in pairs:
        acc = by_type.setdefault(p.field_type, {"tp": 0, "fp": 0, "fn": 0})
        key = {"agreed": "tp", "only_a": "fp", "only_b": "fn"}.get(p.status)
        if key is None:           # a disagreement is both a wrong value and a missed one
            acc["fp"] += 1
            acc["fn"] += 1
            total["fp"] += 1
            total["fn"] += 1
            continue
        acc[key] += 1
        total[key] += 1
    return {**_pr(**total), "by_field_type": {k: _pr(**v) for k, v in sorted(by_type.items())}}


def _merge(scores: list[dict[str, Any]]) -> dict[str, Any]:
    total = {"tp": 0, "fp": 0, "fn": 0}
    by_type: dict[str, dict[str, int]] = {}
    for s in scores:
        for k in total:
            total[k] += int(s.get(k) or 0)
        for t, v in (s.get("by_field_type") or {}).items():
            acc = by_type.setdefault(t, {"tp": 0, "fp": 0, "fn": 0})
            for k in acc:
                acc[k] += int(v.get(k) or 0)
    return {**_pr(**total), "by_field_type": {k: _pr(**v) for k, v in sorted(by_type.items())}}


def _run_readings(rd: Path, which: str) -> dict[tuple[str, int], dict[str, Any]]:
    """The run's readings by (file, page): from `readings/` for a loop run, from the results file's rows
    that name the run for a batch run."""
    out: dict[tuple[str, int], dict[str, Any]] = {}
    pages_file = rd / "pages.jsonl"
    if pages_file.is_file():
        for line in pages_file.read_text().splitlines():
            if not line.strip():
                continue
            st = json.loads(line)
            p = rd / "readings" / f"{st['page_id'].replace(':', '_')}.{which}.json"
            if p.is_file():
                out[(st["file_num"], int(st["page_no"]))] = json.loads(p.read_text())
    if not out and which == "a":
        for r in read_results().values():
            if r.get("run_id") == rd.name and r.get("result"):
                out[(r["file_num"], int(r["page_no"]))] = r["result"]
    return out


def score_run(run_id: str, which: str = "a", gold_root: Path | None = None,
              log: Callable[[str], None] = print) -> dict[str, Any]:
    """Precision and recall of one run against every keyed gold page it read, written beside the run."""
    rd = run_dir(run_id)
    keyed, skeletons = load_gold(gold_root)
    readings = _run_readings(rd, which)
    per_page: list[dict[str, Any]] = []
    unread: list[str] = []
    for g in keyed:
        key = (str(g["file_num"]), int(g["page"]))
        reading = readings.get(key)
        if reading is None:
            unread.append(f"{key[0]}:{key[1]}")
            continue
        s = score_page(reading, g)
        per_page.append({"file_num": key[0], "page": key[1], "keyed_by": g.get("keyed_by"), **s})
    out = {
        "version": "gold-score/v1", "run_id": run_id, "which": which,
        "scored_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "gold_pages": len(keyed), "skeletons": skeletons, "pages_scored": len(per_page),
        "gold_pages_not_in_run": unread,
        "note": ("no hand-keyed gold page exists: nothing is scored, and no number here is an accuracy"
                 if not keyed else
                 f"{len(per_page)} of {len(keyed)} keyed page(s) were read by this run; rates are over the values listed"),
        **_merge(per_page), "pages": per_page,
    }
    if rd.is_dir():
        (rd / "gold_score.json").write_text(json.dumps(out, indent=1) + "\n")
    log(f"  gold pages keyed: {len(keyed)} ({skeletons} skeleton(s) waiting); scored {len(per_page)} against run {run_id}")
    if per_page:
        log(f"  precision {out['precision']} over {out['n_run']} read values, recall {out['recall']} over {out['n_gold']} keyed values")
        for t, v in out["by_field_type"].items():
            log(f"    {t:<11} precision {v['precision']} ({v['tp']}/{v['n_run']})  recall {v['recall']} ({v['tp']}/{v['n_gold']})")
    else:
        log(f"  {out['note']}")
    return out

"""`ue export-eval`: run statistics for the eval page, before any gold labels exist.

These are NOT accuracy figures. Without a gold set there is no denominator for "how often is the reading wrong":
what can be said is how much was read, how much of it points back at the page, what the checks flagged, and what
the run cost. The web page states that, and the `gold` field stays null until `ue score` writes a scorecard.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import __version__
from .ids import sha256_json, short
from .paths import PATHS
from .validators import REGISTRY
from .values import derived, registry, stat

SCHEMA_VERSION = "1.0.0"

CAVEATS = [
    "These are run statistics, not accuracy. No gold set has been labelled yet, so there is no denominator for how often a reading is wrong.",
    "A located quote means the words were found on the page, not that the value is right.",
    "A hole placed at a provincial position was not placed from the page, so its distance to that provincial record measures nothing.",
    "Pages were chosen by a router that prefers collar, assay and lithology pages; they are not a random sample of the file.",
]


def _reports() -> list[dict[str, Any]]:
    idx = PATHS.web_data / "reports" / "index.json"
    if not idx.is_file():
        raise RuntimeError("no reports exported; run `ue export-reports` first")
    files = [r["file_num"] for r in json.loads(idx.read_text())["reports"]]
    return [json.loads((PATHS.web_data / "reports" / f / "report.json").read_text()) for f in files]


def _runs() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for calls in sorted(PATHS.runs.glob("*/calls.jsonl")):
        rows += [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]
    return rows


def build() -> dict[str, Any]:
    reports = _reports()
    vals: list[dict[str, Any]] = []
    v = lambda vid, n, note=None, fmt="int": (vals.append(stat(vid, n, fmt=fmt, note=note)), vid)[1]  # noqa: E731

    # ---------- reading ----------
    printed = located = illegible = empty_boxed = empty_total = 0
    digits = Counter()
    per_file: list[dict[str, Any]] = []
    check_hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    holes_total = placed_page = placed_prov = not_placed = 0
    offsets_independent: list[float] = []
    matches = independent = signatures = 0

    for rep in reports:
        f = rep["summary"]["file_num"]
        f_printed = f_located = 0
        for vid, val in rep["values"].items():
            if val["kind"] != "extracted":
                continue
            lin = val.get("lineage") or {}
            if val.get("as_printed") is None:
                empty_total += 1
                if lin.get("bbox"):
                    empty_boxed += 1
                if val.get("value") is None and (val.get("note") or "").startswith("illegible"):
                    illegible += 1
            else:
                printed += 1
                f_printed += 1
                if lin.get("quote_located"):
                    located += 1
                    f_located += 1
            for chk in lin.get("validators", []):
                check_hits[chk["id"]].append({
                    "value_id": vid, "file_num": f, "page": lin.get("page"),
                    "message": chk.get("message", ""), "outcome": chk.get("outcome"),
                    "class_a": bool(chk.get("class_a")),
                })
        for h in rep["holes"]:
            holes_total += 1
            pos = h.get("position")
            if not pos:
                not_placed += 1
            elif str(pos.get("source", "")).startswith("extracted"):
                placed_page += 1
            else:
                placed_prov += 1
            for m in h.get("matches", []):
                matches += 1
                if m.get("datum_shift_signature"):
                    signatures += 1
                off = rep["values"].get(m["offset_m"])
                note = (off or {}).get("note") or ""
                if off and isinstance(off.get("value"), (int, float)) and "not a measure" not in note:
                    independent += 1
                    offsets_independent.append(float(off["value"]))
        per_file.append({
            "file_num": f,
            "values": v(f"e:file:{f}:values", f_printed, note=f"printed values extracted from {f}"),
            "located": v(f"e:file:{f}:located", f_located, note=f"printed values in {f} whose quote was located on the page"),
            "holes": v(f"e:file:{f}:holes", len(rep["holes"]), note=f"holes assembled from {f}"),
            "tables": v(f"e:file:{f}:tables", len(rep["tables"]), note=f"tables read from {f}"),
        })

    # ---------- checks ----------
    checks = []
    for vid, hits in sorted(check_hits.items(), key=lambda kv: -len(kv[1])):
        meta = REGISTRY.get(vid)
        class_a = [h for h in hits if h["class_a"]]
        checks.append({
            "id": vid,
            "title": meta.title if meta else vid,
            "severity": meta.severity if meta else "warn",
            "flags": v(f"e:check:{vid}:flags", len(hits), note=f"values flagged by {vid}"),
            "class_a": v(f"e:check:{vid}:class_a", len(class_a), note=f"class-A failures raised by {vid}"),
            "examples": [
                {"value_id": h["value_id"], "file_num": h["file_num"], "page": h["page"], "message": h["message"]}
                for h in (class_a or hits)[:3]
            ],
        })

    # digit agreement (transcription against the OCR reading) is recorded in the store, not in the web lineage
    db = PATHS.data / "ue.duckdb"
    if db.is_file():
        import duckdb

        con = duckdb.connect(str(db), read_only=True)
        try:
            for kind, n in con.execute(
                "select digit_agreement, count(*) from read.field_value group by 1"
            ).fetchall():
                digits[kind or "na"] += int(n)
        finally:
            con.close()

    # ---------- cost ----------
    calls = [r for r in _runs() if r.get("status") == "ok"]
    cost = sum(float(r.get("cost_usd") or 0) for r in calls)
    tokens_in = sum((r.get("tokens") or {}).get("cache_creation", 0) + (r.get("tokens") or {}).get("cache_read", 0) + (r.get("tokens") or {}).get("input", 0) for r in calls)
    tokens_out = sum((r.get("tokens") or {}).get("output", 0) for r in calls)
    seconds = sum(float(r.get("duration_s") or 0) for r in calls)
    models = sorted({r.get("model_resolved") or "" for r in calls} - {""})
    pages_read = len({(r.get("file_num"), r.get("page_no")) for r in calls})

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "pipeline_version": __version__,
        "gold": None,  # no labels yet: the page must not show accuracy
        "caveats": CAVEATS,
        "run": {
            "models": models,
            "run_ids": sorted({r.get("run_id") for r in calls if r.get("run_id")}),
            "calls": v("e:run:calls", len(calls), note="model calls that returned a usable answer"),
            "pages_read": v("e:run:pages_read", pages_read, note="distinct pages sent to the model"),
            "cost_usd": v("e:run:cost_usd", round(cost, 2), fmt="m2", note="list-price equivalent of the calls in this run log"),
            "tokens_in": v("e:run:tokens_in", tokens_in, note="input tokens including cache reads and creation"),
            "tokens_out": v("e:run:tokens_out", tokens_out, note="output tokens"),
            "minutes": v("e:run:minutes", round(seconds / 60, 1), fmt="m1", note="model time, summed over calls"),
        },
        "coverage": {
            "files_read": v("e:cov:files_read", len(reports), note="assessment files read by this run"),
            "pages_read": v("e:cov:pages_read", pages_read, note="pages sent to the model"),
            "tables": v("e:cov:tables", sum(len(r["tables"]) for r in reports), note="tables read"),
            "holes": v("e:cov:holes", holes_total, note="holes assembled"),
        },
        "reading": {
            "printed_values": v("e:read:printed", printed, note="values the page prints, extracted"),
            "located": v("e:read:located", located, note="printed values whose quote was found on the page"),
            "located_share": v("e:read:located_share", round(located / printed, 3) if printed else 0, fmt="ratio3", note="located quotes over printed values"),
            "empty_cells": v("e:read:empty_cells", empty_total, note="cells the page leaves empty, recorded as not printed"),
            "empty_cells_boxed": v("e:read:empty_cells_boxed", empty_boxed, note="empty cells with a synthesised box on the page"),
            "illegible": v("e:read:illegible", illegible, note="cells the model marked illegible"),
            "digit_exact": v("e:read:digit_exact", digits.get("exact", 0), note="values whose digits match the second reader (OCR)"),
            "digit_confusable": v("e:read:digit_confusable", digits.get("confusable", 0), note="values matching the second reader only after folding confusable characters"),
            "digit_mismatch": v("e:read:digit_mismatch", digits.get("mismatch", 0), note="values whose digits differ from the second reader"),
        },
        "placement": {
            "from_page": v("e:place:from_page", placed_page, note="holes placed from coordinates printed on the page"),
            "from_provincial": v("e:place:from_provincial", placed_prov, note="holes placed at a provincial position, not from the page"),
            "not_placed": v("e:place:not_placed", not_placed, note="holes that could not be placed at all"),
        },
        "crosscheck": {
            "matches": v("e:xc:matches", matches, note="provincial records matched to extracted holes"),
            "independent": v("e:xc:independent", independent, note="matches where the collar was placed from the page, so the offset means something"),
            "median_offset_m": v("e:xc:median_m", round(statistics.median(offsets_independent), 2) if offsets_independent else 0, fmt="m2", note="median independent offset"),
            "max_offset_m": v("e:xc:max_m", round(max(offsets_independent), 2) if offsets_independent else 0, fmt="m2", note="largest independent offset"),
            "datum_shift_signatures": v("e:xc:signatures", signatures, note="offsets that look like a datum shift"),
        },
        "checks": checks,
        "per_file": per_file,
        "values": registry(*vals),
    }
    summary["build_id"] = short(sha256_json(summary["generated_at"]))
    return summary


def export(log: Any = print) -> Path:
    s = build()
    out = PATHS.web_data / "eval" / "run_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(s, indent=1) + "\n")
    files = s["values"][s["coverage"]["files_read"]]["value"]
    log(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB); "
        f"{files} files, {len(s['checks'])} checks with flags, no gold labels yet")
    return out

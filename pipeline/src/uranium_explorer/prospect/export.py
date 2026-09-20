"""`ue prospect export`: the readiness scorecard and the coverage layer, for the web app.

Nothing here is a score. This publishes what the data can and cannot support: how much of the basin each
feature actually covers, what each source is and under which licence, what is missing entirely, and a per-cell
count of how many features have a real observation behind them. The map layer it writes is a *coverage* map,
not a prospectivity map, and the page says so.

Every number goes out as a stored value, so the web app can print it through the same `<V>` component as the
rest of the project and no digit appears on screen without an id behind it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Callable

from ..ids import sha256_json, short
from ..paths import PATHS
from ..store import connect
from ..values import registry, stat
from .inventory import load as load_inventory
from .readiness import THIN_COVERAGE, table

SCHEMA_VERSION = "1.0.0"

CAVEATS = [
    "This is a coverage map, not a prospectivity map: it shows where measurements exist, not where uranium is.",
    "A feature covering a small share of the basin cannot carry a basin-wide model: what it would mostly encode is where people sampled.",
    "Absence of a measurement is not absence of the thing measured; cells with no observation are drawn as gaps, not as low values.",
    "The single most-used Athabasca datasets, airborne magnetics, radiometrics and gravity, are not published as grids by URL and are missing here entirely.",
]


def _feature_rows(vals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    df = table()
    out = []
    for _, r in df.iterrows():
        key = str(r["feature_key"])
        cov = float(r["coverage"]) if r["coverage"] == r["coverage"] else 0.0
        covered = int(r["covered"]) if r["covered"] == r["covered"] else 0
        median_obs = r["median_obs"]
        median_value = r["median_value"]
        vals.append(stat(f"c:cov:{key}", round(cov, 3), fmt="ratio3",
                         note=f"share of cells with an observation behind {key}"))
        vals.append(stat(f"c:covn:{key}", covered, note=f"cells with an observation behind {key}"))
        row: dict[str, Any] = {
            "feature_key": key,
            "title": str(r["title"]) if r["title"] == r["title"] else key,
            "bears_on": str(r["bears_on"]) if r["bears_on"] == r["bears_on"] else "",
            "is_effort": bool(r["is_effort"]),
            "unit": (str(r["unit"]) if r["unit"] == r["unit"] else None),
            "thin": bool(r["thin"]),
            "coverage": f"c:cov:{key}",
            "covered_cells": f"c:covn:{key}",
            "median_obs": None,
            "median_value": None,
            "notes": str(r["notes"]) if r["notes"] == r["notes"] else "",
            "sources": json.loads(r["source_keys"]) if isinstance(r["source_keys"], str) else [],
        }
        if median_obs == median_obs:
            vals.append(stat(f"c:obs:{key}", round(float(median_obs), 1), fmt="m1",
                             note=f"typical number of observations behind one {key} value"))
            row["median_obs"] = f"c:obs:{key}"
        if median_value == median_value:
            vals.append(stat(f"c:med:{key}", round(float(median_value), 2), fmt="m2",
                             note=f"median {key} across cells that have a value"))
            row["median_value"] = f"c:med:{key}"
        out.append(row)
    return out


def _coverage_geojson(vals: list[dict[str, Any]]) -> dict[str, Any]:
    """One point per cell carrying how many features have a real observation there.

    Points rather than polygons: 30,000 squares is a large payload for a browser, and the map draws this as a
    coverage density, which a point grid renders honestly at every zoom.
    """
    con = connect(read_only=True)
    try:
        rows = con.execute(
            """
            with spec as (select feature_key, is_effort from derived.feature_spec),
                 obs as (
                   select f.cell_id,
                          count(*) filter (where f.n_obs > 0 and not s.is_effort)  as geo,
                          count(*) filter (where f.n_obs > 0 and s.is_effort)      as effort
                   from derived.cell_feature f join spec s using (feature_key)
                   group by 1
                 )
            select c.cell_id, round(c.lon, 4) as lon, round(c.lat, 4) as lat, c.in_basin,
                   coalesce(o.geo, 0) as geo, coalesce(o.effort, 0) as effort
            from derived.cell c left join obs o using (cell_id)
            order by c.cell_id
            """
        ).fetchall()
        n_geo = con.execute(
            "select count(*) from derived.feature_spec where not is_effort"
        ).fetchone()[0]
        n_effort = con.execute("select count(*) from derived.feature_spec where is_effort").fetchone()[0]
    finally:
        con.close()
    vals.append(stat("c:grid:geo_features", int(n_geo), note="geological features built per cell"))
    vals.append(stat("c:grid:effort_features", int(n_effort), note="exploration-effort features per cell"))
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": i,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                # g: geological features with an observation here, e: effort features, b: over the sandstone
                "properties": {"g": int(geo), "e": int(effort), "b": 1 if in_basin else 0},
            }
            for i, (_cell_id, lon, lat, in_basin, geo, effort) in enumerate(rows)
        ],
    }


def _scores_geojson(vals: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """One point per cell carrying the three scores, and the metric table that says what they are worth."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            """
            select c.cell_id, round(c.lon, 4), round(c.lat, 4), c.in_basin,
                   max(case when s.model = 'criteria' then s.score end)  as criteria,
                   max(case when s.model = 'learned'  then s.score end)  as learned,
                   max(case when s.model = 'effort'   then s.score end)  as effort,
                   max(case when s.model = 'criteria' then s.known_share end) as known_share,
                   any_value(l.label_tier)
            from derived.cell c
            left join derived.cell_score s using (cell_id)
            left join derived.cell_label l using (cell_id)
            group by 1, 2, 3, 4 order by 1
            """
        ).fetchall()
        # the run id of the scores run: MLflow ids (hex) and timestamps share this table, so "the latest run"
        # is the latest among the three-score rows, not the lexically largest id of any run
        run = con.execute(
            "select max(run_id) from derived.metric where split_part(metric_key, '.', 1) in ('criteria', 'learned', 'effort')"
        ).fetchone()[0]
        metrics = con.execute(
            "select metric_key, value, note from derived.metric where run_id = ? "
            "and split_part(metric_key, '.', 1) in ('criteria', 'learned', 'effort') order by metric_key", [run]
        ).fetchall()
    finally:
        con.close()

    tiers = {"deposit": 2, "occurrence": 1}
    features = []
    for i, (cid, lon, lat, in_basin, crit, learned, effort, known, tier) in enumerate(rows):
        # the cell id travels with the point: the map addresses a cell by name rather than by reprojecting
        # its centre back into the grid's CRS and hoping the arithmetic agrees
        props: dict[str, Any] = {"cid": cid, "b": 1 if in_basin else 0, "t": tiers.get(tier, 0)}
        for key, value in (("c", crit), ("l", learned), ("e", effort), ("k", known)):
            if value is not None:
                props[key] = round(float(value), 3)
        # the difference view is computed here rather than in the map, so the number the map colours and the
        # number a panel prints come from the same arithmetic
        if learned is not None and effort is not None:
            props["d"] = round(float(learned) - float(effort), 3)
        features.append({"type": "Feature", "id": i,
                         "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props})

    table = []
    for key, value, note in metrics:
        model, fold, metric = key.split(".", 2)
        vid = f"c:m:{key}"
        vals.append(stat(vid, round(float(value), 4), fmt="ratio3", note=note))
        table.append({"model": model, "fold": fold, "metric": metric, "value_id": vid})
    return {"type": "FeatureCollection", "features": features}, {"run_id": run, "rows": table}



def _num(x: Any) -> float | None:
    """A finite number or None: the run JSONs carry NaN as null and numpy floats as floats."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _interval(vals: list[dict[str, Any]], vid: str, ci: Any, note: str) -> list[str] | None:
    """The two bounds of a bootstrap interval as values of their own, so the page can print them."""
    if not isinstance(ci, (list, tuple)) or len(ci) != 2:
        return None
    lo, hi = _num(ci[0]), _num(ci[1])
    if lo is None or hi is None:
        return None
    vals.append(stat(f"{vid}.lo", round(lo, 4), fmt="ratio3", note=f"lower bound of the 95% interval; {note}"))
    vals.append(stat(f"{vid}.hi", round(hi, 4), fmt="ratio3", note=f"upper bound of the 95% interval; {note}"))
    return [f"{vid}.lo", f"{vid}.hi"]


#: the metrics the Eval page prints per configuration or arm; the rest stay in the tracker
PHASE_METRICS = ("pr_auc", "roc_auc", "capture_top10", "base_rate")


def _headline_block(vals: list[dict[str, Any]], d: dict[str, Any]) -> dict[str, Any]:
    """Phase 0: every configuration of the effort-against-geology re-test, with intervals and the verdict."""
    run_id = d.get("mlflow_run_id") or d["run_id"]
    rows = []
    for r in d.get("rows") or []:
        if _num(r.get("pr_auc")) is None:
            continue
        key = r["config"]
        note = f"{r['feature_set']} features, positives={r['positives']}, matched={r['matched']}, thinned={r['thinned']}, {r['fold']} folds"
        for metric in PHASE_METRICS:
            v = _num(r.get(metric))
            if v is None:
                continue
            vid = f"c:h:{key}.{metric}"
            vals.append(stat(vid, round(v, 4), fmt="ratio3", note=f"{metric}; {note}"))
            entry: dict[str, Any] = {"config": key, "feature_set": r["feature_set"], "positives": r["positives"],
                                     "matched": bool(r["matched"]), "thinned": bool(r["thinned"]), "fold": r["fold"],
                                     "metric": metric, "run_id": run_id, "value_id": vid}
            ci = _interval(vals, vid, r.get(f"{metric}_ci"), f"{metric}; {note}")
            if ci:
                entry["ci"] = ci
            rows.append(entry)
    minetrace = []
    for fs, mt in (d.get("minetrace") or {}).items():
        for metric in ("roc_auc_mean", "roc_auc_sd"):
            v = _num(mt.get(metric))
            if v is None:
                continue
            vid = f"c:h:minetrace.{fs}.{metric}"
            vals.append(stat(vid, round(v, 4), fmt="ratio3", note=str(mt.get("protocol") or "MineTRACE protocol")))
            minetrace.append({"feature_set": fs, "metric": metric, "run_id": run_id, "value_id": vid})
    cells = _num(d.get("cells"))
    if cells is not None:
        vals.append(stat("c:h:cells", int(cells), note="common complete cases the re-test scored"))
    verdict = d.get("verdict") or {}
    return {"run_id": d["run_id"], "mlflow_run_id": d.get("mlflow_run_id"), "store_sha256": d.get("store_sha256"),
            "snapshot": d.get("snapshot"), "cells": "c:h:cells" if cells is not None else None,
            "verdict": verdict.get("text") if isinstance(verdict, dict) else (str(verdict) if verdict else None),
            "rows": rows, "minetrace": minetrace}


def _search_block(vals: list[dict[str, Any]], d: dict[str, Any]) -> dict[str, Any]:
    """Phase 3: every arm of the model search with its own tracker run id, and the served-model decision."""
    rows = []
    for r in d.get("rows") or []:
        if _num(r.get("pr_auc")) is None:
            continue
        key = r["arm"]
        run_id = r.get("run_id") or d["run_id"]
        note = f"{r['name']} on {r['feature_set']} under {r['fold']} folds, positives={r.get('positives', 'all')}"
        for metric in PHASE_METRICS:
            v = _num(r.get(metric))
            if v is None:
                continue
            vid = f"c:s:{key}.{metric}"
            vals.append(stat(vid, round(v, 4), fmt="ratio3", note=f"{metric}; {note}"))
            entry: dict[str, Any] = {"arm": key, "name": r["name"], "feature_set": r["feature_set"], "fold": r["fold"],
                                     "positives": str(r.get("positives", "all")), "metric": metric, "run_id": run_id,
                                     "value_id": vid}
            ci = _interval(vals, vid, r.get(f"{metric}_ci"), f"{metric}; {note}")
            if ci:
                entry["ci"] = ci
            rows.append(entry)
    dec = d.get("decision") or None
    decision = None
    if isinstance(dec, dict) and dec.get("reason"):
        decision = {"model": dec.get("model") or dec.get("name"), "stage": dec.get("stage"),
                    "served": bool(dec.get("served", False)), "reason": str(dec["reason"]),
                    "run_id": dec.get("run_id"), "version": dec.get("version"), "card": None}
        # the model card, from the tracker's own row for the registered run: what it was fitted on and how
        arm = next((r for r in d.get("rows") or [] if dec.get("run_id") and r.get("run_id") == dec.get("run_id")), None)
        if arm is not None:
            card: dict[str, Any] = {"name": arm["name"], "feature_set": arm["feature_set"], "fold": arm["fold"],
                                    "positives": str(arm.get("positives", "all")), "matched": bool(arm.get("matched", True)),
                                    "thinned": bool(arm.get("thinned", True)), "features": [str(f) for f in arm.get("features") or []],
                                    "n_pos": None, "scored": None}
            for k in ("n_pos", "scored"):
                v = _num(arm.get(k))
                if v is not None:
                    vals.append(stat(f"c:s:card.{k}", int(v), note=f"{k} for the registered model's run"))
                    card[k] = f"c:s:card.{k}"
            decision["card"] = card
    cells = _num(d.get("cells"))
    if cells is not None:
        vals.append(stat("c:s:cells", int(cells), note="common complete cases the model search scored"))
    return {"run_id": d["run_id"], "store_sha256": d.get("store_sha256"), "snapshot": d.get("snapshot"),
            "quick": bool(d.get("quick", False)), "cells": "c:s:cells" if cells is not None else None,
            "rows": rows, "decision": decision}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def _hindcast_block(vals: list[dict[str, Any]], d: dict[str, Any]) -> dict[str, Any]:
    """Phase 3: where each later discovery ranked under each model frozen at a cutoff, as a share of area."""
    run_id = d["run_id"]
    rows = []
    for r in d.get("rows") or []:
        v = _num(r.get("area_share"))
        if v is None:
            continue
        slug = _slug(r["discovery"])
        vid = f"c:hc:{r['cutoff']}.{r['model']}.{slug}"
        vals.append(stat(vid, round(v, 4), fmt="pct1",
                         note=f"share of basin area scoring at least the {r['discovery']} cell ({r.get('year')}) "
                              f"by the {r['model']} model frozen at {r['cutoff']}"))
        rows.append({"cutoff": int(r["cutoff"]), "model": r["model"], "discovery": slug, "title": str(r["discovery"]),
                     "year": int(r["year"]) if _num(r.get("year")) is not None else None,
                     "confidence": r.get("confidence"), "cells": list(r.get("cells") or []), "run_id": run_id,
                     "value_id": vid})
    summary = []
    for key, value in (d.get("summary") or {}).items():
        v = _num(value)
        if v is None or "." not in key:
            continue
        model, stat_key = key.split(".", 1)
        vid = f"c:hc:summary.{model}.{stat_key}"
        fmt = "pct1" if "share" in stat_key else "int"
        vals.append(stat(vid, round(v, 4) if fmt == "pct1" else int(v), fmt=fmt,
                         note=f"{stat_key} for the {model} model over every cutoff and later discovery"))
        summary.append({"model": model, "key": stat_key, "run_id": run_id, "value_id": vid})
    return {"run_id": run_id, "store_sha256": d.get("store_sha256"), "snapshot": d.get("snapshot"),
            "rows": rows, "summary": summary}


GATE_COLUMNS = ("present", "licensed", "covers", "servable", "versioned")


def _gate_columns_block(out_dir: Path | None = None) -> dict[str, Any] | None:
    """The five-column readiness gate (PRD 9.1) as `ue prospect gate` last wrote it; absent until it has run.

    The notes are the gate's own status strings, not measurements, so they travel as text; the counts they
    mention are on this page already as values (the sources and features tables)."""
    p = (out_dir or (PATHS.out / "prospect")) / "gate.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    rows = []
    for r in d.get("rows") or []:
        row = {"dataset": str(r["dataset"]), "title": str(r.get("title") or r["dataset"]), "kind": str(r["kind"])}
        for c in GATE_COLUMNS:
            cell = r.get(c) or {}
            row[c] = {"ok": bool(cell.get("ok")), "note": str(cell.get("note") or "")}
        rows.append(row)
    return {"generated_at": str(d.get("generated_at") or ""), "store_sha256": d.get("store_sha256"),
            "snapshot": d.get("snapshot"), "green": bool(d.get("green")), "rows": rows,
            "failures": [str(f) for f in d.get("failures") or []]}


def _phase_blocks(vals: list[dict[str, Any]], out_dir: Path | None = None) -> dict[str, Any]:
    """The Phase 0 re-test, the model search and the dated hindcast, each from the JSON its run wrote.

    Every number becomes a value with an id, and every row carries the tracker run id it came from, so the
    Eval page can name the run behind each figure. A block is absent, not empty, when its run has not happened."""
    out_dir = out_dir or (PATHS.out / "prospect")
    blocks: dict[str, Any] = {}
    for name, builder in (("headline", _headline_block), ("search", _search_block), ("hindcast", _hindcast_block)):
        p = out_dir / ("modelsearch.json" if name == "search" else f"{name}.json")
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("run_id"):
            blocks[name] = builder(vals, d)
    return blocks


#: the metrics the Eval page prints per benchmark row; the rest stay in table.json and the tracker
BENCH_METRICS = ("f1", "precision", "recall", "pr_auc", "roc_auc", "pr_auc_all", "roc_auc_all", "ece", "abstain_rate")
#: what only an arm can report, with the formatter each takes: the gate, the probes, and what a cell cost
BENCH_EXTRA = {"gate_rejection_rate": "ratio3", "probe_abstain_rate": "ratio3", "cost_usd_per_cell": "m2",
               "latency_s_per_cell": "m1"}
BENCH_STRATA = ("deposit", "occurrence", "negative")
#: the staged loop's per-stage columns (PRD §8.5) as the table names them without their `stage_` prefix, each
#: with its formatter and what it counts. Only a staged arm has them: a v0 arm answers in one call and carries
#: none, and a column identical across every arm so far (the shallow share, refusals per rule) stays in table.json
BENCH_STAGES = {
    "n_chains": ("int", "chains the staged loop ran; the denominator of the other stage columns"),
    "gate_rejection_rate": ("ratio3", "node gate refusals over executor attempts"),
    "valid_rate": ("ratio3", "share of chains a verifier round validated"),
    "verifier_catch_rate": ("ratio3", "share of chains the verifier refused at least once"),
    "rounds_to_valid_mean": ("m2", "verifier rounds per chain, over the chains that validated"),
    "reexecuted_mean": ("m2", "nodes re-executed on the verifier's feedback, per chain"),
    "verifier_agreement_rate": ("ratio3", "share of chains whose verifier label matched the final verdict"),
    "decider_agreement_rate": ("ratio3", "share of chains where the weighted sum and the adjudicator fell on the "
                                         "same side of the threshold"),
}


def _version_key(name: str) -> list[Any]:
    """`v10` sorts after `v2`: the digits of a version name compare as numbers, the rest as text."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", name) if p]


def _bench_block(vals: list[dict[str, Any]], out_dir: Path | None = None) -> dict[str, Any] | None:
    """The analyst benchmark table (`ue bench table`): every arm and every baseline scored on the same open
    cells of the frozen benchmark, from the highest version that has a table.

    Every number becomes a value under `c:bench:<version>:<row>:<metric>` (`.lo`/`.hi` for an interval's
    bounds, `:<stratum>:<key>` for a stratum, `:stage:<key>` for one of the staged loop's per-stage columns),
    so the page prints the row through `<V>` like any other. A row
    scored on no cells, an arm still running, is left out: it has nothing to print yet. Absent, not empty,
    until a table has been written."""
    root = out_dir or (PATHS.out / "bench")
    tables = {p.parent.name: p for p in root.glob("*/table.json")} if root.is_dir() else {}
    if not tables:
        return None
    versions = sorted(tables, key=_version_key)
    version = versions[-1]
    try:
        d = json.loads(tables[version].read_text())
    except json.JSONDecodeError:
        return None
    rows = []
    for r in d.get("rows") or []:
        n = _num(r.get("n_cells", r.get("n")))
        if not n:
            continue
        name, kind = str(r["name"]), str(r.get("kind"))
        pre = f"c:bench:{version}:{name}"
        note = f"{kind} {name} ({r.get('model')}) on {int(n)} open cells of benchmark {version}"
        vals.append(stat(f"{pre}:n", int(n), note=f"labelled open cells scored; {note}"))
        row: dict[str, Any] = {"name": name, "kind": kind, "model": r.get("model"),
                               "effort": r.get("effort"), "n": f"{pre}:n",
                               "n_pos": None, "n_neg": None, "run_id": r.get("run_id"),
                               "mlflow_run_id": r.get("mlflow_run_id"), "metrics": {}}
        for key, what in (("n_pos", "positive cells"), ("n_neg", "negative cells")):
            v = _num(r.get(key))
            if v is not None:
                vals.append(stat(f"{pre}:{key}", int(v), note=f"{what}; {note}"))
                row[key] = f"{pre}:{key}"
        if r.get("note"):
            row["note"] = str(r["note"])
        ci: dict[str, list[str]] = {}
        for metric in BENCH_METRICS:
            v = _num(r.get(metric))
            if v is None:
                continue
            vid = f"{pre}:{metric}"
            vals.append(stat(vid, round(v, 4), fmt="ratio3", note=f"{metric}; {note}"))
            row["metrics"][metric] = vid
            bounds = _interval(vals, vid, r.get(f"{metric}_ci"), f"{metric}; {note}")
            if bounds:
                ci[metric] = bounds
        if ci:
            row["ci"] = ci
        if kind == "arm":
            extra: dict[str, str] = {}
            for key, fmt in BENCH_EXTRA.items():
                v = _num(r.get(key))
                if v is None:
                    continue
                vid = f"{pre}:{key}"
                vals.append(stat(vid, round(v, 4), fmt=fmt, note=f"{key}; {note}",
                                 unit="s" if key.startswith("latency") else None))
                extra[key] = vid
            if extra:
                row["extra"] = extra
            stages: dict[str, str] = {}
            chains = _num(r.get("stage_n_chains"))
            for key, (fmt, what) in BENCH_STAGES.items():
                v = _num(r.get(f"stage_{key}"))
                if v is None:
                    continue
                vid = f"{pre}:stage:{key}"
                over = f"; over {int(chains)} chains" if chains and key != "n_chains" else ""
                vals.append(stat(vid, int(v) if fmt == "int" else round(v, 4), fmt=fmt, note=f"{what}{over}; {note}"))
                stages[key] = vid
            if stages:
                row["stages"] = stages
        strata: dict[str, dict[str, str]] = {}
        for stratum, cell in (r.get("strata") or {}).items():
            if stratum not in BENCH_STRATA or not isinstance(cell, dict):
                continue
            entry: dict[str, str] = {}
            for key, fmt in (("n", "int"), ("accuracy", "ratio3"), ("abstain_rate", "ratio3")):
                v = _num(cell.get(key))
                if v is None:
                    continue
                vid = f"{pre}:{stratum}:{key}"
                vals.append(stat(vid, int(v) if fmt == "int" else round(v, 4), fmt=fmt,
                                 note=f"{key} over the {stratum} cells; {note}"))
                entry[key] = vid
            if entry:
                strata[stratum] = entry
        if strata:
            row["strata"] = strata
        rows.append(row)
    return {"version": version, "manifest_sha256": d.get("manifest_sha256"), "computed_at": d.get("computed_at"),
            "rows": rows, "versions": versions[:-1]}


def _gate_block(vals: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What the gate scored on the adversarial suite, if it has been run.

    Published as Vals like everything else, because a number about the checker is still a number on the screen
    and gets no exemption from the rule it exists to enforce.
    """
    path = Path("data/exports/gate_eval.json")
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    session = report["conditions"]["production"]
    vals += [
        stat("c:gate:cases", int(report["cases"]), note="claims put to the gate"),
        stat("c:gate:put", int(session["fabrications_put"]), note="corrupted claims in the suite"),
        stat("c:gate:caught", int(session["fabrications_caught"]), note="corrupted claims the gate refused"),
        stat("c:gate:missed", int(session["fabrications_missed"]), note="corrupted claims that got through"),
        stat("c:gate:caught_rate", float(session["caught_rate"]), fmt="ratio3",
             note="share of corrupted claims the gate refused"),
        stat("c:gate:honest", int(session["honest_put"]), note="true claims in the suite"),
        stat("c:gate:wrongly_rejected", int(session["honest_rejected"]),
             note="true claims the gate refused"),
    ]
    kinds: dict[str, Any] = {}
    for kind, row in sorted(report["by_kind"].items()):
        vals += [
            stat(f"c:gate:{kind}:caught", int(row["caught"]), note=f"{kind} claims the gate refused"),
            stat(f"c:gate:{kind}:missed", int(row["missed"]), note=f"{kind} claims that got through"),
        ]
        kinds[kind] = {"caught": f"c:gate:{kind}:caught", "missed": f"c:gate:{kind}:missed"}
    return {
        "run_id": report["run_id"],
        "cells": report["cells"],
        "cases": "c:gate:cases",
        "put": "c:gate:put",
        "caught": "c:gate:caught",
        "missed": "c:gate:missed",
        "caught_rate": "c:gate:caught_rate",
        "honest": "c:gate:honest",
        "wrongly_rejected": "c:gate:wrongly_rejected",
        "by_kind": kinds,
        "escaped": report["escaped"],
    }


def build(log: Callable[[str], None] = print) -> dict[str, Any]:
    inv = load_inventory()
    vals: list[dict[str, Any]] = []

    con = connect(read_only=True)
    try:
        grid = con.execute(
            "select grid_id, cell_m, epsg, buffer_m, n_cells from derived.grid order by built_at desc limit 1"
        ).fetchone()
        in_basin = con.execute(
            "select count(*) from derived.cell where grid_id = ? and in_basin", [grid[0]]
        ).fetchone()[0] if grid else 0
    finally:
        con.close()
    if not grid:
        raise RuntimeError("no grid; run `ue prospect grid` then `ue prospect features`")
    grid_id, cell_m, epsg, buffer_m, n_cells = grid

    vals += [
        stat("c:grid:cells", int(n_cells), note="cells in the analysis grid"),
        stat("c:grid:cell_m", int(cell_m), note="cell size in metres", unit="m"),
        stat("c:grid:in_basin", int(in_basin), note="cells over the Athabasca sandstone"),
        stat("c:grid:area_km2", round(int(n_cells) * (cell_m / 1000) ** 2, 1), fmt="m1",
             note="area the grid covers", unit="km2"),
        stat("c:grid:buffer_km", round(buffer_m / 1000, 1), fmt="m1",
             note="how far outside the basin outline the grid reaches", unit="km"),
        stat("c:src:total", len(inv.sources), note="data sources in the register"),
        stat("c:src:verified_features", len(inv.usable_features()),
             note="feature sources confirmed by a live call"),
        stat("c:src:redistributable", sum(1 for s in inv.sources if s.redistributable),
             note="sources whose licence allows redistribution"),
        stat("c:src:gaps", len(inv.gaps), note="datasets that do not exist publicly, recorded as gaps"),
    ]

    features = _feature_rows(vals)
    coverage = _coverage_geojson(vals)
    scores, metrics = _scores_geojson(vals)
    gate = _gate_block(vals)
    phases = _phase_blocks(vals)
    bench = _bench_block(vals)
    gate_columns = _gate_columns_block()

    doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "grid": {
            "grid_id": grid_id, "epsg": int(epsg), "cells": "c:grid:cells", "cell_m": "c:grid:cell_m",
            "in_basin": "c:grid:in_basin", "area_km2": "c:grid:area_km2", "buffer_km": "c:grid:buffer_km",
        },
        "totals": {
            "sources": "c:src:total", "verified_features": "c:src:verified_features",
            "redistributable": "c:src:redistributable", "gaps": "c:src:gaps",
            "geo_features": "c:grid:geo_features", "effort_features": "c:grid:effort_features",
        },
        "thin_coverage_threshold": THIN_COVERAGE,
        "caveats": CAVEATS,
        "features": features,
        "metrics": metrics,
        **({"gate": gate} if gate else {}),
        **({"readiness_gate": gate_columns} if gate_columns else {}),
        **phases,
        **({"bench": bench} if bench else {}),
        "sources": [
            {
                "key": s.key, "title": s.title, "role": s.role, "bears_on": s.bears_on, "tier": s.tier,
                "access": s.access, "url": s.url, "licence": s.licence.name, "licence_url": s.licence.url,
                "redistributable": s.redistributable, "verified": s.verified, "verified_at": s.verified_at,
                "record_count": s.record_count, "notes": s.notes, "caveats": list(s.caveats),
            }
            for s in inv.sources
        ],
        "gaps": [
            {"key": g.key, "title": g.title, "status": g.status, "why_it_matters": g.why_it_matters,
             "evidence": g.evidence, "workaround": g.workaround}
            for g in inv.gaps
        ],
        "values": registry(*vals),
    }
    doc["build_id"] = short(sha256_json(doc["generated_at"]))

    # refuse to publish a scorecard that breaks the contract: an unbacked number or a label used as a feature
    from ..contract_check import check_readiness

    errors = check_readiness(doc)
    if errors:
        for err in list(errors)[:10]:
            log(f"  contract: {err}")
        raise ValueError(f"export refused: {len(errors)} contract violation(s); nothing was written")

    out_dir = PATHS.web_data / "prospect"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "readiness.json").write_text(json.dumps(doc, indent=1) + "\n")
    (out_dir / "coverage.geojson").write_text(json.dumps(coverage, separators=(",", ":")) + "\n")
    (out_dir / "scores.geojson").write_text(json.dumps(scores, separators=(",", ":")) + "\n")

    sizes = {p.name: p.stat().st_size for p in out_dir.iterdir()}
    log(f"  wrote {out_dir.relative_to(PATHS.root)}: "
        + ", ".join(f"{k} {v / 1024:.0f} KB" for k, v in sorted(sizes.items())))
    log(f"  {len(features)} features, {len(doc['sources'])} sources, {len(doc['gaps'])} gaps, "
        f"{len(doc['values'])} values")
    return {"features": len(features), "cells": int(n_cells), "values": len(doc["values"])}

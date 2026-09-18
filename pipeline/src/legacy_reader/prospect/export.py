"""`lr prospect export`: the readiness scorecard and the coverage layer, for the web app.

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
        run = con.execute("select max(run_id) from derived.metric").fetchone()[0]
        metrics = con.execute(
            "select metric_key, value, note from derived.metric where run_id = ? order by metric_key", [run]
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
        raise RuntimeError("no grid; run `lr prospect grid` then `lr prospect features`")
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

"""A small synthetic store for the benchmark tests: every table the tools and the builder read, filled from
one seeded frame, in a temporary DuckDB file. Nothing here touches the project's own store."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from shapely.geometry import box

from legacy_reader.prospect import models as M
from legacy_reader.store import append_frame, connect, write_meta

GRID_ID = "testgrid"
NOW = "2026-09-20T00:00:00+00:00"

#: (file_num, company, property, cell index the file sits at, holes, hole names)
FILES = [
    ("64L05-0060", "ASAMERA OIL CORPORATION LTD (Tenure Holder)", "Cluff Lake", 0, 355, "14-013, Q6-1, Q6-2"),
    ("74H09-0039", "SMDC (Tenure Holder)", None, 1, 20, "KL-101, KL-102"),
    ("MAW00509", "CAMECO CORPORATION (Tenure Holder); UEM INC (JV Partner)", "McArthur River", 2, 57, "MC-361, MC-362"),
    ("74G07-0064", "DENISON MINES CORP/JNR RESOURCES (Tenure Holder)", None, 3, 18, "WR-12, WR-13"),
]

PAGES = [
    ("64L05-0060", "annual report.pdf", "sha-a", 12,
     "Drilling by Asamera on the Cluff Lake property tested the graphitic conductor at the unconformity; hole "
     "Q6-1 intersected pitchblende with clay alteration in the sandstone above, near 58.3510 N."),
    ("74H09-0039", "logs.pdf", "sha-b", 3,
     "SMDC drilled the graphitic conductor beneath the unconformity in hole KL-101 on sheet 74H09 and logged "
     "uranium mineralization with hematite alteration."),
    ("MAW00509", "report.pdf", "sha-c", 7,
     "Cameco's drilling at McArthur River followed the P2 conductor along the unconformity; hole MC-361 "
     "returned uranium mineralization with strong alteration."),
]


def bench_frame(n_dep: int = 30, n_occ: int = 60, n_neg: int = 120, n_probe: int = 90, seed: int = 0) -> pd.DataFrame:
    """Cells with known strata pools: deposits in four camps, drilled occurrences, drilled unlabelled ground,
    and never-drilled ground. Positions are in a synthetic metric frame, 200 by 100 km."""
    rng = np.random.default_rng(seed)
    n = n_dep + n_occ + n_neg + n_probe
    tier = np.array(["deposit"] * n_dep + ["occurrence"] * n_occ + ["unlabelled"] * (n_neg + n_probe))
    holes = np.concatenate([
        rng.integers(5, 80, n_dep), rng.integers(1, 40, n_occ), rng.integers(5, 60, n_neg), np.zeros(n_probe, dtype=int),
    ]).astype(float)
    # deposits cluster in four camps of about 25 km so the thinning has something to thin and something left
    camps = np.array([(20_000, 20_000), (150_000, 70_000), (80_000, 10_000), (170_000, 20_000)], dtype=float)
    at = camps[rng.integers(0, len(camps), n_dep)]
    cx = np.concatenate([at[:, 0] + rng.uniform(0, 25_000, n_dep), rng.uniform(0, 200_000, n - n_dep)])
    cy = np.concatenate([at[:, 1] + rng.uniform(0, 25_000, n_dep), rng.uniform(0, 100_000, n - n_dep)])
    positive = tier != "unlabelled"
    d_conductor = np.where(positive, rng.uniform(0, 2_500, n), rng.uniform(0, 20_000, n))
    df = pd.DataFrame({
        "cell_id": [f"{i // 100:04d}_{i % 100:04d}" for i in range(n)], "label_tier": tier,
        "label_name": np.where(tier == "deposit", "Cigar Lake", np.where(tier == "occurrence", "Rabbit Lake", "")),
        "camp_id": np.where(positive, (cx > 100_000).astype(int), -1),
        "block_id": (np.floor(cx / 30_000) * 100 + np.floor(cy / 30_000)).astype(int),
        "lon": -108 + cx / 60_000, "lat": 57 + cy / 111_000, "cx": cx, "cy": cy,
        "d_conductor_m": d_conductor, "d_fault_m": rng.uniform(0, 20_000, n), "fault_density": rng.random(n),
        "graphitic_host": rng.integers(0, 2, n).astype(float), "unconformity_depth_m": rng.uniform(50, 900, n),
        "water_fraction": rng.random(n), "vegetation_fraction": rng.random(n), "bare_fraction": rng.random(n),
        "elevation_m": rng.uniform(200, 600, n), "relief_m": rng.uniform(0, 200, n),
        "holes_n": holes, "holes_first_year": np.where(holes > 0, rng.uniform(1950, 2020, n), np.nan),
        "sed_samples_n": rng.integers(0, 9, n).astype(float), "boulder_samples_n": rng.integers(0, 4, n).astype(float),
        "airborne_surveys_n": rng.integers(0, 5, n).astype(float), "ground_surveys_n": rng.integers(0, 5, n).astype(float),
    })
    df["criteria_score"] = (1.0 - df["d_conductor_m"] / 20_000).clip(0, 1).round(4)
    return df


def fake_fit(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
    """Scores by the first column, as the headline tests do: no scikit-learn in the loop."""
    col = x_test[:, 0]
    span = np.ptp(col) or 1.0
    scaled = (col - col.min()) / span
    return 1.0 - scaled if col.max() > 100 else scaled


def make_bench_store(path: Path, df: pd.DataFrame | None = None, cell_m: float = 2000.0) -> pd.DataFrame:
    """Write the frame into a fresh store at `path` and return the frame."""
    df = bench_frame() if df is None else df
    con = connect(path)
    try:
        write_meta(con, "test")
        append_frame(con, "derived", "grid", pd.DataFrame([{
            "grid_id": GRID_ID, "cell_m": int(cell_m), "epsg": 2957, "buffer_m": 0, "extent_wkt": "POLYGON EMPTY",
            "n_cells": len(df), "built_at": NOW}]), "derived")
        half = cell_m / 2
        append_frame(con, "derived", "cell", pd.DataFrame({
            "cell_id": df["cell_id"], "grid_id": GRID_ID, "col": range(len(df)), "row": 0,
            "cx": df["cx"], "cy": df["cy"], "lon": df["lon"], "lat": df["lat"],
            "geom_wkb": [box(x - half, y - half, x + half, y + half).wkb for x, y in zip(df["cx"], df["cy"], strict=True)],
            "in_basin": True}), "derived")
        append_frame(con, "derived", "cell_label", pd.DataFrame({
            "cell_id": df["cell_id"], "label_tier": df["label_tier"], "label_name": df["label_name"],
            "camp_id": df["camp_id"], "block_id": df["block_id"], "computed_at": NOW}), "derived")
        keys = [*M.LEARNED_FEATURES, *M.EFFORT_FEATURES]
        specs = pd.DataFrame([{
            "feature_key": k, "title": k.replace("_", " "), "unit": "m" if k.endswith("_m") else None,
            "from_tier": "native", "source_keys": json.dumps(["em_conductors"]), "bears_on": "pathway",
            "is_effort": k in M.EFFORT_FEATURES, "is_label": False, "is_count": k.endswith("_n"), "notes": None,
        } for k in keys])
        append_frame(con, "derived", "feature_spec", specs, "derived")
        long = df.melt(id_vars=["cell_id"], value_vars=keys, var_name="feature_key", value_name="value")
        long["value_text"] = None
        long["unit"] = long["feature_key"].map(lambda k: "m" if k.endswith("_m") else None)
        long["n_obs"] = np.where(long["value"].notna(), 1, 0)
        long["nearest_m"] = np.where(long["value"].isna(), 4200.0, np.nan)
        long["from_tier"] = "native"
        long["op"] = "test"
        long["tool"] = "test"
        long["params"] = None
        long["inputs"] = None
        long["computed_at"] = NOW
        append_frame(con, "derived", "cell_feature", long, "derived")
        append_frame(con, "derived", "cell_score", pd.DataFrame({
            "cell_id": df["cell_id"], "model": "criteria", "score": df["criteria_score"], "known_share": 1.0,
            "in_aoa": True, "params": None, "computed_at": NOW}), "derived")
        crit = []
        for cid, d, g in zip(df["cell_id"], df["d_conductor_m"], df["graphitic_host"], strict=True):
            m = float(np.clip((5000 - d) / 4500, 0, 1))
            crit += [
                {"cell_id": cid, "criterion": "conductor_proximity", "membership": m, "weight": 3.0, "contribution": 3 * m},
                {"cell_id": cid, "criterion": "graphitic_host", "membership": float(g), "weight": 2.0, "contribution": 2 * g},
                {"cell_id": cid, "criterion": "lake_water_uranium", "membership": None, "weight": 1.0, "contribution": None},
            ]
        append_frame(con, "derived", "cell_criterion", pd.DataFrame(crit).assign(computed_at=NOW), "derived")
        append_frame(con, "native", "corpus_file", pd.DataFrame([{
            "file_num": f, "company": c, "property": p, "work_period": "1979-80", "nts": "74-H-09",
            "work_description": "Diamond drilling", "lon": float(df["lon"].iat[i]), "lat": float(df["lat"].iat[i]),
            "n_holes": h, "hole_names": names, "retrieved_at": NOW} for f, c, p, i, h, names in FILES]), "native")
        append_frame(con, "read", "corpus_page", pd.DataFrame([{
            "file_num": f, "doc_name": d, "doc_sha256": s, "page": pg, "chars": len(t), "text": t,
            "extracted_at": NOW, "source": "text_layer"} for f, d, s, pg, t in PAGES]), "read")
        # the loader creates this one, not schema.sql; retrieval's extracted tier reads it unguarded
        con.execute("create table if not exists read.field_value (file_num text, value_id text, page integer, "
                    "as_printed text, unit_as_printed text, quote text, field text, status text, tier text)")
    finally:
        con.close()
    return df

"""The knowledge-driven score: published targeting criteria, applied consistently, with every step visible.

This is the only kind of Athabasca prospectivity model with a peer-reviewed basin-scale precedent, and that
precedent is worth stating plainly: index-overlay models of this basin "'rediscover' the known uranium
mineralisation", with no numeric metric, on criteria those same deposits shaped. So this score is not evidence
that the criteria are right. It is a consistent application of what the literature says, with each criterion's
contribution kept separately so a geologist can argue with any one of them.

Two rules the arithmetic enforces:

* **Folklore never moves the number.** Claims the literature records as personal communication with no
  published test are carried in `criteria.toml` at weight zero. They are shown to the agent and printed in the
  memo, and they cannot change a score.
* **A cell with little known is not scored.** The combination is a weighted mean over the criteria that have a
  value, and if the known weight falls below `min_known_weight` the score is null. Scoring a cell from two
  criteria out of nine would mostly measure where somebody sampled.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import tomllib
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..paths import PATHS
from ..store import append_frame, connect

CRITERIA_PATH = PATHS.pipeline / "knowledge" / "criteria.toml"
TOOL = "prospect/criteria"

SHAPES = ("falling", "rising", "band", "binary", "percentile_rising")
STATUSES = ("published", "assumed", "folklore")


class CriteriaError(Exception):
    """The criteria file does not describe a usable score."""


@dataclass(frozen=True)
class Criterion:
    key: str
    title: str
    element: str
    feature: str
    shape: str
    weight: float
    status: str
    evidence: str
    caveat: str
    params: dict[str, float]

    @property
    def counts(self) -> bool:
        return self.weight > 0


@dataclass(frozen=True)
class CriteriaSet:
    schema_version: str
    min_known_weight: float
    criteria: tuple[Criterion, ...]

    @property
    def counted(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.counts)

    @property
    def folklore(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.status == "folklore")

    @property
    def total_weight(self) -> float:
        return sum(c.weight for c in self.counted)


def load(path: Any = None) -> CriteriaSet:
    raw = tomllib.loads((path or CRITERIA_PATH).read_text())
    out: list[Criterion] = []
    seen: set[str] = set()
    for entry in raw.get("criterion") or []:
        key = str(entry.get("key") or "")
        where = f"criterion {key or '<no key>'}"
        for field in ("key", "title", "element", "feature", "shape", "status", "evidence", "caveat"):
            if not entry.get(field):
                raise CriteriaError(f"{where}: missing {field}")
        if key in seen:
            raise CriteriaError(f"{where}: duplicate key")
        seen.add(key)
        if entry["shape"] not in SHAPES:
            raise CriteriaError(f"{where}: shape {entry['shape']!r} not one of {SHAPES}")
        if entry["status"] not in STATUSES:
            raise CriteriaError(f"{where}: status {entry['status']!r} not one of {STATUSES}")
        weight = float(entry.get("weight", 0.0))
        if entry["status"] == "folklore" and weight != 0.0:
            raise CriteriaError(
                f"{where}: folklore must carry weight zero; the literature records it as personal "
                f"communication with no published test, so it may be named but never counted"
            )
        if weight < 0:
            raise CriteriaError(f"{where}: negative weight")
        out.append(Criterion(
            key=key, title=str(entry["title"]), element=str(entry["element"]),
            feature=str(entry["feature"]), shape=str(entry["shape"]), weight=weight,
            status=str(entry["status"]), evidence=str(entry["evidence"]), caveat=str(entry["caveat"]),
            params={k: float(v) for k, v in (entry.get("params") or {}).items()},
        ))
    if not out:
        raise CriteriaError("no [[criterion]] blocks")
    cs = CriteriaSet(str(raw.get("schema_version") or "0"),
                     float(raw.get("min_known_weight", 0.5)), tuple(out))
    if cs.total_weight <= 0:
        raise CriteriaError("every criterion carries weight zero: nothing would be scored")
    return cs


# ---------------------------------------------------------------- membership


def falling(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """1 at or below `lo`, 0 at or above `hi`: nearer is better."""
    if hi <= lo:
        raise CriteriaError("falling needs hi > lo")
    return np.clip((hi - x) / (hi - lo), 0.0, 1.0)


def rising(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    if hi <= lo:
        raise CriteriaError("rising needs hi > lo")
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


def band(x: np.ndarray, a: float, b: float, c: float, d: float) -> np.ndarray:
    """A trapezoid: 0 below a, 1 from b to c, 0 above d."""
    if not a <= b <= c <= d:
        raise CriteriaError("band needs a <= b <= c <= d")
    up = np.clip((x - a) / (b - a), 0.0, 1.0) if b > a else (x >= a).astype(float)
    down = np.clip((d - x) / (d - c), 0.0, 1.0) if d > c else (x <= d).astype(float)
    return np.minimum(up, down)


def membership(c: Criterion, values: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    """Membership in 0..1 for one criterion, and the thresholds actually used."""
    x = np.asarray(values, dtype=float)
    p = c.params
    if c.shape == "falling":
        return falling(x, p["lo"], p["hi"]), {"lo": p["lo"], "hi": p["hi"]}
    if c.shape == "rising":
        return rising(x, p["lo"], p["hi"]), {"lo": p["lo"], "hi": p["hi"]}
    if c.shape == "band":
        return band(x, p["a"], p["b"], p["c"], p["d"]), {k: p[k] for k in "abcd"}
    if c.shape == "binary":
        return np.clip(x, 0.0, 1.0), {}
    if c.shape == "percentile_rising":
        known = x[np.isfinite(x)]
        if known.size == 0:
            return np.full(x.shape, np.nan), {}
        lo = float(np.percentile(known, p["p_lo"]))
        hi = float(np.percentile(known, p["p_hi"]))
        if hi <= lo:  # a flat distribution cannot be split into an anomaly and a background
            return np.full(x.shape, np.nan), {"lo": lo, "hi": hi, "degenerate": 1.0}
        return rising(x, lo, hi), {"lo": lo, "hi": hi, "p_lo": p["p_lo"], "p_hi": p["p_hi"]}
    raise CriteriaError(f"unknown shape {c.shape!r}")


# ---------------------------------------------------------------- the score


def feature_matrix(grid_id: str | None = None) -> pd.DataFrame:
    """One row per cell, one column per feature, with nulls left as nulls."""
    con = connect(read_only=True)
    try:
        if grid_id is None:
            row = con.execute("select grid_id from derived.grid order by built_at desc limit 1").fetchone()
            grid_id = row[0] if row else None
        df = con.execute(
            "select c.cell_id, f.feature_key, f.value from derived.cell c "
            "left join derived.cell_feature f using (cell_id) where c.grid_id = ?",
            [grid_id],
        ).df()
    finally:
        con.close()
    wide = df.pivot_table(index="cell_id", columns="feature_key", values="value", dropna=False)
    return wide.reset_index()


def score(grid_id: str | None = None, cs: CriteriaSet | None = None,
          log: Callable[[str], None] = print) -> dict[str, Any]:
    """Compute the criteria score and write it to `derived.cell_score` with per-criterion contributions."""
    cs = cs or load()
    wide = feature_matrix(grid_id)
    n = len(wide)
    log(f"  {n} cells, {len(cs.counted)} counted criteria, {len(cs.folklore)} folklore at weight zero")

    total = np.zeros(n)
    known_weight = np.zeros(n)
    contributions: list[pd.DataFrame] = []
    thresholds: dict[str, dict[str, float]] = {}

    for c in cs.criteria:
        if c.feature not in wide.columns:
            log(f"    {c.key:24} feature {c.feature} missing; criterion skipped")
            continue
        values = wide[c.feature].to_numpy(dtype=float)
        m, used = membership(c, values)
        thresholds[c.key] = used
        have = np.isfinite(m)
        if c.counts:
            total[have] += c.weight * m[have]
            known_weight[have] += c.weight
        contributions.append(pd.DataFrame({
            "cell_id": wide["cell_id"], "criterion": c.key, "membership": m,
            "weight": c.weight, "contribution": np.where(have, c.weight * m, np.nan),
        }))
        log(f"    {c.key:24} {'folklore' if not c.counts else f'weight {c.weight:g}'}: "
            f"{int(have.sum()) / max(n, 1):.0%} of cells known")

    share = known_weight / max(cs.total_weight, 1e-9)
    enough = share >= cs.min_known_weight
    value = np.full(n, np.nan)
    value[enough] = total[enough] / known_weight[enough]
    log(f"  scored {int(enough.sum()):,} of {n:,} cells ({enough.mean():.0%}); the rest have less than "
        f"{cs.min_known_weight:.0%} of the criteria weight known")

    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    scores = pd.DataFrame({
        "cell_id": wide["cell_id"], "model": "criteria", "score": value,
        "known_share": share, "in_aoa": True, "computed_at": now,
        "params": json.dumps({"min_known_weight": cs.min_known_weight, "thresholds": thresholds}),
    })
    contrib = pd.concat(contributions, ignore_index=True) if contributions else pd.DataFrame()

    con = connect()
    try:
        con.execute("delete from derived.cell_score where model = 'criteria'")
        append_frame(con, "derived", "cell_score", scores, "derived")
        if len(contrib):
            con.execute("delete from derived.cell_criterion")
            append_frame(con, "derived", "cell_criterion", contrib.assign(computed_at=now), "derived")
    finally:
        con.close()

    return {
        "cells": n,
        "scored": int(enough.sum()),
        "criteria": len(cs.counted),
        "folklore": len(cs.folklore),
        "median": float(np.nanmedian(value)) if np.isfinite(value).any() else math.nan,
        "thresholds": thresholds,
    }

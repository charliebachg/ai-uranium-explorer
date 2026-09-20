"""Which cells go in the benchmark, drawn once from the seed and never again.

Four strata, each answering a different question about an analyst:

* **deposit** — a mapped deposit footprint, thinned to one cell per coarse block so a camp of adjacent cells
  counts once. Can the analyst recognise the ground a deposit sits on?
* **occurrence** — a drilled occurrence. Weaker ground truth, but somebody put a hole in it.
* **negative** — an unlabelled cell with real drilling in it, drawn to match the positives' exploration-effort
  profile. This is the stratum that keeps the benchmark honest: a cell nobody looked at is not a negative,
  and a negative drawn without matching would let "how many holes" stand in for geology.
* **probe** — a never-drilled unlabelled cell. Not scored; watched, so an analyst's behaviour on unknown ground
  is on the record.

Folds are spatial blocks, committed here before any model sees the cells, and the held-out split is drawn per
stratum so every stratum gives up the same share. Everything is a pure function of the frame and the seed.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..prospect import headline as H
from ..prospect import modelsearch as MS
from .spec import STRATA, BenchSpec

COLUMNS = ("cell_id", "stratum", "fold", "split")


def sample_cells(df: pd.DataFrame, spec: BenchSpec) -> pd.DataFrame:
    """One row per sampled cell: `cell_id, stratum, fold, split`, in a stable order.

    Deterministic from `spec.seed`: every draw comes from one generator consumed in a fixed order. A stratum
    with fewer candidates than asked contributes what it has; `shortfalls()` reports the difference and the
    frame carries it in `attrs["shortfall"]` as well."""
    rng = np.random.default_rng(spec.seed)
    fold = MS.spatial_fold(df, spec.fold_km, n_splits=spec.n_folds, seed=spec.seed)
    tier = df["label_tier"].to_numpy()
    holes = df["holes_n"].fillna(0.0).to_numpy(dtype=float)

    # deposits: one per block, then a uniform draw among the survivors
    y_dep = (tier == "deposit").astype(int)
    thinned = H.thinned_positives(df, y_dep, block_m=spec.strata.thin_block_m, seed=spec.seed)
    deposits = _draw(rng, np.flatnonzero(thinned & (y_dep == 1)), spec.strata.deposit)

    # occurrences: drilled ones only
    occurrences = _draw(rng, np.flatnonzero((tier == "occurrence") & (holes > 0)), spec.strata.occurrence)

    # negatives: unlabelled, drilled at least min_holes times, matched to the sampled positives' effort profile
    positives = np.concatenate([deposits, occurrences])
    negatives = _matched_negatives(df, rng, positives,
                                   np.flatnonzero((tier == "unlabelled") & (holes >= spec.strata.min_holes)),
                                   spec.strata.negative, spec.seed)

    # probes: never drilled, unlabelled
    probes = _draw(rng, np.flatnonzero((tier == "unlabelled") & (holes == 0)), spec.strata.probe)

    rows = []
    for stratum, idx in (("deposit", deposits), ("occurrence", occurrences), ("negative", negatives), ("probe", probes)):
        held = _held_out(rng, len(idx), spec.held_out_share)
        for j, i in enumerate(idx):
            rows.append({"cell_id": str(df["cell_id"].iat[i]), "stratum": stratum, "fold": int(fold.groups[i]),
                         "split": "heldout" if held[j] else "open"})
    out = pd.DataFrame(rows, columns=list(COLUMNS))
    out["fold"] = out["fold"].astype(int)
    out = out.sort_values(["stratum", "cell_id"]).reset_index(drop=True)
    out.attrs["shortfall"] = shortfalls(out, spec)
    return out


def shortfalls(cells: pd.DataFrame, spec: BenchSpec) -> dict[str, int]:
    """Per stratum, how many cells the spec asked for that the frame could not supply. Empty when none."""
    counts = cells["stratum"].value_counts() if len(cells) else pd.Series(dtype=int)
    out = {}
    for s in STRATA:
        short = spec.strata.asked(s) - int(counts.get(s, 0))
        if short > 0:
            out[s] = short
    return out


def assign_bench_ids(cells: pd.DataFrame, seed: int) -> pd.DataFrame:
    """`b-0001`... in a seeded shuffle, so an id's rank says nothing about its stratum or its cell."""
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(cells))
    ids = np.empty(len(cells), dtype=object)
    for rank, i in enumerate(order):
        ids[i] = f"b-{rank + 1:04d}"
    out = cells.copy()
    out.insert(0, "bench_id", ids)
    return out.sort_values("bench_id").reset_index(drop=True)


# ---------------------------------------------------------------- draws


def _draw(rng: np.random.Generator, pool: np.ndarray, n: int) -> np.ndarray:
    """Up to n of the pool, without replacement, in a stable (sorted) order."""
    if n <= 0 or len(pool) == 0:
        return np.array([], dtype=int)
    take = rng.choice(pool, size=min(n, len(pool)), replace=False)
    return np.sort(take)


def _matched_negatives(df: pd.DataFrame, rng: np.random.Generator, positives: np.ndarray, pool: np.ndarray,
                       n: int, seed: int) -> np.ndarray:
    """Negatives whose effort-index deciles follow the sampled positives', via the headline sampler.

    The sampler is run on the sub-frame of sampled positives plus eligible negatives, asking for enough per
    positive to cover `n`; if it returns more than `n` a uniform draw thins them, which keeps the decile
    proportions in expectation, and if it returns fewer, that is the shortfall."""
    if n <= 0 or len(pool) == 0:
        return np.array([], dtype=int)
    if len(positives) == 0:
        return _draw(rng, pool, n)
    idx = np.concatenate([positives, pool])
    sub = df.iloc[idx].reset_index(drop=True)
    y = np.concatenate([np.ones(len(positives), dtype=int), np.zeros(len(pool), dtype=int)])
    per_positive = max(1, math.ceil(n / len(positives)))
    keep = H.matched_background(sub, y, seed=seed, per_positive=per_positive)
    matched = idx[np.flatnonzero(keep & (y == 0))]
    return _draw(rng, matched, n)


def _held_out(rng: np.random.Generator, n: int, share: float) -> np.ndarray:
    """A boolean mask holding out round(share * n) of n, drawn uniformly."""
    held = np.zeros(n, dtype=bool)
    k = int(round(share * n))
    if n and k:
        held[rng.choice(n, size=min(k, n), replace=False)] = True
    return held

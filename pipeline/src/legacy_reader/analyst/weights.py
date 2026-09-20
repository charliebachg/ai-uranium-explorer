"""Decider (a) of Analyst v1: a weighted sum over node strengths, with weights fitted out of fold (PRD 8.4
Stage 5).

MineAgent's decision module, MineTRACE's fitted expert weights and the fitted-weights criteria arm of PRD C are
one thing here: a chain's nodes become a feature vector, a weight set turns the vector into a number in 0..1,
and the only question is where the weights came from. Two answers, one `Weights` type:

* **The criteria table's own weights** (`criteria_weights`). Nothing was learned from the labels, so they
  need no fold; they are the fallback every arm can score with before a single chain exists. Folklore
  criteria carry weight zero in the table and are left out here, so a chain that leans on them scores nothing
  for it. The rule is the criteria score's own: a weighted mean over the criteria the chain knows, null when
  too little is known, because a score built from two criteria out of eight would mostly say where somebody
  sampled.
* **Fitted weights** (`fit_weights`). A logistic regression over the same features, fitted per spatial fold on
  the rows of the OTHER folds (B18): the weights a fold's cells are scored with never saw those cells' labels.
  The version string carries a hash of the training rows, so a refit on different chains is a different
  version and the manifest can say which one scored what.

A feature is a criterion's signed strength: met is +strength/5, not met is -strength/5, unknown is 0 - and
unknown is not absent, so every criterion also carries a known flag, which lets the fitted weights learn what
"nobody measured this" is worth instead of reading it as "measured and weak". A cross-check node contributes
under its pair key like any criterion. The criteria weights read the flag only to decide what is known; the
fitted weights weigh it.

Nothing here calls a model, draws a random number, or reads the store: the fitting frame is built from stored
chains and the benchmark key by `rows_from_chains`, and the weights are JSON under `WEIGHTS_DIR`.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from ..paths import PATHS
from ..prospect.criteria import CriteriaSet
from .score import FOLD_KIND, LABEL_STRATA, POSITIVE_LABELS
from .wire import NODE_STATUSES, STRENGTH_MAX, Node

SCHEMA_VERSION = "1.0.0"
WEIGHTS_DIR = PATHS.out / "analyst" / "weights"

#: the two scoring rules a weight set can name
RULE_MEAN = "weighted_mean"
RULE_LOGISTIC = "logistic"
RULES = (RULE_MEAN, RULE_LOGISTIC)

#: the feature key of a criterion's known flag, beside the criterion's own key
KNOWN_SUFFIX = ":known"

#: a fold with fewer cells than this cannot be scored on its own and is refused before any fit
MIN_FOLD_CELLS = 10

#: one row per (cell, criterion): what `fit_weights` reads and `rows_from_chains` writes
ROW_COLUMNS = ("cell_id", "fold", "criterion", "status", "strength", "label")

#: L2 at unit strength: the features are all in -1..1, so no scaling and no tuning stands between a row of
#: nodes and its weight; the class weighting answers the positive-unlabelled base rate
L2_C = 1.0


@dataclass(frozen=True)
class Weights:
    """One weight set. `by_criterion` is keyed the way `node_features` keys a vector: a criterion (or a
    cross-check pair) key for its signed strength and `<key>:known` for its known flag. Criteria weights carry
    only the former and score by `RULE_MEAN`; fitted weights carry both and score by `RULE_LOGISTIC`.

    `min_known_weight` is the share of the criteria weight a chain must know for the mean rule to return a
    number; below it the score is None, an abstention. Zero never abstains, which is what fitted weights use,
    because for them an unknown criterion is a feature with a weight, not a missing term.

    `fold` is the fold these weights score, and `fitted_on` says what they were fitted on - which never
    includes that fold. Both are None for criteria weights, which were fitted on nothing."""

    version: str
    by_criterion: dict[str, float]
    intercept: float = 0.0
    fold: int | None = None
    fitted_on: dict[str, Any] | None = None
    rule: str = RULE_MEAN
    min_known_weight: float = 0.0

    def __post_init__(self) -> None:
        if self.rule not in RULES:
            raise ValueError(f"rule {self.rule!r} is not one of {RULES}")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Weights":
        fold = d.get("fold")
        return cls(**{**d, "fold": int(fold) if fold is not None else None})


# ---------------------------------------------------------------- features

def _feature(status: str, strength: int) -> tuple[float, float]:
    """(signed strength, known) of one node: the arithmetic `node_features` and `design` share, so a chain
    scored live and a chain in the fitting frame are read the same way."""
    if status == "unknown":
        return 0.0, 0.0
    sign = 1.0 if status == "met" else -1.0
    return sign * float(strength) / STRENGTH_MAX, 1.0


def _latest(nodes: Iterable[Node]) -> dict[str, Node]:
    """The published node that currently stands for each criterion key, by (round, attempt) - the rule of
    `Chain.current_nodes` applied per criterion rather than per segment. A retrieval node names no criterion
    and is skipped; an unpublished node (the gate's final unknown record) is skipped too, which reads the
    same as unknown."""
    latest: dict[str, Node] = {}
    for n in nodes:
        if not n.published or n.criterion is None:
            continue
        cur = latest.get(n.criterion)
        if cur is None or (n.round, n.attempt) >= (cur.round, cur.attempt):
            latest[n.criterion] = n
    return latest


def node_features(nodes: Iterable[Node]) -> dict[str, float]:
    """A chain as a feature vector: per criterion key, the signed strength (met +s/5, not met -s/5, unknown 0)
    and `<key>:known` (1 when measured). A criterion with no published node is simply absent, which every
    reader takes as unknown."""
    out: dict[str, float] = {}
    for key, n in _latest(nodes).items():
        value, known = _feature(n.status, n.strength)
        out[key] = value
        out[key + KNOWN_SUFFIX] = known
    return out


# ---------------------------------------------------------------- scoring

def criteria_weights(criteria: CriteriaSet) -> Weights:
    """The criteria table's own weights, folklore excluded (it carries weight zero in the table and would
    count as a known criterion at no weight, which is a distraction, not a term). No cross-check pair has a
    table weight, so under these weights a cross-check node neither scores nor counts as known."""
    return Weights(
        version=f"criteria/{criteria.schema_version}",
        by_criterion={c.key: float(c.weight) for c in criteria.counted},
        rule=RULE_MEAN, min_known_weight=float(criteria.min_known_weight),
    )


def score(nodes: Iterable[Node], weights: Weights) -> float | None:
    """The decision as a number in 0..1, or None when the mean rule has too little to go on.

    `RULE_LOGISTIC`: sigmoid(intercept + sum of w_k * f_k) over every weighted feature key, an absent feature
    reading as 0 (unknown). Always a number: for fitted weights the known flags are terms.

    `RULE_MEAN`: the criteria score's rule on nodes. Numerator: sum over met criteria of w * strength/5 (a
    not-met criterion is membership 0, whatever its strength). Denominator: the weight of the known criteria.
    None when the known weight is nothing, or below `min_known_weight` of the total."""
    f = node_features(nodes)
    if weights.rule == RULE_LOGISTIC:
        z = weights.intercept + sum(w * f.get(k, 0.0) for k, w in weights.by_criterion.items())
        return 1.0 / (1.0 + math.exp(-z))
    total = sum(weights.by_criterion.values())
    known = sum(w * f.get(k + KNOWN_SUFFIX, 0.0) for k, w in weights.by_criterion.items())
    if known <= 0.0 or known / total < weights.min_known_weight:
        return None
    met = sum(w * max(f.get(k, 0.0), 0.0) for k, w in weights.by_criterion.items())
    return met / known


# ---------------------------------------------------------------- the fitting frame

def _checked(rows: pd.DataFrame) -> pd.DataFrame:
    """The fitting frame with its contract enforced: the six columns, a status the wire knows, a strength in
    range, a 0/1 label, one row per (cell, criterion), and one fold and one label per cell. A cell in two
    folds would be fitted on and scored by the same weights, which is the leak this module exists to
    prevent, so it is refused rather than resolved."""
    missing = [c for c in ROW_COLUMNS if c not in rows.columns]
    if missing:
        raise ValueError(f"fitting frame: missing columns {missing}")
    if len(rows) == 0:
        raise ValueError("fitting frame: no rows")
    df = rows.loc[:, list(ROW_COLUMNS)].copy()
    bad_status = sorted(set(df["status"]) - set(NODE_STATUSES))
    if bad_status:
        raise ValueError(f"fitting frame: status {bad_status} not in {NODE_STATUSES}")
    if not df["strength"].between(0, STRENGTH_MAX).all():
        raise ValueError(f"fitting frame: strength outside 0..{STRENGTH_MAX}")
    bad_label = sorted(set(df["label"]) - {0, 1})
    if bad_label:
        raise ValueError(f"fitting frame: label {bad_label} not in {{0, 1}}")
    dup = df.duplicated(subset=["cell_id", "criterion"])
    if dup.any():
        raise ValueError(f"fitting frame: {int(dup.sum())} duplicate (cell_id, criterion) rows, "
                         f"first {tuple(df.loc[dup, ['cell_id', 'criterion']].iloc[0])}")
    per_cell = df.groupby("cell_id")[["fold", "label"]].nunique()
    torn = per_cell[(per_cell > 1).any(axis=1)]
    if len(torn):
        raise ValueError(f"fitting frame: cells with more than one fold or label: {list(torn.index[:5])}")
    df["fold"] = df["fold"].astype(int)
    df["label"] = df["label"].astype(int)
    df["strength"] = df["strength"].astype(int)
    df["cell_id"] = df["cell_id"].astype(str)
    df["criterion"] = df["criterion"].astype(str)
    return df


@dataclass(frozen=True)
class Design:
    """One row per cell, ready for the estimator: `x` has a column per key in `columns` (the criterion keys,
    then their known flags), `y` the label, `fold` the fold, all aligned to `cells`."""

    cells: list[str]
    columns: list[str]
    x: np.ndarray
    y: np.ndarray
    fold: np.ndarray


def design(rows: pd.DataFrame, keys: list[str] | None = None) -> Design:
    """The frame pivoted to one row per cell. `keys` fixes the criterion columns, so every fold's design has
    the same columns whether or not a criterion happens to appear in its training rows; a criterion absent
    from a cell's rows is unknown (0, not known)."""
    df = _checked(rows)
    keys = sorted(keys) if keys is not None else sorted(df["criterion"].unique())
    feats = [_feature(s, k) for s, k in zip(df["status"], df["strength"], strict=True)]
    df["value"] = [v for v, _ in feats]
    df["known"] = [k for _, k in feats]
    value = df.pivot(index="cell_id", columns="criterion", values="value").reindex(columns=keys).fillna(0.0)
    cells = list(value.index)
    known = (df.pivot(index="cell_id", columns="criterion", values="known")
             .reindex(index=cells, columns=keys).fillna(0.0))
    per_cell = df.groupby("cell_id")[["fold", "label"]].first().reindex(cells)
    return Design(
        cells=cells, columns=[*keys, *(k + KNOWN_SUFFIX for k in keys)],
        x=np.hstack([value.to_numpy(dtype=float), known.to_numpy(dtype=float)]),
        y=per_cell["label"].to_numpy(dtype=int), fold=per_cell["fold"].to_numpy(dtype=int),
    )


def training_rows(rows: pd.DataFrame, fold: int) -> pd.DataFrame:
    """The rows fold `fold`'s weights may be fitted on: every row of every other fold. This is the one place
    the leakage rule is applied, so a test can watch it."""
    return rows[rows["fold"] != fold]


def rows_sha(rows: pd.DataFrame) -> str:
    """A short hash of a fitting frame's content, order-free and dtype-free, so the same rows in another order
    or read back from disk hash the same and one changed strength does not."""
    lines = sorted(f"{c}\t{k}\t{s}\t{int(st)}\t{int(lb)}" for c, k, s, st, lb in zip(
        rows["cell_id"], rows["criterion"], rows["status"], rows["strength"], rows["label"], strict=True))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def estimator(seed: int = 0) -> Any:
    """The one model the fitted weights come from: L2 logistic regression, classes balanced because the
    benchmark's negatives are unlabelled drilled cells and outnumber its positives. lbfgs is deterministic;
    the seed is passed through so a change of solver could not silently make a fit unrepeatable."""
    from sklearn.linear_model import LogisticRegression

    return LogisticRegression(C=L2_C, class_weight="balanced", solver="lbfgs", max_iter=1000, random_state=seed)


def fit_weights(rows: pd.DataFrame, fold_kind: str = FOLD_KIND, seed: int = 0) -> dict[int, Weights]:
    """Out-of-fold weights: for each fold k, the estimator fitted on `training_rows(rows, k)` - the rows of
    every other fold - so that scoring fold k's cells with `result[k]` never uses their labels. `fold_kind`
    names what the frame's fold ids mean (they are spatial blocks in the benchmark) and goes into the version
    and the manifest; it changes nothing about the fit.

    Refused (ValueError) before anything is fitted: fewer than two folds (nothing to hold out), a fold with
    fewer than `MIN_FOLD_CELLS` cells or with a single class (it could not be scored, and it would leave the
    other folds' training set short of a class), and any frame `_checked` rejects."""
    df = _checked(rows)
    per_fold = df.groupby("fold").agg(cells=("cell_id", "nunique"), classes=("label", "nunique"))
    folds = sorted(int(f) for f in per_fold.index)
    if len(folds) < 2:
        raise ValueError(f"out-of-fold weights need at least two folds; the frame has {folds}")
    for k in folds:
        n, classes = int(per_fold.loc[k, "cells"]), int(per_fold.loc[k, "classes"])
        if n < MIN_FOLD_CELLS:
            raise ValueError(f"fold {k} has {n} cells; fewer than {MIN_FOLD_CELLS} cannot be scored")
        if classes < 2:
            raise ValueError(f"fold {k} has a single class; it cannot be scored")
    keys = sorted(df["criterion"].unique())
    out: dict[int, Weights] = {}
    for k in folds:
        train = training_rows(df, k)
        d = design(train, keys)
        model = estimator(seed).fit(d.x, d.y)
        sha = rows_sha(train)
        out[k] = Weights(
            version=f"fitted/{fold_kind}/{k}/{sha}",
            by_criterion={c: float(w) for c, w in zip(d.columns, model.coef_[0], strict=True)},
            intercept=float(model.intercept_[0]), fold=k, rule=RULE_LOGISTIC,
            fitted_on={"n_cells": len(d.cells), "folds": [f for f in folds if f != k],
                       "positives": int(d.y.sum()), "fold_kind": fold_kind, "seed": seed,
                       "criteria": keys, "rows_sha256": sha},
        )
    return out


# ---------------------------------------------------------------- on disk

def save(weights_by_fold: dict[int, Weights], version_dir: Path | None = None) -> Path:
    """The per-fold weights as one JSON file, named by fold kind and a hash of the folds' versions so a refit
    is a new file beside the old one, never over it. Refuses weights without a fold: the file is keyed by
    fold, and criteria weights are read from the criteria table, not from here."""
    if not weights_by_fold:
        raise ValueError("no weights to save")
    kinds: set[str] = set()
    for w in weights_by_fold.values():
        if w.fold is None or w.fitted_on is None:
            raise ValueError(f"only out-of-fold weights are saved by fold; {w.version!r} carries no fold")
        kinds.add(str(w.fitted_on.get("fold_kind")))
    if len(kinds) != 1:
        raise ValueError(f"one file holds one fold kind, not {sorted(kinds)}")
    kind = kinds.pop()
    versions = [weights_by_fold[k].version for k in sorted(weights_by_fold)]
    sha = hashlib.sha256("\n".join(versions).encode()).hexdigest()[:12]
    out_dir = Path(version_dir) if version_dir is not None else WEIGHTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{kind}-{sha}.json"
    payload = {"schema_version": SCHEMA_VERSION, "fold_kind": kind,
               "by_fold": {str(k): weights_by_fold[k].as_dict() for k in sorted(weights_by_fold)}}
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))
    return path


def load(path: Path) -> dict[int, Weights]:
    """The file `save` wrote, folds as ints again."""
    payload = json.loads(Path(path).read_text())
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: weights schema {payload.get('schema_version')!r}, expected {SCHEMA_VERSION!r}")
    return {int(k): Weights.from_dict(w) for k, w in payload["by_fold"].items()}


# ---------------------------------------------------------------- from a finished run

def rows_from_chains(chains: list[dict[str, Any]], key: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """The fitting frame from stored chain dicts (`Chain.as_dict` shape: `cell`, `nodes`) and the benchmark
    key, which gives each bench id its label tier and its fold. A chain's cell is its bench id in a benchmark
    run; a store row that also names `bench_id` is read by that.

    Positives are the deposit and occurrence tiers. Probes carry no label to fit on and are left out; a
    held-out cell is refused outright (the fitted weights would then have seen the held-out labels, and no
    later scoring could undo that); a cell absent from the key is an error, not a negative. The last chain
    per cell wins, as `score.read_cells` reads a resumed run. A chain with no published criterion node yields
    no rows: it abstained, and there is nothing of it to weigh."""
    by_cell: dict[str, dict[str, Any]] = {}
    for chain in chains:
        by_cell[str(chain.get("bench_id") or chain["cell"])] = chain
    records: list[dict[str, Any]] = []
    for bench_id, chain in by_cell.items():
        k = key.get(bench_id)
        if k is None:
            raise KeyError(f"chain {chain.get('chain_id')!r}: cell {bench_id!r} is not in the benchmark key")
        if k.get("split") == "heldout":
            raise PermissionError(f"chain {chain.get('chain_id')!r} is over held-out cell {bench_id!r}; "
                                  f"weights are never fitted on the held-out split")
        if (k.get("stratum") or k.get("label")) not in LABEL_STRATA:
            continue
        label = int(k.get("label") in POSITIVE_LABELS)
        fold = int(k["fold"])
        nodes = [Node.from_dict(n) if isinstance(n, dict) else n for n in chain.get("nodes") or []]
        for criterion, n in _latest(nodes).items():
            records.append({"cell_id": bench_id, "fold": fold, "criterion": criterion, "status": n.status,
                            "strength": int(n.strength), "label": label})
    return pd.DataFrame.from_records(records, columns=list(ROW_COLUMNS))

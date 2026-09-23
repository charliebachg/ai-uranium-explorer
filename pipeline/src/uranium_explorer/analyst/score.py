"""Scoring: an arm's run against the key, the fitted models on the same cells, and one table with intervals.

Three decisions shape every number here, and they are the same for an arm and for a baseline:

* **Abstaining is allowed and it is counted.** `insufficient` is a verdict the analyst may give and the
  handbook says is often right, so it is not a wrong answer; but it is not a positive either. Precision,
  recall and F1 are computed with abstained cells as "not positive", the abstain rate is reported beside them
  with its denominator, and the ranking metrics are given twice: over the cells the model committed to, and
  over every cell with an abstention scored as 0.5. An answer the gate refused is an abstention too, since
  nothing it said may be used, and the gate rejection rate says how often that happened.
* **Probes carry no label.** Probe cells exist to see whether the model abstains where it should, so they are
  excluded from every label metric and reported only as a probe abstain rate.
* **Intervals are bootstraps over cells**, positives and negatives resampled separately, as `headline` does,
  so a row's interval means the same thing whether the row is an arm or the learned model.

The baselines answer the question every arm row invites: an analyst that copied the learned score, the three
fitted scores themselves at a 0.5 threshold, and chance. Chance is given twice, analytically and as a seeded
draw, because a table whose random row moves with the seed invites the wrong conversation.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..prospect import models as M
from ..prospect import tracking as TR
from ..store import append_frame, connect
from . import frozen as F
from .v0 import ABSTAIN, NEGATIVE, POSITIVE

BOOT = 1000
BINS = 10
POSITIVE_LABELS = ("deposit", "occurrence")
LABEL_STRATA = ("deposit", "occurrence", "negative")
BASELINE_MODELS = ("learned", "effort", "criteria")
#: the learned model given the extended evidence, a baseline where the benchmark carries it
EXTENDED = "extended"
FOLD_KIND = "spatial"
#: the staged loop's per-stage columns the bench table carries for an arm, each with the formatter
#: the store's metric row and the Eval page print it with. A v0 arm and a baseline have no stages, so their
#: rows hold null under each; so does a stage a run never had (no verifier, no rounds).
STAGE_COLUMNS: dict[str, str] = {
    "stage_n_chains": "int",
    "stage_gate_rejection_rate": "ratio3",
    "stage_valid_rate": "ratio3",
    "stage_verifier_catch_rate": "ratio3",
    "stage_rounds_to_valid_mean": "m2",
    "stage_reexecuted_mean": "m2",
    "stage_verifier_agreement_rate": "ratio3",
    "stage_decider_agreement_rate": "ratio3",
}
#: the metrics a table row carries into derived.metric, when finite
TABLE_METRICS = ("precision", "recall", "f1", "pr_auc", "roc_auc", "pr_auc_all", "roc_auc_all",
                 "pr_auc_rank", "roc_auc_rank", "coverage", "brier", "cal_slope", "ece",
                 "ece_committed", "abstain_rate", "gate_rejection_rate", "probe_abstain_rate",
                 "cost_usd_per_cell", "latency_s_per_cell", *STAGE_COLUMNS)
NAN = float("nan")


def out_dir(version: str) -> Path:
    """Beside the prospect output, and sandboxed with it in tests."""
    return TR.OUT_DIR.parent / "bench" / version


# ---------------------------------------------------------------- the pure metrics

def _pr_auc(y: np.ndarray, p: np.ndarray) -> float:
    return M.pr_auc(y, p) if len(y) and 0 < y.sum() < len(y) else NAN


def _roc_auc(y: np.ndarray, p: np.ndarray) -> float:
    return M.roc_auc(y, p) if len(y) and 0 < y.sum() < len(y) else NAN


def ece(y: np.ndarray, p: np.ndarray, bins: int = BINS) -> float:
    """Expected calibration error over equal-width bins: the bin-weighted gap between mean outcome and mean
    stated probability."""
    if len(y) == 0:
        return NAN
    b = np.clip((p * bins).astype(int), 0, bins - 1)
    total = 0.0
    for k in range(bins):
        m = b == k
        if m.any():
            total += m.mean() * abs(float(y[m].mean()) - float(p[m].mean()))
    return float(total)


def brier(y: np.ndarray, p: np.ndarray) -> float:
    """Mean squared error of the stated probability, over the cells that state one."""
    ok = np.isfinite(p)
    return float(np.mean((p[ok] - y[ok]) ** 2)) if ok.any() else NAN


def calibration_slope(y: np.ndarray, p: np.ndarray, iters: int = 25) -> float:
    """The slope of a logistic fit of the outcome on the logit of the stated probability: 1 is calibrated,
    below 1 is overconfident, above 1 underconfident, near 0 says the probability carries no ranking. A plain
    Newton fit in two parameters, so a bootstrap of a thousand draws costs nothing."""
    ok = np.isfinite(p)
    y, p = y[ok].astype(float), np.clip(p[ok], 1e-4, 1 - 1e-4)
    if len(y) < 3 or y.min() == y.max() or np.ptp(p) < 1e-9:
        return NAN
    x = np.column_stack([np.ones_like(p), np.log(p / (1 - p))])
    b = np.zeros(2)
    for _ in range(iters):
        mu = 1.0 / (1.0 + np.exp(-(x @ b)))
        w = mu * (1 - mu) + 1e-9
        try:
            step = np.linalg.solve(x.T @ (x * w[:, None]), x.T @ (y - mu))
        except np.linalg.LinAlgError:
            return NAN
        b = b + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return float(b[1]) if np.all(np.isfinite(b)) and abs(b[1]) < 1e3 else NAN


def _point(y: np.ndarray, p: np.ndarray, pred_pos: np.ndarray, ab: np.ndarray) -> dict[str, float]:
    tp = int((pred_pos & (y == 1)).sum())
    fp = int((pred_pos & (y == 0)).sum())
    fn = int((~pred_pos & (y == 1)).sum())
    committed = ~ab & np.isfinite(p)
    p_all = np.where(committed, p, 0.5)
    return {
        "precision": tp / (tp + fp) if tp + fp else NAN,
        "recall": tp / (tp + fn) if tp + fn else NAN,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else NAN,
        "abstain_rate": float(ab.mean()),
        "pr_auc": _pr_auc(y[committed], p[committed]),
        "roc_auc": _roc_auc(y[committed], p[committed]),
        "pr_auc_all": _pr_auc(y, p_all),
        "roc_auc_all": _roc_auc(y, p_all),
        "ece": ece(y, p_all),
        "ece_committed": ece(y[committed], p[committed]),
        # the ranking the model gives whatever its verdict: every cell with a published probability ranks by it,
        # an insufficient verdict included; a refused or failed cell has none and sits at 0.5, and coverage says
        # how many did
        "pr_auc_rank": _pr_auc(y, np.where(np.isfinite(p), p, 0.5)),
        "roc_auc_rank": _roc_auc(y, np.where(np.isfinite(p), p, 0.5)),
        "coverage": float(np.isfinite(p).mean()) if len(p) else NAN,
        "brier": brier(y, p),
        "cal_slope": calibration_slope(y, p),
    }


def per_stratum(y: np.ndarray, pred_pos: np.ndarray, pred_neg: np.ndarray, ab: np.ndarray,
                strata: np.ndarray) -> dict[str, dict[str, float]]:
    """Accuracy by stratum with an abstention counted as wrong, and the abstain share, so a stratum the model
    declines to answer shows up as such rather than as accuracy."""
    correct = (pred_pos & (y == 1)) | (pred_neg & (y == 0))
    out = {}
    for s in LABEL_STRATA:
        m = strata == s
        if m.any():
            out[s] = {"n": int(m.sum()), "accuracy": float(correct[m].mean()), "abstain_rate": float(ab[m].mean())}
    return out


def metrics(y_true: np.ndarray, p: np.ndarray, verdicts: list[str], abstain_mask: np.ndarray, boot: int = BOOT,
            seed: int = 0, strata: list[str] | None = None) -> dict[str, Any]:
    """Every label metric over one set of labelled cells, with bootstrap intervals over cells.

    `verdicts` maps to predictions: supports_closer_look is positive, evidence_against negative, insufficient
    abstains. `abstain_mask` says which cells count as abstained (the verdict, or a gate refusal, or no
    answer); an abstained cell is never a positive and its probability is 0.5 in the `_all` metrics."""
    y = np.asarray(y_true, dtype=int).ravel()
    p = np.asarray(p, dtype=float).ravel()
    v = np.asarray(list(verdicts), dtype=object).ravel()
    ab = np.asarray(abstain_mask, dtype=bool).ravel()
    n = len(y)
    if not (len(p) == len(v) == len(ab) == n):
        raise ValueError("y_true, p, verdicts and abstain_mask must be the same length")
    out: dict[str, Any] = {"n": n, "n_pos": int(y.sum()), "n_neg": int((y == 0).sum()),
                           "base_rate": float(y.mean()) if n else NAN,
                           "abstain_n": int(ab.sum()), "abstain_denominator": n}
    if n == 0:
        return out
    pred_pos = (v == POSITIVE) & ~ab
    pred_neg = (v == NEGATIVE) & ~ab
    out |= _point(y, p, pred_pos, ab)
    if boot > 0 and n > 1:
        rng = np.random.default_rng(seed)
        pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
        draws: dict[str, list[float]] = {}
        for _ in range(boot):
            idx = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
            for k, val in _point(y[idx], p[idx], pred_pos[idx], ab[idx]).items():
                draws.setdefault(k, []).append(val)
        for k, vals in draws.items():
            arr = np.asarray(vals, dtype=float)
            out[f"{k}_ci"] = [NAN, NAN] if np.all(np.isnan(arr)) else \
                [float(np.nanpercentile(arr, 2.5)), float(np.nanpercentile(arr, 97.5))]
    if strata is not None:
        out["strata"] = per_stratum(y, pred_pos, pred_neg, ab, np.asarray(list(strata), dtype=object))
    return out


# ---------------------------------------------------------------- one run against the key

def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def read_cells(run_dir: Path) -> dict[str, dict[str, Any]]:
    """The run's cell rows, last line per bench id winning, so a resumed run reads the same as a whole one."""
    p = Path(run_dir) / "cells.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if p.is_file():
        for line in p.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                out[str(row["bench_id"])] = row
    return out


def score_run(run_dir: Path, key: dict[str, dict[str, Any]], boot: int = BOOT, seed: int = 0) -> dict[str, Any]:
    """Every metric for one run: the label metrics over the labelled open cells, the probe abstain rate, the
    gate rejection rate, and cost and latency per cell. Refuses a run that holds a held-out cell."""
    rows = read_cells(run_dir)
    labelled: list[dict[str, Any]] = []
    probes: list[bool] = []
    unknown = 0
    for bench_id, r in rows.items():
        k = key.get(bench_id)
        if k is None:
            unknown += 1
            continue
        if k.get("split") == "heldout":
            raise PermissionError(f"run {run_dir} holds held-out cell {bench_id}; it is not scored")
        answer = r.get("answer") if isinstance(r.get("answer"), dict) else None
        published = bool(r.get("published")) and answer is not None
        verdict = str(answer.get("verdict")) if published else ""
        abstain = (not published) or verdict == ABSTAIN
        prob = float(answer["probability"]) if published and _number(answer.get("probability")) else NAN
        rec = {"label": k.get("label"), "stratum": k.get("stratum") or k.get("label"), "answered": answer is not None, "published": published,
               "verdict": verdict, "abstain": abstain, "p": prob,
               "cost": float(r.get("cost_usd") or 0.0), "duration": r.get("duration_s"),
               "from_cache": bool(r.get("from_cache"))}
        # the key's `label` is the label tier (deposit, occurrence, unlabelled); the stratum says what the
        # cell is for (a negative is an unlabelled drilled cell; a probe is never scored)
        if rec["stratum"] == "probe":
            probes.append(abstain)
        elif rec["stratum"] in LABEL_STRATA:
            labelled.append(rec)
    everything = [r for r in rows.values()]
    answered = sum(1 for r in everything if isinstance(r.get("answer"), dict))
    rejected = sum(1 for r in everything if isinstance(r.get("answer"), dict) and not r.get("published"))
    costs = [float(r.get("cost_usd") or 0.0) for r in everything]
    durations = [float(r["duration_s"]) for r in everything if _number(r.get("duration_s"))]
    out: dict[str, Any] = {
        "n_rows": len(rows), "n_labelled": len(labelled), "n_probe": len(probes), "n_unknown_key": unknown,
        "n_answered": answered, "n_rejected": rejected, "n_failed": len(rows) - answered,
        "gate_rejection_rate": rejected / answered if answered else NAN, "gate_rejection_denominator": answered,
        "probe_abstain_rate": float(np.mean(probes)) if probes else NAN,
        "cost_usd_total": float(sum(costs)), "cost_usd_per_cell": float(np.mean(costs)) if costs else NAN,
        "latency_s_per_cell": float(np.mean(durations)) if durations else NAN,
        "from_cache_share": float(np.mean([r.get("from_cache", False) for r in everything])) if everything else NAN,
    }
    out |= metrics(
        np.array([1 if r["label"] in POSITIVE_LABELS else 0 for r in labelled], dtype=int),
        np.array([r["p"] for r in labelled], dtype=float),
        [r["verdict"] for r in labelled],
        np.array([r["abstain"] for r in labelled], dtype=bool),
        boot=boot, seed=seed, strata=[r["stratum"] for r in labelled],
    )
    out |= stage_metrics(rows)
    return out


def stage_metrics(rows: dict[str, dict[str, Any]]) -> dict[str, float]:
    """The per-stage metrics of the staged loop, from the counts each v1 row carries under
    `stages`: the node gate's rejection rate over every executor attempt, the share of nodes recorded unknown
    after three attempts, the share of chains a round validated, the share the verifier refused at least once,
    rounds per chain and rounds over the chains that validated, re-executions per chain, how often the
    verifier's recorded label and the weighted decider agreed with the final verdict, the shallow-path share,
    and refusals per leakage rule. Flat floats under a `stage_` prefix so the MLflow flattener keeps
    them. Empty for a run without chains, so a v0 score is unchanged."""
    st = [r["stages"] for r in rows.values() if isinstance(r.get("stages"), dict)]
    if not st:
        return {}

    def total(key: str) -> float:
        return float(sum(float(s.get(key) or 0) for s in st))

    def rate(values: list[Any]) -> float:
        known = [float(v) for v in values if v is not None]
        return float(np.mean(known)) if known else NAN

    attempts, nodes = total("attempts_total"), total("n_nodes")
    out: dict[str, float] = {
        "stage_n_chains": float(len(st)),
        "stage_gate_rejection_rate": total("n_gate_rejections") / attempts if attempts else NAN,
        "stage_attempts_per_node": attempts / nodes if nodes else NAN,
        "stage_unknown_recorded_rate": total("n_recorded_unknown") / nodes if nodes else NAN,
        "stage_valid_rate": rate([bool(s.get("valid")) for s in st]),
        "stage_rounds_mean": rate([s.get("rounds") for s in st]),
        # the verifier caught a chain when its first round did not validate it; a chain with no round at all
        # (no verifier) was neither caught nor passed, so it is left out of the rate
        "stage_verifier_catch_rate": rate([None if not s.get("rounds") else not (s["rounds"] == 1 and bool(s.get("valid")))
                                           for s in st]),
        "stage_rounds_to_valid_mean": rate([s.get("rounds") for s in st if bool(s.get("valid")) and s.get("rounds")]),
        "stage_reexecuted_mean": rate([s.get("n_reexecuted") for s in st]),
        "stage_verifier_agreement_rate": rate([s.get("verifier_agreement") for s in st]),
        "stage_decider_agreement_rate": rate([s.get("decider_agreement") for s in st]),
        "stage_shallow_rate": rate([bool(s.get("shallow")) for s in st]),
    }
    rules: dict[str, float] = {}
    for s in st:
        for rule, n in (s.get("n_refusals_by_rule") or {}).items():
            rules[rule] = rules.get(rule, 0.0) + float(n or 0)
    out |= {f"stage_refusals_{rule}": n for rule, n in sorted(rules.items())}
    return out


# ---------------------------------------------------------------- the baselines

def verdicts_at(p: np.ndarray, threshold: float = 0.5) -> list[str]:
    return [ABSTAIN if not np.isfinite(x) else (POSITIVE if x >= threshold else NEGATIVE) for x in p]


def random_expected(base_rate: float, n: int, bins: int = BINS) -> dict[str, Any]:
    """What a uniform random probability at a 0.5 threshold scores in expectation, with no draw involved."""
    pi = float(base_rate)
    mids = (np.arange(bins) + 0.5) / bins
    return {
        "name": "random_expected", "kind": "baseline", "model": "random", "n_cells": int(n),
        "run_id": None, "mlflow_run_id": None, "cost_usd": 0.0,
        "precision": pi, "recall": 0.5, "f1": (2 * pi * 0.5 / (pi + 0.5)) if pi + 0.5 else NAN,
        "pr_auc": pi, "roc_auc": 0.5, "pr_auc_all": pi, "roc_auc_all": 0.5,
        "ece": float(np.mean(np.abs(pi - mids))), "abstain_rate": 0.0, **dict.fromkeys(STAGE_COLUMNS),
        "note": "analytic expectation for a uniform random probability at a 0.5 threshold; no interval",
    }


def _row(name: str, model: str, y: np.ndarray, p: np.ndarray, abstain: np.ndarray, strata: list[str],
         boot: int, seed: int, **extra: Any) -> dict[str, Any]:
    m = metrics(y, p, verdicts_at(p), abstain, boot=boot, seed=seed, strata=strata)
    return {"name": name, "kind": "baseline", "model": model, "n_cells": int(len(y)), "run_id": None,
            "mlflow_run_id": None, "cost_usd": 0.0, **dict.fromkeys(STAGE_COLUMNS), **m, **extra}


def bench_oof_scores(version: str, cell_ids: list[str]) -> tuple[dict[str, dict[str, float]], str | None]:
    """Out-of-fold scores by model from the benchmark's own `oof_scores.csv`, which the build writes under the
    spec's seed and folds. The store's table is filled by `ue bench oof-scores --write` under its own seed, a
    different draw of folds and background, so a benchmark's baselines are read from the benchmark."""
    bench = F.load_bench(version)
    path = bench.dir / "oof_scores.csv"
    if not path.is_file():
        return {}, f"{path} is missing"
    cell_of = {str(c["bench_id"]): str(c["cell_id"]) for c in bench.cells}
    wanted = set(cell_ids)
    df = pd.read_csv(path)
    out: dict[str, dict[str, float]] = {}
    for bid, model, score in df[["bench_id", "model", "score"]].itertuples(index=False):
        cid = cell_of.get(str(bid))
        if cid in wanted and score == score:
            out.setdefault(str(model), {})[cid] = float(score)
    return out, None if out else f"{path} holds no scores for these cells"


def oof_scores(con: Any, cell_ids: list[str], fold_kind: str = FOLD_KIND) -> tuple[dict[str, dict[str, float]], str | None]:
    """Out-of-fold scores by model for the given cells, or an empty map and the reason."""
    exists = con.execute(
        "select count(*) from information_schema.tables where table_schema = 'derived' and table_name = 'cell_score_oof'"
    ).fetchone()[0]
    if not exists:
        return {}, "derived.cell_score_oof is missing; the fitted baselines need the out-of-fold scorer to have run"
    con.register("ue_bench_cells", pd.DataFrame({"cell_id": list(cell_ids)}))
    try:
        rows = con.execute(
            "select s.cell_id, s.model, avg(s.score) from derived.cell_score_oof s join ue_bench_cells b using (cell_id) "
            "where s.fold_kind = ? and s.score is not null group by 1, 2", [fold_kind]
        ).fetchall()
    finally:
        con.unregister("ue_bench_cells")
    out: dict[str, dict[str, float]] = {}
    for cell_id, model, score in rows:
        out.setdefault(str(model), {})[str(cell_id)] = float(score)
    return out, None if out else f"derived.cell_score_oof has no {fold_kind!r} scores for these cells"


def baselines(version: str, con: Any, boot: int = BOOT, seed: int = 0, fold_kind: str = FOLD_KIND,
              log: Callable[[str], None] = lambda _m: None) -> list[dict[str, Any]]:
    """Rows on the open labelled cells of the benchmark: chance twice, an analyst copying the learned score,
    and the three fitted scores at a 0.5 threshold."""
    bench = F.load_bench(version)
    def stratum(bench_id: str) -> str | None:
        k = bench.key.get(bench_id, {})
        return k.get("stratum") or k.get("label")

    cells = [c for c in bench.open_cells() if stratum(c["bench_id"]) in LABEL_STRATA]
    if not cells:
        return []
    # `labels` here are the strata the rows are broken down by; y comes from the label tier
    labels = [stratum(c["bench_id"]) for c in cells]
    y = np.array([1 if bench.key[c["bench_id"]].get("label") in POSITIVE_LABELS else 0 for c in cells], dtype=int)
    ids = [str(c["cell_id"]) for c in cells]
    n = len(y)
    never = np.zeros(n, dtype=bool)
    rows = [random_expected(float(y.mean()), n)]
    rng = np.random.default_rng(seed)
    rows.append(_row("random", "random", y, rng.random(n), never, labels, boot, seed,
                     note=f"one uniform draw, seed {seed}, 0.5 threshold"))
    scores, note = bench_oof_scores(version, ids)
    if note:
        log(f"  {note}; falling back to the store's out-of-fold table")
        scores, note = oof_scores(con, ids, fold_kind)
    if note:
        log(f"  {note}")
        return rows
    learned = scores.get("learned", {})
    p = np.array([learned.get(c, NAN) for c in ids], dtype=float)
    rows.append(_row("copy_learned", "learned", y, p, ~np.isfinite(p), labels, boot, seed,
                     n_missing=int((~np.isfinite(p)).sum()),
                     note="an analyst that copies the out-of-fold learned score and abstains where there is none"))
    for model in (*BASELINE_MODELS, *((EXTENDED,) if EXTENDED in scores else ())):
        s = scores.get(model, {})
        raw = np.array([s.get(c, NAN) for c in ids], dtype=float)
        rows.append(_row(model, model, y, np.where(np.isfinite(raw), raw, 0.5), never, labels, boot, seed,
                         n_missing=int((~np.isfinite(raw)).sum()),
                         note=f"the out-of-fold {model} score at a 0.5 threshold ({fold_kind} folds); a missing score is 0.5"))
    return rows


# ---------------------------------------------------------------- the table

def _effort_of(summary: dict[str, Any]) -> str | None:
    """The reasoning effort the arm ran at: in the summary, or in the run's manifest for runs written before
    the summary carried it (the manifest's config is the arm file as loaded)."""
    if summary.get("effort"):
        return str(summary["effort"])
    rd = summary.get("run_dir")
    if rd and (Path(rd) / "manifest.json").is_file():
        try:
            cfg = json.loads((Path(rd) / "manifest.json").read_text()).get("config") or {}
            return str(cfg["effort"]) if cfg.get("effort") else None
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _stages_of(summary: dict[str, Any]) -> dict[str, float]:
    """The per-stage metrics from the run's own cells rather than from the summary's score, so a run scored
    before a stage column existed still fills it; the summary's score stands for everything else. Empty when
    the run directory is gone, or the run has no chains, and a v0 score is then unchanged."""
    rd = summary.get("run_dir")
    if not rd or not (Path(rd) / "cells.jsonl").is_file():
        return {}
    return stage_metrics(read_cells(Path(rd)))


def sample_name(arm: str, sample: int = 0) -> str:
    """The row and pointer name of one sample of an arm: the arm's own name for sample 0, `<arm>~s<N>` after."""
    return f"{arm}~s{int(sample)}" if sample else arm


def arm_row(summary: dict[str, Any], key: dict[str, dict[str, Any]] | None = None, boot: int = BOOT,
            seed: int = 0) -> dict[str, Any]:
    """A table row from the summary `run_arm` wrote for an arm: its score, the per-stage columns re-derived
    from the run's cells, and null under a stage the arm never had (a v0 arm has no verifier and no rounds),
    so every row carries the same columns. Given the key, the score is recomputed from the run's own cells
    with the scorer as it stands, so a change to the scoring reaches every arm at once, not only the arms run
    after it; without the key, or without the run's cells, the score as written stands."""
    rd = summary.get("run_dir")
    fresh = score_run(Path(rd), key, boot=boot, seed=seed) if key is not None and rd and (Path(rd) / "cells.jsonl").is_file() else None
    score = dict(fresh or summary.get("score") or {}) | _stages_of(summary)
    stages = {k: float(score[k]) if _number(score.get(k)) else None for k in STAGE_COLUMNS}
    return {"name": sample_name(summary["arm"], int(summary.get("sample") or 0)), "kind": "arm",
            "model": summary.get("model"), "effort": _effort_of(summary),
            "n_cells": int(score.get("n", 0)), "run_id": summary.get("run_id"),
            "mlflow_run_id": summary.get("mlflow_run_id"), "cost_usd": float(score.get("cost_usd_total") or 0.0),
            "pending": len(summary.get("pending") or []), **score, **stages}


def table(version: str, con: Any = None, boot: int = BOOT, seed: int = 0, write: bool = True,
          log: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Arms and baselines in one table: `data/out/bench/<version>/table.json`, and the store's metric rows."""
    from . import compare as CMP

    d = out_dir(version)
    arm_files = sorted((d / "arms").glob("*.json")) if (d / "arms").is_dir() else []
    summaries = [json.loads(p.read_text()) for p in arm_files]
    key = F.load_bench(version).key
    rows = [arm_row(s, key, boot=boot, seed=seed) for s in summaries]
    own = con is None
    con = con if con is not None else connect()
    try:
        rows += baselines(version, con, boot=boot, seed=seed, log=log)
        derived, contrasts = CMP.extras(version, summaries, boot=boot, seed=seed)
        rows += derived
        by_own = CMP.own_columns(version, summaries, boot=boot, seed=seed)
        rows = [r | by_own.get(r["name"], {}) for r in rows]
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        out = {"version": version, "computed_at": now, "manifest_sha256": F.load_bench(version).manifest_sha256,
               "rows": rows, "contrasts": contrasts}
        if write:
            d.mkdir(parents=True, exist_ok=True)
            (d / "table.json").write_text(json.dumps(TR.jsonable(out), indent=1) + "\n")
            write_metrics(rows, version, con, now)
    finally:
        if own:
            con.close()
    return out


def write_metrics(rows: list[dict[str, Any]], version: str, con: Any, now: str | None = None) -> int:
    """`derived.metric` rows under `bench.<version>.<row>.<metric>`, replacing the version's earlier rows,
    citing the arm's run id where there is one and the timestamp otherwise."""
    now = now or dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    out = []
    for r in rows:
        for key in TABLE_METRICS:
            val = r.get(key)
            if not _number(val) or not math.isfinite(float(val)):
                continue
            ci = r.get(f"{key}_ci")
            out.append({
                "metric_key": f"bench.{version}.{r['name']}.{key}", "run_id": r.get("run_id") or now,
                "value": float(val), "computed_at": now,
                "fmt": STAGE_COLUMNS.get(key) or ("usd" if key.startswith("cost") else ("s1" if key.startswith("latency") else "ratio3")),
                "note": f"{key}; {r['kind']} {r['name']} ({r.get('model')}) on {r.get('n_cells')} open cells of "
                        f"benchmark {version}"
                        + (f"; 95% CI {ci[0]:.3f}-{ci[1]:.3f}" if isinstance(ci, list) and len(ci) == 2
                           and all(_number(x) and math.isfinite(x) for x in ci) else ""),
            })
    con.execute("delete from derived.metric where metric_key like ?", [f"bench.{version}.%"])
    if out:
        append_frame(con, "derived", "metric", pd.DataFrame(out), "derived")
    return len(out)

"""Phase 0: the two sampling corrections, the extra metrics, the MineTRACE protocol, and the verdict.

Every model fit here is a stand-in that scores by distance to conductor, so the tests exercise the sampling
and the bookkeeping, not scikit-learn."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from legacy_reader.prospect import headline as H
from legacy_reader.prospect import models as M


def frame(n: int = 1200, seed: int = 0) -> pd.DataFrame:
    """Positives cluster in two camps and sit where effort is high, as in the real store."""
    rng = np.random.default_rng(seed)
    cx = rng.uniform(0, 200_000, n)
    cy = rng.uniform(0, 100_000, n)
    d_conductor = rng.uniform(0, 20_000, n)
    camp = np.where(cx < 100_000, 0, 1)
    positive = (d_conductor < 2_500) & (rng.random(n) < 0.7)
    tier = np.where(positive, np.where(rng.random(n) < 0.3, "deposit", "occurrence"), "unlabelled")
    holes = np.where(positive, rng.integers(5, 60, n), rng.integers(0, 3, n)).astype(float)
    df = pd.DataFrame({
        "cell_id": [f"c{i:05d}" for i in range(n)], "label_tier": tier,
        "camp_id": np.where(positive, camp, -1),
        "block_id": (np.floor(cx / 30_000) * 100 + np.floor(cy / 30_000)).astype(int),
        "lon": -108 + cx / 60_000, "lat": 57 + cy / 111_000, "cx": cx, "cy": cy,
        "d_conductor_m": d_conductor, "d_fault_m": rng.uniform(0, 20_000, n), "fault_density": rng.random(n),
        "graphitic_host": rng.integers(0, 2, n).astype(float), "unconformity_depth_m": rng.uniform(50, 900, n),
        "water_fraction": rng.random(n), "vegetation_fraction": rng.random(n), "bare_fraction": rng.random(n),
        "elevation_m": rng.uniform(200, 600, n), "relief_m": rng.uniform(0, 200, n),
        "holes_n": holes, "holes_first_year": rng.uniform(1950, 2020, n),
        "sed_samples_n": rng.integers(0, 9, n).astype(float), "boulder_samples_n": rng.integers(0, 4, n).astype(float),
        "airborne_surveys_n": rng.integers(0, 5, n).astype(float), "ground_surveys_n": rng.integers(0, 5, n).astype(float),
    })
    return df


def fake_fit(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
    """Scores by the first column (distance to conductor for the learned set, holes for the effort set)."""
    col = x_test[:, 0]
    return 1.0 - (col - col.min()) / (np.ptp(col) or 1.0) if col.max() < 25_000 and col.max() > 100 else \
        (col - col.min()) / (np.ptp(col) or 1.0)


# ---------------------------------------------------------------- corrections


def test_effort_index_is_in_unit_range_and_rises_with_holes() -> None:
    df = frame()
    e = H.effort_index(df)
    assert 0.0 <= e.min() and e.max() <= 1.0
    assert e[df["holes_n"] > 20].mean() > e[df["holes_n"] == 0].mean()


def test_matched_background_keeps_every_positive_and_matches_the_effort_deciles() -> None:
    df = frame()
    y = H.labels(df, "all")
    keep = H.matched_background(df, y, seed=1, per_positive=3)
    assert keep[y == 1].all(), "no positive is dropped"
    assert keep[y == 0].sum() < (y == 0).sum(), "fewer negatives than the naive everything"
    e = H.effort_index(df)
    edges = np.quantile(e, np.linspace(0, 1, 11))
    dec = np.clip(np.searchsorted(edges, e, side="right") - 1, 0, 9)
    pos_share = np.bincount(dec[y == 1], minlength=10) / (y == 1).sum()
    neg_share = np.bincount(dec[keep & (y == 0)], minlength=10) / (keep & (y == 0)).sum()
    naive_share = np.bincount(dec[y == 0], minlength=10) / (y == 0).sum()
    assert np.abs(pos_share - neg_share).max() < np.abs(pos_share - naive_share).max(), \
        "the kept negatives follow the positives' effort profile more closely than all negatives do"


def test_thinned_positives_keeps_one_per_block_and_every_negative() -> None:
    df = frame()
    y = H.labels(df, "all")
    keep = H.thinned_positives(df, y, block_m=10_000, seed=0)
    assert keep[y == 0].all()
    kept = df[keep & (y == 1)]
    blocks = list(zip(np.floor(kept["cx"] / 10_000), np.floor(kept["cy"] / 10_000), strict=True))
    assert len(blocks) == len(set(blocks)), "at most one positive per 10 km block"
    assert 0 < len(kept) < (y == 1).sum()


# ---------------------------------------------------------------- metrics


def test_capture_at_budgets_is_one_for_a_perfect_ranking_and_near_budget_for_a_random_one() -> None:
    rng = np.random.default_rng(0)
    y = np.zeros(2000, dtype=int)
    y[:40] = 1
    perfect = np.where(y == 1, 1.0, 0.0) + rng.random(2000) * 1e-3
    assert H.capture_at_budgets(y, perfect)["capture_top5"] == 1.0
    random = rng.random(2000)
    c = H.capture_at_budgets(y, random)
    assert 0.02 < c["capture_top10"] < 0.25 and c["capture_top5"] <= c["capture_top10"] <= c["capture_top15"]


def test_bootstrap_interval_brackets_the_point_estimate() -> None:
    rng = np.random.default_rng(0)
    y = (rng.random(600) < 0.1).astype(int)
    p = np.clip(y * 0.4 + rng.random(600), 0, 1)
    point = M.roc_auc(y, p)
    lo, hi = H.bootstrap_ci(y, p, M.roc_auc, n=100, seed=0)
    assert lo <= point <= hi and hi - lo > 0


def test_minetrace_protocol_holds_out_whole_blocks_and_never_trains_on_them() -> None:
    df = frame(n=3000, seed=2)
    y = H.labels(df, "all")
    seen: list[tuple[int, int, int]] = []

    def spy(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
        seen.append((int(y_train.sum()), len(x_test), int(len(x_train))))
        return fake_fit(x_train, y_train, x_test)

    out = H.minetrace_protocol(df, "learned", y, repeats=3, n_pos=30, n_neg=200, seed=0, fit=spy)
    assert len(seen) == 3
    for n_train_pos, n_test, n_train in seen:
        assert n_test >= 230, "30 held-out positives (whole blocks, so possibly a few more) plus 200 negatives"
        assert n_train_pos == y.sum() - (n_test - 200)
        assert n_train + n_test == len(df)
    assert 0.5 < out["roc_auc_mean"] <= 1.0 and "3 repeats" in out["protocol"]


# ---------------------------------------------------------------- one configuration, and the table


def test_a_configuration_reports_metrics_intervals_and_its_key() -> None:
    df = frame()
    cfg = H.Config("learned", "all", matched=True, thinned=True, fold="spatial")
    r = H.evaluate_config(df, cfg, seed=0, boot=30, fit=fake_fit)
    assert r["config"] == "learned.all.matched+thinned.spatial"
    assert r["cells"] < len(df) and r["n_pos"] > 0 and r["n_deposits"] > 0
    assert 0 < r["pr_auc"] <= 1 and r["pr_auc_ci"][0] <= r["pr_auc"] <= r["pr_auc_ci"][1]
    assert set(("capture_top5", "capture_top10", "capture_top15")) <= set(r)


def test_the_naive_key_and_deposit_labels() -> None:
    df = frame()
    assert H.Config("effort", "deposits", False, False, "random").key == "effort.deposits.naive.random"
    assert H.labels(df, "deposits").sum() < H.labels(df, "all").sum()


def test_run_covers_every_configuration_and_writes_nothing_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    df = frame()
    monkeypatch.setattr(M, "matrix", lambda fs, grid_id=None: df.copy())
    wrote = []
    monkeypatch.setattr(H, "_write_metrics", lambda out, now: wrote.append(now))
    cfgs = H.configs(folds=("spatial",))
    out = H.run(seed=0, boot=20, log=lambda *a: None, write=False, fit=fake_fit, cfgs=cfgs)
    assert len(out["rows"]) == len(cfgs) == 16
    assert set(out["minetrace"]) == {"learned", "effort"}
    assert out["verdict"]["text"].startswith("after matched background and thinned positives")
    assert wrote == []

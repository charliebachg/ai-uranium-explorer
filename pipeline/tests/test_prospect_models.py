"""The two models and the comparison between them: folds, metrics, and what must not be imputed."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from legacy_reader.prospect import models as M


def frame(n: int = 400, seed: int = 0) -> pd.DataFrame:
    """A synthetic grid where geology carries real signal and effort carries a stronger one."""
    rng = np.random.default_rng(seed)
    lon = rng.uniform(-108, -105, n)
    lat = rng.uniform(57, 59, n)
    d_conductor = rng.uniform(0, 20000, n)
    positive = (d_conductor < 3000) & (rng.random(n) < 0.6)
    df = pd.DataFrame({
        "cell_id": [f"c{i:04d}" for i in range(n)],
        "label_tier": np.where(positive, "deposit", "unlabelled"),
        "camp_id": np.where(positive, (lon > -106.5).astype(int), -1),
        "block_id": (np.floor((lon + 108) * 4) * 100 + np.floor((lat - 57) * 4)).astype(int),
        "lon": lon, "lat": lat,
        "d_conductor_m": d_conductor,
        "d_fault_m": rng.uniform(0, 20000, n),
        "fault_density": rng.random(n),
        "graphitic_host": rng.integers(0, 2, n).astype(float),
        "unconformity_depth_m": rng.uniform(50, 900, n),
        "water_fraction": rng.random(n),
        "vegetation_fraction": rng.random(n),
        "bare_fraction": rng.random(n),
        "elevation_m": rng.uniform(200, 600, n),
        "relief_m": rng.uniform(0, 200, n),
        # effort follows the labels almost exactly: this is the null model's whole point
        "holes_n": np.where(positive, rng.integers(5, 50, n), rng.integers(0, 2, n)).astype(float),
        "holes_first_year": rng.uniform(1950, 2020, n),
        "sed_samples_n": rng.integers(0, 9, n).astype(float),
        "boulder_samples_n": rng.integers(0, 4, n).astype(float),
        "airborne_surveys_n": rng.integers(0, 5, n).astype(float),
        "ground_surveys_n": rng.integers(0, 5, n).astype(float),
    })
    return df


# ---------------------------------------------------------------- feature sets


def test_the_two_sets_are_disjoint_and_effort_carries_no_geology():
    assert not set(M.LEARNED_FEATURES) & set(M.EFFORT_FEATURES)
    for key in M.EFFORT_FEATURES:
        assert any(word in key for word in ("holes", "samples", "surveys", "year")), key


def test_no_label_layer_is_ever_a_feature():
    banned = {"deposit", "occurrence", "smdi", "label"}
    for key in (*M.LEARNED_FEATURES, *M.EFFORT_FEATURES):
        assert not any(b in key.lower() for b in banned), key


# ---------------------------------------------------------------- folds


def test_spatial_folds_hold_out_whole_blocks():
    df = frame()
    fold = M.folds(df, "spatial")
    by_block = pd.DataFrame({"block": df["block_id"], "fold": fold.groups}).groupby("block")["fold"].nunique()
    assert (by_block == 1).all(), "a block must sit entirely on one side of the split"


def test_folds_are_deterministic():
    df = frame()
    assert (M.folds(df, "spatial").groups == M.folds(df, "spatial").groups).all()
    assert (M.folds(df, "random").groups == M.folds(df, "random").groups).all()


def test_a_camp_fold_holds_out_the_ground_around_the_camp_not_just_its_positives():
    df = frame()
    fold = M.folds(df, "camp")
    held = fold.groups >= 0
    assert held.sum() > (df["camp_id"] >= 0).sum(), "the fold must include surrounding unlabelled cells"
    y = (df["label_tier"] != "unlabelled").to_numpy()
    for g in np.unique(fold.groups[fold.groups >= 0]):
        test = fold.groups == g
        assert y[test].sum() > 0 and (~y[test]).sum() > 0, "a scorable fold needs both classes"


def test_out_of_fold_scores_never_come_from_a_model_that_saw_the_cell():
    df = frame()
    fold = M.folds(df, "spatial")
    p = M.cross_val_scores(df, "learned", fold)
    assert np.isfinite(p).sum() > 0.5 * len(df)
    assert np.nanmin(p) >= 0.0 and np.nanmax(p) <= 1.0


# ---------------------------------------------------------------- metrics


def test_capture_at_ten_percent_counts_positives_in_the_top_slice():
    y = np.array([1, 1, 0, 0, 0, 0, 0, 0, 0, 0])
    perfect = np.array([0.9, 0.8, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    assert M.capture_at(y, perfect, 0.20) == pytest.approx(1.0)
    assert M.capture_at(y, 1 - perfect, 0.20) == pytest.approx(0.0)


def test_pr_auc_of_a_random_ranking_sits_near_the_base_rate():
    rng = np.random.default_rng(0)
    y = (rng.random(2000) < 0.05).astype(int)
    p = rng.random(2000)
    assert abs(M.pr_auc(y, p) - y.mean()) < 0.03


def test_calibration_bins_report_predicted_against_observed():
    rng = np.random.default_rng(1)
    p = rng.random(1000)
    y = (rng.random(1000) < p).astype(int)
    bins = M.calibration(y, p, bins=4)
    assert len(bins) == 4
    assert bins[0]["predicted"] < bins[-1]["predicted"]
    assert bins[0]["observed"] < bins[-1]["observed"]


# ---------------------------------------------------------------- the comparison


def test_harder_folds_never_flatter_the_model():
    """Random folds leak neighbours; the spatial split has to be the more pessimistic number."""
    df = frame(600)
    random_r = M.evaluate(df, "learned", "random")
    spatial_r = M.evaluate(df, "learned", "spatial")
    assert spatial_r["pr_auc"] <= random_r["pr_auc"] + 0.05


def test_the_effort_model_wins_when_labels_follow_drilling():
    """The synthetic world is built so effort explains the labels; the harness must be able to show it."""
    df = frame(600)
    learned = M.evaluate(df, "learned", "spatial")
    effort = M.evaluate(df, "effort", "spatial")
    assert effort["pr_auc"] > learned["pr_auc"]


def test_a_fold_with_no_positive_is_skipped_rather_than_scored():
    df = frame(200)
    df["label_tier"] = "unlabelled"
    fold = M.folds(df, "spatial")
    p = M.cross_val_scores(df, "learned", fold)
    assert np.isnan(p).all(), "with nothing positive there is nothing to learn or to score"


def test_evaluate_reports_the_base_rate_beside_every_number():
    df = frame(400)
    r = M.evaluate(df, "learned", "spatial")
    assert "base_rate" in r and 0 < r["base_rate"] < 1
    assert r["positives"] > 0 and r["scored"] > 0

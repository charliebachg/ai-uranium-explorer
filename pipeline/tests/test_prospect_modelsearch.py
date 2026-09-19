"""Phase 3: the candidates run, the folds hold, the promotion rule decides, and nothing is served today."""

from __future__ import annotations

import numpy as np
import pytest

from legacy_reader.prospect import headline as H
from legacy_reader.prospect import modelsearch as MS
from legacy_reader.prospect import models as M
from legacy_reader.prospect import tracking as TR
from test_prospect_headline import frame


def synthetic():
    df = frame(n=900, seed=3)
    df["criteria_score"] = (1.0 - df["d_conductor_m"] / 20_000).clip(0, 1)
    MS.register_feature_sets(df)
    return df


def test_feature_sets_cover_the_candidates_and_the_ablations() -> None:
    synthetic()
    assert M.FEATURE_SETS["learned+xy"][-2:] == ("lon", "lat")
    assert "criteria_score" in M.FEATURE_SETS["learned+criteria"]
    assert set(M.EFFORT_FEATURES) <= set(M.FEATURE_SETS["learned+effort"])
    for group, cols in MS.GROUPS.items():
        assert not set(cols) & set(M.FEATURE_SETS[f"learned-{group}"]), group


@pytest.mark.parametrize("name", ["histgb", "random_forest", "logistic_spatial", "bagging_pu", "criteria_prior", "effort"])
def test_every_candidate_fits_and_returns_probabilities(name: str) -> None:
    df = synthetic()
    fs, fit = MS.CANDIDATES[name]
    x = df[list(M.FEATURE_SETS[fs])].to_numpy(dtype=float)
    y = H.labels(df, "all")
    p = fit(x[:600], y[:600], x[600:])
    assert p.shape == (300,) and np.all((p >= 0) & (p <= 1))
    assert M.roc_auc(y[600:], p) > 0.6, "geology carries signal in the synthetic frame; every learner should find some"


def test_spatial_folds_at_other_block_sizes_hold_whole_blocks_out() -> None:
    df = synthetic()
    for km in MS.BLOCK_KM:
        fold = MS.spatial_fold(df, km, n_splits=5)
        assert fold.name == f"spatial{km}" and set(np.unique(fold.groups)) <= set(range(5))
        bx = np.floor(df["cx"] / (km * 1000)).astype(int)
        by = np.floor(df["cy"] / (km * 1000)).astype(int)
        blocks = bx * 100_000 + by
        for b in np.unique(blocks):
            assert len(np.unique(fold.groups[blocks == b])) == 1, "a block never straddles two folds"


def test_an_arm_reports_the_same_metrics_as_the_headline_with_intervals() -> None:
    df = synthetic()
    arm = MS.Arm("histgb", "learned", MS.fit_histgb, "spatial")
    r = MS.evaluate_arm(df, arm, seed=0, boot=20)
    assert r["arm"] == "histgb.all.spatial" and r["cells"] == len(df) and r["train_cells"] < len(df)
    assert 0 < r["pr_auc"] <= 1 and r["pr_auc_ci"][0] <= r["pr_auc"] <= r["pr_auc_ci"][1]
    assert set(("capture_top5", "capture_top10", "capture_top15")) <= set(r)


def test_the_promotion_rule_needs_intervals_apart() -> None:
    assert TR.beats_null({"pr_auc_ci": (0.30, 0.40)}, {"pr_auc_ci": (0.10, 0.25)}) is True
    assert TR.beats_null({"pr_auc_ci": (0.30, 0.40)}, {"pr_auc_ci": (0.10, 0.32)}) is False
    assert TR.beats_null({"pr_auc_ci": (0.30, 0.40)}, {}) is False


def test_quick_run_decides_without_tracking_or_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    df = synthetic()
    monkeypatch.setattr(TR, "record_decision", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not register")))
    out = MS.run(quick=True, boot=10, log=lambda *a: None, write=False, track=False, df=df)
    assert len(out["rows"]) == 4 and {r["name"] for r in out["rows"]} == {"histgb", "effort", "random_forest", "bagging_pu"}
    assert out["decision"]["best"] in {"histgb", "random_forest", "bagging_pu"}
    assert out["decision"]["served"] is False and "against the effort null" in out["decision"]["reason"]


def test_the_decision_never_promotes_an_arm_that_carries_effort_features() -> None:
    rows = [
        {"name": "effort", "fold": "spatial", "pr_auc": 0.18, "pr_auc_ci": (0.16, 0.20), "features": list(M.EFFORT_FEATURES)},
        {"name": "learned+effort", "fold": "spatial", "pr_auc": 0.30, "pr_auc_ci": (0.28, 0.32),
         "features": [*M.LEARNED_FEATURES, *M.EFFORT_FEATURES]},
        {"name": "random_forest", "fold": "spatial", "pr_auc": 0.12, "pr_auc_ci": (0.11, 0.14), "features": list(M.LEARNED_FEATURES)},
    ]
    d = MS.decide(rows, log=lambda *a: None, track=False)
    assert d["best"] == "random_forest" and d["validated"] is False

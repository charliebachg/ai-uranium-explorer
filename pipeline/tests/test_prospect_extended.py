"""The learned model given the extended evidence: the sets, the missingness check and the paired differences."""

from __future__ import annotations

import numpy as np
import pytest

from uranium_explorer.prospect import extended as X
from uranium_explorer.prospect import models as M
from conftest import skip_without_store
from test_prospect_headline import frame


def synthetic(n: int = 900, seed: int = 5):
    """The headline's synthetic frame plus every extended feature: one that carries signal and is missing where
    nothing was sampled, the rest noise."""
    df = frame(n=n, seed=seed)
    rng = np.random.default_rng(seed)
    pos = (df["label_tier"] != "unlabelled").to_numpy()
    for k in X.EXTENDED_FEATURES:
        df[k] = rng.random(n)
    df["sed_u_th_max"] = np.where(pos, rng.uniform(2, 6, n), rng.uniform(0, 3, n))
    df.loc[rng.random(n) < 0.5, "sed_u_th_max"] = np.nan
    df.loc[rng.random(n) < 0.3, "boulder_max_cps"] = np.nan
    df = X.with_missing(df)
    X.register_feature_sets(df)
    return df


def test_the_sets_are_the_learned_model_plus_each_family_and_the_missingness_check():
    df = synthetic()
    assert M.FEATURE_SETS["extended"][:len(M.LEARNED_FEATURES)] == M.LEARNED_FEATURES
    assert set(X.EXTENDED_FEATURES) <= set(M.FEATURE_SETS["extended"])
    for fam, keys in X.FAMILIES.items():
        assert M.FEATURE_SETS[f"learned+{fam}"] == (*M.LEARNED_FEATURES, *keys)
    missing = M.FEATURE_SETS["missing-only"]
    assert set(missing) == {"missing:sed_u_th_max", "missing:boulder_max_cps"}, "only features that are ever missing"
    assert set(np.unique(df[list(missing)].to_numpy())) <= {0.0, 1.0}
    assert not set(M.EFFORT_FEATURES) & set(M.FEATURE_SETS["extended"]), "the geology sets never read effort"


def test_a_paired_difference_is_positive_when_one_ranking_is_better_on_the_same_cells():
    rng = np.random.default_rng(0)
    y = (rng.random(400) < 0.3).astype(int)
    good = y + rng.normal(0, 0.3, 400)
    noise = rng.random(400)
    d = X.paired_difference(y, good, noise, M.pr_auc, n=200)
    assert d["diff"] > 0.3 and d["diff_ci"][0] > 0 and d["p_not_better"] == 0.0 and d["cells"] == 400
    same = X.paired_difference(y, noise, noise, M.pr_auc, n=50)
    assert same["diff"] == 0.0 and same["diff_ci"] == [0.0, 0.0]
    gap = noise.copy()
    gap[:10] = np.nan
    assert X.paired_difference(y, gap, noise, M.pr_auc, n=10)["cells"] == 390, "only cells both sets scored"


def quick_fit(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
    """The same learner, smaller: it still takes a missing value without imputing it."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    from threadpoolctl import threadpool_limits

    model = HistGradientBoostingClassifier(max_iter=25, max_depth=3, class_weight="balanced", random_state=0)
    with threadpool_limits(1):   # a small fit spends longer starting threads than using them
        return model.fit(x_train, y_train).predict_proba(x_test)[:, 1]


def test_every_set_is_scored_over_the_grid_with_its_paired_difference_from_the_learned_model(monkeypatch):
    monkeypatch.setattr(X.MS, "fit_histgb", quick_fit)
    df = synthetic(n=600)
    sets = ("learned", "extended", "learned+geochem", "missing-only")
    rows, scores = X.grid_rows(df, boot=20, log=lambda _m: None, sets=sets)
    assert [(r["feature_set"], r["positives"]) for r in rows] == [(s, p) for p in X.POSITIVES for s in sets]
    for r in rows:
        if "pr_auc" not in r:
            continue
        assert ("vs_learned" in r) == (r["feature_set"] != "learned")
        assert len(r["pr_auc_ci"]) == 2 and r["scored"] == len(df)
    all_rows = {r["feature_set"]: r for r in rows if r["positives"] == "all"}
    # the one informative extended feature is in the geochemistry family
    assert all_rows["learned+geochem"]["vs_learned"]["diff"] > 0


def test_the_benchmark_rows_score_the_open_labelled_cells_under_the_benchmarks_own_seed():
    skip_without_store()
    from uranium_explorer.paths import PATHS

    if not (PATHS.data / "bench" / "v2" / "oof_scores.csv").is_file():
        pytest.skip("benchmark v2 is not built on this machine")
    df = X.load()
    rows = X.bench_rows(df, "v2", boot=20, log=lambda _m: None, spread_seeds=(), sets=("learned", "effort"))
    assert [r["feature_set"] for r in rows] == ["learned", "effort"]
    assert all(r["cells"] == 114 and not r["not_in_frame"] for r in rows)
    effort = next(r for r in rows if r["feature_set"] == "effort")
    rec = X.reconcile_effort("v2")
    # the benchmark's CSV was written under the spec's seed, and this recomputes it exactly
    assert effort["pr_auc"] == pytest.approx(rec["bench_csv"]["pr_auc"], abs=1e-9)


def test_a_metric_name_holds_only_the_characters_the_tracker_accepts():
    import re

    for fs in X.SETS:
        name = X.metric_name({"scope": "bench:v2", "feature_set": fs, "positives": "all"})
        assert re.fullmatch(r"[A-Za-z0-9_.\- :/]+", name), name

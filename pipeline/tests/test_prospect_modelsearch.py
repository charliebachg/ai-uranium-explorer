"""Phase 3: the candidates run, the folds hold, the promotion rule decides, and nothing is served today."""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from legacy_reader.prospect import headline as H
from legacy_reader.prospect import modelsearch as MS
from legacy_reader.prospect import models as M
from legacy_reader.prospect import tracking as TR
from legacy_reader.store import snapshot as SN
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


# ---------------------------------------------------------------- the run names its store


def test_run_refuses_a_snapshot_nobody_took_before_fitting_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MS, "evaluate_arm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fit")))
    with pytest.raises(FileNotFoundError):
        MS.run(quick=True, boot=10, log=lambda *a: None, write=False, track=False, df=synthetic(), snapshot="nope")


def test_run_names_its_snapshot_in_the_result_every_arm_the_decision_and_modelsearch_json(
        monkeypatch: pytest.MonkeyPatch, prospect_sandbox) -> None:
    sha = SN.take(log=lambda *a: None, path=prospect_sandbox.make_store())["store_sha256"]
    logged: list[tuple] = []
    decided: list[str | None] = []
    monkeypatch.setattr(TR, "log_run", lambda name, params, metrics, tags=None, artifacts=None:
                        logged.append((name, params, tags)) or f"run-{len(logged)}")
    monkeypatch.setattr(TR, "record_decision", lambda run_id, model, validated, reason, store_sha256=None:
                        decided.append(store_sha256) or {"run_id": run_id, "store_sha256": store_sha256, "stage": "candidate"})
    monkeypatch.setattr(MS, "_write_metrics", lambda rows, now: None)
    out = MS.run(quick=True, boot=10, log=lambda *a: None, write=True, track=True, df=synthetic(), snapshot=sha[:12])
    assert out["store_sha256"] == sha and out["snapshot"] == sha[:12]
    assert len(logged) == 4 and all(p["store_sha256"] == sha and t["snapshot"] == sha[:12] for _, p, t in logged)
    assert decided == [sha] and out["decision"]["store_sha256"] == sha
    written = json.loads((prospect_sandbox.out / "modelsearch.json").read_text())
    assert written["store_sha256"] == sha and written["snapshot"] == sha[:12] and written["run_id"] == out["run_id"]
    assert written["quick"] is True and written["seed"] == 0 and written["boot"] == 10 and written["cells"] == 900
    assert {r["run_id"] for r in written["rows"]} == {"run-1", "run-2", "run-3", "run-4"}
    assert {"arm", "name", "feature_set", "fold", "pr_auc", "pr_auc_ci", "capture_top10", "features"} <= set(written["rows"][0])
    assert written["decision"]["store_sha256"] == sha and written["decision"]["best"] == out["decision"]["best"]


def test_the_registered_decision_writes_the_store_hash_into_registry_json(monkeypatch: pytest.MonkeyPatch,
                                                                        prospect_sandbox) -> None:
    tags: list[tuple] = []

    class Client:
        def get_registered_model(self, name):
            raise LookupError(name)

        def create_registered_model(self, name, description=""):
            return None

        def create_model_version(self, name, source, run_id, description):
            return SimpleNamespace(version="3")

        def set_model_version_tag(self, name, version, key, value):
            tags.append((key, value))

    monkeypatch.setattr(TR, "_mlflow", lambda: SimpleNamespace(MlflowClient=Client))
    d = TR.record_decision("run-9", "histgb", False, "not validated", store_sha256="f" * 64)
    assert d["store_sha256"] == "f" * 64 and d["version"] == 3 and ("store_sha256", "f" * 64) in tags
    assert json.loads((prospect_sandbox.out / "registry.json").read_text())["store_sha256"] == "f" * 64
    assert TR.served_model() is None, "the registry says candidate, so nothing is served"

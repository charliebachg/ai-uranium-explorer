"""Out-of-fold scores: the shape, the folds, the criteria pass-through, and the store table round trip."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uranium_explorer import store as ST
from uranium_explorer.bench import oof as O
from uranium_explorer.prospect import modelsearch as MS
from bench_store import bench_frame, fake_fit, make_bench_store


def test_shape_models_folds_and_that_criteria_is_the_stored_column() -> None:
    df = bench_frame()
    out = O.oof_scores(seed=3, df=df, fit=fake_fit)
    assert list(out.columns) == list(O.COLUMNS)
    assert len(out) == 3 * len(df) and set(out["model"]) == set(O.MODELS)
    assert (out["fold_kind"] == "spatial").all() and set(out["fold"]) <= set(range(5))
    fold = MS.spatial_fold(df, 30.0, n_splits=5, seed=3)
    learned = out[out["model"] == "learned"].set_index("cell_id")
    assert (learned.loc[df["cell_id"], "fold"].to_numpy() == fold.groups).all()
    crit = out[out["model"] == "criteria"].set_index("cell_id").loc[df["cell_id"], "score"].to_numpy()
    np.testing.assert_allclose(crit, df["criteria_score"].to_numpy())
    scored = out[out["model"] == "learned"]["score"]
    assert scored.notna().mean() > 0.9 and ((scored.dropna() >= 0) & (scored.dropna() <= 1)).all()


def test_a_cell_is_never_scored_by_a_model_that_saw_its_fold() -> None:
    df = bench_frame()
    seen: list[set[int]] = []
    fold = MS.spatial_fold(df, 30.0, n_splits=5, seed=0)
    groups = pd.Series(fold.groups, index=df["cell_id"])

    def spy(x_train, y_train, x_test):
        seen.append((len(x_train), len(x_test)))
        return fake_fit(x_train, y_train, x_test)

    O.oof_scores(seed=0, df=df, fit=spy)
    assert len(seen) == 2 * len(set(groups)) and all(tr + te <= len(df) for tr, te in seen)


def test_the_real_fit_runs_on_the_synthetic_frame() -> None:
    df = bench_frame()
    out = O.oof_scores(seed=0, df=df)
    p = out[out["model"] == "effort"].dropna()
    assert len(p) > 0 and ((p["score"] >= 0) & (p["score"] <= 1)).all()


@pytest.fixture
def store(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = prospect_sandbox.db
    make_bench_store(db, bench_frame(n_dep=6, n_occ=8, n_neg=10, n_probe=6))
    monkeypatch.setattr(ST, "db_path", lambda: db)
    return db


def test_from_the_store_criteria_equals_the_stored_score_and_the_table_round_trips(store: Path) -> None:
    con = ST.connect(store, read_only=True)
    try:
        out = O.oof_scores(seed=1, con=con, fit=fake_fit)
        stored = dict(con.execute("select cell_id, score from derived.cell_score where model = 'criteria'").fetchall())
    finally:
        con.close()
    crit = out[out["model"] == "criteria"]
    assert len(crit) == len(stored) and all(abs(stored[c] - s) < 1e-9 for c, s in zip(crit["cell_id"], crit["score"], strict=True))
    con = ST.connect(store)
    try:
        n = O.write_oof_scores(out, con, run_id="run-1")
        assert n == len(out)
        back = O.read_oof_scores(con)
        assert len(back) == len(out) and set(back["model"]) == set(O.MODELS)
        assert con.execute("select distinct tier, run_id from derived.cell_score_oof").fetchall() == [("derived", "run-1")]
        O.write_oof_scores(out, con, run_id="run-2")
        assert con.execute("select count(*), min(run_id) from derived.cell_score_oof").fetchone() == (len(out), "run-2"), \
            "a rewrite replaces the models it carries rather than stacking rows"
        one = O.read_oof_scores(con, [str(out["cell_id"].iat[0])])
        assert len(one) == 3
        with pytest.raises(Exception):
            con.execute("insert into derived.cell_score_oof values ('x', 'learned', 'spatial', 0, 0.5, 'r', 't', 'native')")
    finally:
        con.close()


def test_with_extended_the_extended_model_is_scored_beside_the_others_on_the_same_folds() -> None:
    from uranium_explorer.prospect.extended import EXTENDED_FEATURES

    df = bench_frame()
    rng = np.random.default_rng(0)
    for k in EXTENDED_FEATURES:
        df[k] = np.where(rng.random(len(df)) < 0.3, np.nan, rng.random(len(df)))
    out = O.oof_scores(seed=3, df=df, fit=fake_fit, extended=True)
    assert set(out["model"]) == {*O.MODELS, O.EXTENDED_MODEL} and len(out) == 4 * len(df)
    folds = out.pivot_table(index="cell_id", columns="model", values="fold")
    assert (folds["extended"] == folds["learned"]).all(), "the same folds, so the rows compare cell for cell"

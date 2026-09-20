"""The hindcast freezes what can be frozen and says what it cannot."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from uranium_explorer.prospect import hindcast as HC
from uranium_explorer.prospect import models as M
from uranium_explorer.prospect import tracking as TR
from uranium_explorer.store import snapshot as SN
from test_prospect_headline import frame


def dated_frame():
    df = frame(n=1500, seed=5)
    df["label_name"] = ""
    dep = np.flatnonzero((df["label_tier"] == "deposit").to_numpy())
    names = ["Old A", "Old B", "Old C", "New A", "New B"]
    for i, name in enumerate(names):
        df.loc[dep[i], "label_name"] = name
    df["in_basin"] = True
    discoveries = [
        {"name": "Old A", "label_name": "Old A", "year": 1975, "confidence": "high", "source": "s"},
        {"name": "Old B", "label_name": "Old B", "year": 1988, "confidence": "high", "source": "s"},
        {"name": "Old C", "label_name": "Old C", "year": 1995, "confidence": "medium", "source": "s"},
        {"name": "New A", "label_name": "New A", "year": 2013, "confidence": "high", "source": "s"},
        {"name": "New B", "label_name": "New B", "year": 2018, "confidence": "medium", "source": "s"},
        {"name": "Nowhere", "label_name": "Not a label", "year": 2016, "confidence": "high", "source": "s"},
    ]
    return df, discoveries


def test_labels_freeze_at_the_cutoff_and_undated_deposits_are_masked_from_training() -> None:
    df, disc = dated_frame()
    dated = HC.map_to_cells(disc, df[["cell_id", "label_name"]])
    y, train_ok = HC.frozen_labels(df, dated, cutoff=2000)
    assert y.sum() == 3, "three deposits were known by 2000"
    is_dep = (df["label_tier"] == "deposit").to_numpy()
    assert not train_ok[is_dep & (y == 0)].any(), "later and undated deposits are neither positive nor negative"
    assert train_ok[~is_dep].all()


def test_drilling_is_frozen_at_the_cutoff() -> None:
    df, _ = dated_frame()
    df.loc[0, "holes_first_year"], df.loc[0, "holes_n"] = 2015.0, 40.0
    df.loc[1, "holes_first_year"], df.loc[1, "holes_n"] = 1980.0, 40.0
    f = HC.frozen_effort(df, 2000)
    assert f.loc[0, "holes_n"] == 0 and f.loc[0, "holes_first_year"] == 2000.0
    assert f.loc[1, "holes_n"] == 40 and f.loc[1, "holes_first_year"] == 1980.0


def test_area_share_is_the_share_of_basin_scoring_at_least_the_cell() -> None:
    s = np.array([0.1, 0.5, 0.9, 0.3, np.nan])
    basin = np.array([True, True, True, True, True])
    assert HC.share_at_least(s, 0.9, basin) == 0.25 and HC.share_at_least(s, 0.1, basin) == 1.0


def test_run_ranks_later_discoveries_and_names_the_unmapped(monkeypatch) -> None:
    df, disc = dated_frame()
    fake = lambda xt, yt, xs: 1.0 - (xs[:, 0] - xs[:, 0].min()) / (np.ptp(xs[:, 0]) or 1.0)
    out = HC.run(cutoffs=(2000,), min_confidence="medium", log=lambda *a: None, write=False, track=False, fit=fake,
                 df=df, discoveries=disc)
    assert out["unmapped"] == ["Nowhere"]
    names = {r["discovery"] for r in out["rows"]}
    assert names == {"New A", "New B"} and {r["model"] for r in out["rows"]} == {"learned", "effort"}
    for r in out["rows"]:
        assert 0 < r["area_share"] <= 1
    assert out["summary"]["learned.n"] == 2 and "geological layers are compilations" in out["leakage"][0]
    high = HC.run(cutoffs=(2000,), min_confidence="high", log=lambda *a: None, write=False, track=False, fit=fake, df=df, discoveries=disc)
    assert {r["discovery"] for r in high["rows"]} == {"New A"}


# ---------------------------------------------------------------- the run names its store


def test_run_refuses_a_snapshot_nobody_took_before_fitting_anything() -> None:
    df, disc = dated_frame()

    def no_fit(*a):
        raise AssertionError("must not fit")

    with pytest.raises(FileNotFoundError):
        HC.run(cutoffs=(2000,), log=lambda *a: None, write=False, track=False, fit=no_fit, df=df, discoveries=disc,
               snapshot="nope")


def test_run_names_its_snapshot_in_the_result_the_mlflow_run_and_hindcast_json(monkeypatch: pytest.MonkeyPatch,
                                                                              prospect_sandbox) -> None:
    sha = SN.take(log=lambda *a: None, path=prospect_sandbox.make_store())["store_sha256"]
    df, disc = dated_frame()
    fake = lambda xt, yt, xs: 1.0 - (xs[:, 0] - xs[:, 0].min()) / (np.ptp(xs[:, 0]) or 1.0)
    logged: list[tuple] = []
    monkeypatch.setattr(TR, "log_run", lambda name, params, metrics, tags=None, artifacts=None:
                        logged.append((name, params, tags)) or "run-h")
    monkeypatch.setattr(HC, "_write_metrics", lambda rows, summary, run_id: None)
    out = HC.run(cutoffs=(2000,), log=lambda *a: None, write=True, track=True, fit=fake, df=df, discoveries=disc,
                 snapshot=sha[:12])
    assert out["store_sha256"] == sha and out["snapshot"] == sha[:12] and out["mlflow_run_id"] == "run-h"
    [(name, params, tags)] = logged
    assert name == "hindcast" and params["store_sha256"] == sha and tags["snapshot"] == sha[:12] and tags["kind"] == "hindcast"
    written = json.loads((prospect_sandbox.out / "hindcast.json").read_text())
    assert written["run_id"] == "run-h" and written["store_sha256"] == sha and written["snapshot"] == sha[:12]
    assert {r["discovery"] for r in written["rows"]} == {"New A", "New B"} and written["summary"] == out["summary"]

"""The sampler: the right cells in the right numbers, the same every time, honest about what it could not find."""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from legacy_reader.bench import sample as SM
from legacy_reader.bench import spec as S
from legacy_reader.prospect import headline as H
from bench_store import bench_frame


def spec(**strata) -> S.BenchSpec:
    base = {"deposit": 10, "occurrence": 12, "negative": 24, "probe": 6}
    return S.BenchSpec(version="t", seed=7, strata=S.Strata(**{**base, **strata}), fold_km=30, n_folds=5,
                       held_out_share=0.25, card=S.CardSpec(20, 200, ("em_conductors",)))


def test_counts_strata_rules_and_columns() -> None:
    df = bench_frame()
    cells = SM.sample_cells(df, spec())
    assert list(cells.columns) == list(SM.COLUMNS)
    assert cells["stratum"].value_counts().to_dict() == {"negative": 24, "occurrence": 12, "deposit": 10, "probe": 6}
    assert cells["cell_id"].is_unique
    by = df.set_index("cell_id")
    for _, r in cells.iterrows():
        tier, holes = by.at[r.cell_id, "label_tier"], by.at[r.cell_id, "holes_n"]
        if r.stratum == "deposit":
            assert tier == "deposit"
        elif r.stratum == "occurrence":
            assert tier == "occurrence" and holes > 0
        elif r.stratum == "negative":
            assert tier == "unlabelled" and holes >= 5
        else:
            assert tier == "unlabelled" and holes == 0
    assert set(cells["fold"]) <= set(range(5)) and cells["fold"].dtype.kind == "i"
    assert cells.attrs["shortfall"] == {}


def test_deposits_are_thinned_to_one_per_block() -> None:
    df = bench_frame()
    cells = SM.sample_cells(df, spec(deposit=100))
    dep = df.set_index("cell_id").loc[cells.loc[cells["stratum"] == "deposit", "cell_id"]]
    blocks = list(zip(np.floor(dep["cx"] / 10_000), np.floor(dep["cy"] / 10_000), strict=True))
    assert len(blocks) == len(set(blocks))
    assert len(dep) < (df["label_tier"] == "deposit").sum(), "the camps were thinned"


def test_negatives_follow_the_positives_effort_profile_better_than_a_uniform_draw() -> None:
    df = bench_frame(n_neg=400)
    sp = spec(negative=60)
    cells = SM.sample_cells(df, sp)
    by = df.set_index("cell_id")
    pos = by.loc[cells.loc[cells["stratum"].isin(["deposit", "occurrence"]), "cell_id"]]
    neg = by.loc[cells.loc[cells["stratum"] == "negative", "cell_id"]]
    pool = df[(df["label_tier"] == "unlabelled") & (df["holes_n"] >= 5)]
    e = pd.Series(H.effort_index(df), index=df["cell_id"])
    gap_matched = abs(e[pos.index].mean() - e[neg.index].mean())
    gap_uniform = abs(e[pos.index].mean() - e[pool["cell_id"]].mean())
    assert gap_matched <= gap_uniform + 0.02, "matched negatives sit closer to the positives' effort than the pool does"


def test_the_same_seed_gives_the_same_cells_and_a_different_seed_does_not() -> None:
    df = bench_frame()
    a, b = SM.sample_cells(df, spec()), SM.sample_cells(df, spec())
    pd.testing.assert_frame_equal(a, b)
    other = dataclasses.replace(spec(), seed=8)
    c = SM.sample_cells(df, other)
    assert set(a["cell_id"]) != set(c["cell_id"])
    ids_a, ids_b = SM.assign_bench_ids(a, 7), SM.assign_bench_ids(b, 7)
    pd.testing.assert_frame_equal(ids_a, ids_b)
    assert list(ids_a["bench_id"]) == [f"b-{i:04d}" for i in range(1, len(a) + 1)]
    assert list(ids_a["stratum"]) != sorted(ids_a["stratum"]), "bench ids do not sort by stratum"


def test_a_short_stratum_takes_what_exists_and_records_the_shortfall() -> None:
    df = bench_frame(n_probe=3, n_dep=4)
    cells = SM.sample_cells(df, spec(probe=10, deposit=10))
    assert (cells["stratum"] == "probe").sum() == 3
    assert (cells["stratum"] == "deposit").sum() <= 4
    short = SM.shortfalls(cells, spec(probe=10, deposit=10))
    assert short["probe"] == 7 and short["deposit"] >= 6 and "negative" not in short
    assert cells.attrs["shortfall"] == short


def test_the_held_out_share_is_drawn_per_stratum() -> None:
    df = bench_frame()
    cells = SM.sample_cells(df, spec())
    for stratum, n in (("deposit", 10), ("occurrence", 12), ("negative", 24), ("probe", 6)):
        held = (cells.loc[cells["stratum"] == stratum, "split"] == "heldout").sum()
        assert held == round(0.25 * n), stratum
    assert set(cells["split"]) == {"open", "heldout"}


def test_folds_hold_whole_blocks_together() -> None:
    df = bench_frame()
    sp = spec()
    cells = SM.sample_cells(df, sp)
    by = df.set_index("cell_id")
    blocks = {}
    for _, r in cells.iterrows():
        b = (int(by.at[r.cell_id, "cx"] // 30_000), int(by.at[r.cell_id, "cy"] // 30_000))
        assert blocks.setdefault(b, r.fold) == r.fold

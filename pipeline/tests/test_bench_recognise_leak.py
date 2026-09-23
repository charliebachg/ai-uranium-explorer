"""The two checks benchmark v3 runs before any arm reads its passages: a recognition probe scored against the
names near each cell, and text-only models out of fold. No model is called."""

from __future__ import annotations

import numpy as np

from uranium_explorer.bench import leak as L
from uranium_explorer.bench import recognise as R


def test_a_guess_is_a_recognition_only_when_a_naming_word_matches_ground_near_the_cell() -> None:
    truth = {"mcclean", "sue", "denison"}
    assert R.matches("the Sue C pit at McClean Lake", truth) == ["mcclean", "sue"]
    assert R.matches("MCCLEAN LAKE", truth) == ["mcclean"]
    assert R.matches("somewhere in the Athabasca Basin", truth) == []
    assert R.matches("unknown", truth) == []
    assert "athabasca" in R.REGIONAL, "naming the basin recognises nothing"


def test_the_probe_asks_the_arms_model_about_exactly_the_passages(tmp_path) -> None:
    req = R.request("b-0001", "# Report text about this ground\n\nbleached sandstone", tmp_path)
    assert (req.model, req.effort) == (R.MODEL, R.EFFORT) and req.schema is R.SCHEMA
    assert req.stage_files[0][0].read_text().endswith("bleached sandstone")
    assert "b-0001" in req.user_prompt and "0001_" not in req.user_prompt


def test_out_of_fold_never_scores_a_cell_with_a_model_that_saw_its_fold() -> None:
    rng = np.random.default_rng(0)
    y = np.array([1, 0] * 20)
    x = np.c_[y + rng.normal(0, 0.3, 40), rng.normal(0, 1, 40)]
    folds = np.repeat([0, 1, 2, 3], 10)
    p = L.out_of_fold(x, y, folds)
    assert np.isfinite(p).all() and ((p > 0.5) == (y == 1)).mean() > 0.8
    one_fold = L.out_of_fold(x, y, np.zeros(40, dtype=int))
    assert np.isnan(one_fold).all(), "with a single fold there is nothing to fit on"


def test_the_placebo_view_serves_another_cells_passages(monkeypatch, tmp_path) -> None:
    import json

    from uranium_explorer.analyst import frozen as F
    from fake_bench import make_bench

    fake = make_bench(tmp_path)
    for b in ("b02", "b03"):
        (fake.dir / "passages" / f"{b}.json").write_text(json.dumps({"bench_id": b, "passages": [{"text": f"about {b}"}]}))
    monkeypatch.setattr(F, "bench_root", lambda: tmp_path / "bench")
    bench = F.load_bench(fake.version)
    swap = bench.swap_map()
    assert set(swap) == {"b01", "b02", "b03"} and all(a != b for a, b in swap.items())
    for b in ("b02", "b03"):
        got = bench.passages(b, "swapped")
        assert got and got == bench.passages(swap[b]) and got != bench.passages(b)

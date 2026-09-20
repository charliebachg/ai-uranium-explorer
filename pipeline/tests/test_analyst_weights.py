"""Decider (a): the criteria weights and their mean rule on hand-built nodes, the out-of-fold fit and its
leakage rule on a synthetic frame, the file round trip, and the fitting frame built from stored chains."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uranium_explorer.analyst import weights as W
from uranium_explorer.analyst.wire import Node
from uranium_explorer.prospect import criteria as C

# the counted criteria of the real table, with their weights; folklore is named here only to prove it is absent
CS = C.load()
COUNTED = {c.key: c.weight for c in CS.counted}
TOTAL = sum(COUNTED.values())


def node(criterion: str, status: str, strength: int, *, kind: str = "criterion", published: bool = True,
         round: int = 0, attempt: int = 1, n: int = 1) -> Node:
    return Node(node_id=f"n{n:02d}", segment_id=f"s{n:02d}", kind=kind, criterion=criterion, status=status,
                strength=strength, text=f"{criterion} {status}", published=published, round=round, attempt=attempt,
                depends_on=["n01", "n02"] if kind == "crosscheck" else [])


# ---------------------------------------------------------------- the criteria weights

def test_criteria_weights_exclude_folklore_and_carry_the_schema_version() -> None:
    w = W.criteria_weights(CS)
    assert w.version == f"criteria/{CS.schema_version}" and CS.schema_version == "1.0.0"
    assert w.by_criterion == COUNTED and all(v > 0 for v in w.by_criterion.values())
    assert {c.key for c in CS.folklore}.isdisjoint(w.by_criterion), "folklore never carries a weight"
    assert W.KNOWN_SUFFIX not in "".join(w.by_criterion), "criteria weights weigh strengths, not known flags"
    assert w.rule == W.RULE_MEAN and w.min_known_weight == CS.min_known_weight == 0.5
    assert w.fold is None and w.fitted_on is None and w.intercept == 0.0


def test_node_features_sign_strength_and_flag_known() -> None:
    f = W.node_features([node("a", "met", 4, n=1), node("b", "not_met", 2, n=2), node("c", "unknown", 0, n=3),
                         node("conductor_fault", "met", 5, kind="crosscheck", n=4),
                         node(None, "met", 0, kind="retrieval", n=5),
                         node("d", "met", 5, published=False, n=6)])
    assert f == {"a": 0.8, "a:known": 1.0, "b": -0.4, "b:known": 1.0, "c": 0.0, "c:known": 0.0,
                 "conductor_fault": 1.0, "conductor_fault:known": 1.0}
    # the latest published node per criterion stands, by (round, attempt), whatever order the list is in
    f = W.node_features([node("a", "met", 5, round=1, n=2), node("a", "not_met", 1, round=0, n=1)])
    assert f == {"a": 1.0, "a:known": 1.0}


def test_score_with_criteria_weights_follows_the_criteria_rule() -> None:
    w = W.criteria_weights(CS)
    all_met = [node(k, "met", 5, n=i + 1) for i, k in enumerate(COUNTED)]
    assert W.score(all_met, w) == pytest.approx(1.0)
    all_not = [node(k, "not_met", 5, n=i + 1) for i, k in enumerate(COUNTED)]
    assert W.score(all_not, w) == 0.0
    # a mix, by hand: conductor met at 3, graphitic host met at 5, fault and depth not met, the rest unknown
    mix = [node("conductor_proximity", "met", 3, n=1), node("graphitic_host", "met", 5, n=2),
           node("fault_proximity", "not_met", 4, n=3), node("unconformity_depth", "not_met", 1, n=4)]
    known = sum(COUNTED[k] for k in ("conductor_proximity", "graphitic_host", "fault_proximity", "unconformity_depth"))
    assert known == 9 and TOTAL == 15, "the hand case clears the 0.5 known share of the real table"
    met = COUNTED["conductor_proximity"] * 3 / 5 + COUNTED["graphitic_host"] * 5 / 5
    assert W.score(mix, w) == pytest.approx(met / known) == pytest.approx(3.8 / 9)
    # a cross-check pair has no table weight: it neither scores nor counts as known
    assert W.score(mix + [node("conductor_fault", "met", 5, kind="crosscheck", n=5)], w) == pytest.approx(met / known)


def test_score_with_criteria_weights_abstains_below_the_known_share() -> None:
    w = W.criteria_weights(CS)
    # conductor (3) and graphitic host (2) known: 5 of 15 is under the 0.5 the table asks for
    thin = [node("conductor_proximity", "met", 5, n=1), node("graphitic_host", "met", 5, n=2),
            node("fault_proximity", "unknown", 0, n=3)]
    assert (COUNTED["conductor_proximity"] + COUNTED["graphitic_host"]) / TOTAL < 0.5
    assert W.score(thin, w) is None
    assert W.score([], w) is None, "nothing known is never a score"
    assert W.score([node(k, "unknown", 0, n=i + 1) for i, k in enumerate(COUNTED)], w) is None
    # the same nodes clear a lower threshold, so the threshold is the weights' and nothing else
    assert W.score(thin, W.Weights(version="t", by_criterion=COUNTED, min_known_weight=0.3)) == pytest.approx(1.0)


def test_score_with_logistic_weights_is_a_sigmoid_over_the_features() -> None:
    w = W.Weights(version="fitted/spatial/0/abc", rule=W.RULE_LOGISTIC, intercept=-1.0, fold=0,
                  by_criterion={"a": 2.0, "a:known": 0.5, "b": 1.0, "b:known": -0.25})
    nodes = [node("a", "met", 5, n=1), node("b", "not_met", 2, n=2)]
    z = -1.0 + 2.0 * 1.0 + 0.5 * 1.0 + 1.0 * -0.4 + -0.25 * 1.0
    assert W.score(nodes, w) == pytest.approx(1 / (1 + np.exp(-z)))
    # an absent criterion is unknown: 0 for both terms, never an abstention under fitted weights
    assert W.score([], w) == pytest.approx(1 / (1 + np.exp(1.0)))
    with pytest.raises(ValueError):
        W.Weights(version="t", by_criterion={}, rule="vote")


# ---------------------------------------------------------------- the out-of-fold fit

CRITERIA = ("sep", "noise_a", "noise_b", "lake_water")


def synthetic_frame(n_cells: int = 40, n_folds: int = 4, seed: int = 7) -> pd.DataFrame:
    """One row per (cell, criterion). `sep` is met for positives and not met for negatives; the others are
    unrelated to the label; `lake_water` is unknown for a third of the cells, independently of the label."""
    rng = np.random.default_rng(seed)
    records = []
    for i in range(n_cells):
        label, fold = i % 2, (i // 2) % n_folds  # every fold holds both classes in equal number
        for crit in CRITERIA:
            if crit == "sep":
                status, strength = ("met", 5) if label else ("not_met", 4)
            elif crit == "lake_water" and rng.random() < 1 / 3:
                status, strength = "unknown", 0
            else:
                status, strength = rng.choice(["met", "not_met"]), int(rng.integers(1, 6))
            records.append({"cell_id": f"c{i:02d}", "fold": fold, "criterion": crit, "status": status,
                            "strength": strength, "label": label})
    return pd.DataFrame.from_records(records)


def test_fit_weights_never_fits_a_fold_on_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = synthetic_frame()
    seen: list[tuple[int, set[int]]] = []
    real = W.training_rows

    def spy(frame: pd.DataFrame, fold: int) -> pd.DataFrame:
        out = real(frame, fold)
        seen.append((fold, set(out["fold"])))
        return out

    monkeypatch.setattr(W, "training_rows", spy)
    by_fold = W.fit_weights(rows)
    assert sorted(by_fold) == [0, 1, 2, 3] and [k for k, _ in seen] == [0, 1, 2, 3]
    for k, folds_seen in seen:
        assert k not in folds_seen and folds_seen == {0, 1, 2, 3} - {k}
    for k, w in by_fold.items():
        assert w.fold == k and w.rule == W.RULE_LOGISTIC
        assert k not in w.fitted_on["folds"] and w.fitted_on["folds"] == [f for f in range(4) if f != k]
        assert w.fitted_on["n_cells"] == 30 and w.fitted_on["positives"] == 15
        assert w.fitted_on["fold_kind"] == "spatial" and w.fitted_on["criteria"] == sorted(CRITERIA)
        assert w.version == f"fitted/spatial/{k}/{W.rows_sha(real(rows, k))}"
    # the weights for fold k are not the weights a fit that saw fold k would give
    keys = sorted(CRITERIA)
    full = W.design(rows, keys)
    leaked = W.estimator(0).fit(full.x, full.y)
    for k, w in by_fold.items():
        coef = np.array([w.by_criterion[c] for c in full.columns])
        assert not np.allclose(coef, leaked.coef_[0], atol=1e-6), f"fold {k} weights equal the all-data fit"


def test_fit_weights_finds_the_separating_criterion_and_scores_with_it() -> None:
    rows = synthetic_frame()
    by_fold = W.fit_weights(rows)
    for w in by_fold.values():
        strengths = {c: v for c, v in w.by_criterion.items() if not c.endswith(W.KNOWN_SUFFIX)}
        assert max(strengths, key=strengths.get) == "sep" and strengths["sep"] > 0
        assert set(w.by_criterion) == {*CRITERIA, *(c + W.KNOWN_SUFFIX for c in CRITERIA)}
    # a chain that meets `sep` scores higher than one that does not, under every fold's weights
    for w in by_fold.values():
        up = W.score([node("sep", "met", 5, n=1)], w)
        down = W.score([node("sep", "not_met", 4, n=1)], w)
        assert up is not None and down is not None and 0 < down < up < 1


def test_fit_weights_is_deterministic_and_versioned_by_its_training_rows() -> None:
    rows = synthetic_frame()
    a, b = W.fit_weights(rows), W.fit_weights(rows.sample(frac=1, random_state=3))
    assert a == b, "the same rows in another order give the same weights and the same versions"
    changed = rows.copy()
    i = changed.index[(changed["fold"] == 0) & (changed["criterion"] == "noise_a")][0]
    changed.loc[i, "strength"] = 5 if changed.loc[i, "strength"] != 5 else 1
    c = W.fit_weights(changed)
    assert c[0].version == a[0].version, "fold 0's training rows did not change"
    for k in (1, 2, 3):
        assert c[k].version != a[k].version, f"fold {k} trained on the changed row and must say so"


def test_fit_weights_refuses_thin_or_single_class_folds_and_torn_frames() -> None:
    with pytest.raises(ValueError, match="fewer than 10"):
        W.fit_weights(synthetic_frame(n_cells=24, n_folds=4))
    single = synthetic_frame()
    single.loc[single["fold"] == 2, "label"] = 1
    with pytest.raises(ValueError, match="single class"):
        W.fit_weights(single)
    with pytest.raises(ValueError, match="at least two folds"):
        W.fit_weights(synthetic_frame(n_folds=1))
    rows = synthetic_frame()
    with pytest.raises(ValueError, match="duplicate"):
        W.fit_weights(pd.concat([rows, rows.iloc[:1]]))
    torn = rows.copy()
    torn.loc[torn.index[0], "fold"] = 3
    with pytest.raises(ValueError, match="more than one fold"):
        W.fit_weights(torn)
    bad = rows.copy()
    bad.loc[bad.index[0], "label"] = 2
    with pytest.raises(ValueError, match="label"):
        W.fit_weights(bad)
    with pytest.raises(ValueError, match="missing columns"):
        W.fit_weights(rows.drop(columns=["fold"]))


# ---------------------------------------------------------------- on disk

def test_save_and_load_round_trip(tmp_path: Path) -> None:
    by_fold = W.fit_weights(synthetic_frame())
    path = W.save(by_fold, tmp_path)
    assert path.parent == tmp_path and path.name.startswith("spatial-") and path.suffix == ".json"
    assert W.load(path) == by_fold
    assert W.save(by_fold, tmp_path) == path, "the same weights land in the same file"
    other = W.save(W.fit_weights(synthetic_frame(seed=8)), tmp_path)
    assert other != path, "a refit is a new file, never an overwrite"
    with pytest.raises(ValueError, match="carries no fold"):
        W.save({0: W.criteria_weights(CS)}, tmp_path)
    with pytest.raises(ValueError, match="no weights"):
        W.save({}, tmp_path)


# ---------------------------------------------------------------- from stored chains

def chain(cell: str, nodes: list[Node], chain_id: str = "ch") -> dict:
    return {"chain_id": chain_id, "cell": cell, "nodes": [n.as_dict() for n in nodes]}


KEY = {
    "b01": {"label": "deposit", "stratum": "deposit", "fold": 2, "split": "open"},
    "b02": {"label": "unlabelled", "stratum": "negative", "fold": 0, "split": "open"},
    "b03": {"label": "unlabelled", "stratum": "probe", "fold": 1, "split": "open"},
    "b04": {"label": "occurrence", "stratum": "occurrence", "fold": 3, "split": "heldout"},
}


def test_rows_from_chains_builds_the_frame_from_the_current_nodes_and_the_key() -> None:
    first = chain("b01", [
        node("conductor_proximity", "not_met", 2, n=1),                    # superseded by the round-1 repair
        node("conductor_proximity", "met", 4, round=1, n=2),
        node("graphitic_host", "unknown", 0, n=3),
        node("conductor_fault", "met", 3, kind="crosscheck", n=4),
        node(None, "met", 0, kind="retrieval", n=5),                       # no criterion: no row
        node("fault_proximity", "met", 5, published=False, n=6),           # rejected: no row
    ])
    second = chain("b02", [node("conductor_proximity", "not_met", 5, n=1)], chain_id="ch2")
    probe = chain("b03", [node("conductor_proximity", "met", 5, n=1)], chain_id="ch3")
    rows = W.rows_from_chains([first, second, probe], KEY)
    assert list(rows.columns) == list(W.ROW_COLUMNS)
    expected = pd.DataFrame.from_records([
        {"cell_id": "b01", "fold": 2, "criterion": "conductor_proximity", "status": "met", "strength": 4, "label": 1},
        {"cell_id": "b01", "fold": 2, "criterion": "graphitic_host", "status": "unknown", "strength": 0, "label": 1},
        {"cell_id": "b01", "fold": 2, "criterion": "conductor_fault", "status": "met", "strength": 3, "label": 1},
        {"cell_id": "b02", "fold": 0, "criterion": "conductor_proximity", "status": "not_met", "strength": 5, "label": 0},
    ], columns=list(W.ROW_COLUMNS))
    pd.testing.assert_frame_equal(rows, expected)
    # a store row names its bench id beside the anonymised cell; a re-run chain for the same cell replaces the first
    rerun = {**chain("anon", [node("graphitic_host", "met", 5, n=1)], chain_id="ch1b"), "bench_id": "b01"}
    again = W.rows_from_chains([first, rerun], KEY)
    assert list(again["criterion"]) == ["graphitic_host"] and list(again["cell_id"]) == ["b01"]
    with pytest.raises(PermissionError, match="held-out"):
        W.rows_from_chains([chain("b04", [node("graphitic_host", "met", 5, n=1)])], KEY)
    with pytest.raises(KeyError, match="not in the benchmark key"):
        W.rows_from_chains([chain("b99", [])], KEY)
    assert W.rows_from_chains([], KEY).empty

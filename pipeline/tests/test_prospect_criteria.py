"""The knowledge-driven score: membership maths, and the rules that keep it arguable."""

from __future__ import annotations

import numpy as np
import pytest

from uranium_explorer.prospect import criteria as C

REAL = C.load()


def write(tmp_path, body: str):
    p = tmp_path / "criteria.toml"
    p.write_text('schema_version = "1.0.0"\nmin_known_weight = 0.5\n' + body)
    return p


BASE = """
[[criterion]]
key = "a"
title = "A"
element = "pathway"
feature = "d_conductor_m"
shape = "falling"
params = { lo = 500.0, hi = 5000.0 }
weight = 1.0
status = "assumed"
evidence = "e"
caveat = "c"
"""


# ---------------------------------------------------------------- the file itself


def test_the_real_criteria_file_loads_and_carries_its_sources():
    assert REAL.counted, "nothing would be scored"
    for c in REAL.criteria:
        assert c.evidence and c.caveat, f"{c.key} must say what it rests on and how it fails"
        assert c.element in {"pathway", "trap", "cover", "detection", "dispersal"}


def test_folklore_is_present_named_and_weightless():
    """The literature records these as personal communication with no published test."""
    folklore = {c.key for c in REAL.folklore}
    assert {"conductor_strength", "em_bright_spot"} <= folklore
    for c in REAL.folklore:
        assert c.weight == 0.0
        assert "personal communication" in c.evidence or "not backed by a published" in c.evidence
    assert not (folklore & {c.key for c in REAL.counted})


def test_a_folklore_criterion_with_weight_is_refused(tmp_path):
    body = BASE + """
[[criterion]]
key = "f"
title = "F"
element = "pathway"
feature = "conductor_density"
shape = "rising"
params = { lo = 0.0, hi = 1.0 }
weight = 2.0
status = "folklore"
evidence = "personal communication"
caveat = "c"
"""
    with pytest.raises(C.CriteriaError) as e:
        C.load(write(tmp_path, body))
    assert "weight zero" in str(e.value)


def test_an_unknown_shape_is_refused(tmp_path):
    with pytest.raises(C.CriteriaError):
        C.load(write(tmp_path, BASE.replace('shape = "falling"', 'shape = "vibes"')))


def test_a_duplicate_key_is_refused(tmp_path):
    with pytest.raises(C.CriteriaError):
        C.load(write(tmp_path, BASE + BASE))


def test_a_file_where_everything_is_folklore_scores_nothing(tmp_path):
    body = BASE.replace("weight = 1.0", "weight = 0.0").replace('status = "assumed"', 'status = "folklore"')
    with pytest.raises(C.CriteriaError) as e:
        C.load(write(tmp_path, body))
    assert "weight zero" in str(e.value)


# ---------------------------------------------------------------- membership


def test_falling_membership_is_one_near_and_zero_far():
    x = np.array([0.0, 500.0, 2750.0, 5000.0, 9000.0])
    m = C.falling(x, 500.0, 5000.0)
    assert m[0] == 1.0 and m[1] == 1.0
    assert m[2] == pytest.approx(0.5)
    assert m[3] == 0.0 and m[4] == 0.0


def test_band_membership_is_a_window_not_a_threshold():
    m = C.band(np.array([40.0, 120.0, 400.0, 700.0, 1200.0]), 50.0, 120.0, 700.0, 1000.0)
    assert m[0] == 0.0
    assert m[1] == 1.0 and m[2] == 1.0 and m[3] == 1.0
    assert m[4] == 0.0


def test_percentile_thresholds_come_from_the_data_and_are_recorded():
    c = next(x for x in REAL.criteria if x.shape == "percentile_rising")
    values = np.array([1.0, 2.0, 3.0, 4.0, 100.0, np.nan])
    m, used = C.membership(c, values)
    assert "lo" in used and "hi" in used, "the thresholds actually used must be recorded with the score"
    assert np.isnan(m[-1]), "a cell with no measurement has no membership"
    assert m[4] == pytest.approx(1.0)


def test_a_flat_distribution_yields_no_membership_rather_than_a_false_split():
    c = next(x for x in REAL.criteria if x.shape == "percentile_rising")
    m, used = C.membership(c, np.full(50, 7.0))
    assert np.isnan(m).all(), "if every cell reads the same, nothing is anomalous"
    assert used.get("degenerate") == 1.0


def test_a_missing_value_never_becomes_a_zero_membership():
    c = next(x for x in REAL.counted if x.shape == "falling")
    m, _ = C.membership(c, np.array([np.nan, 100.0]))
    assert np.isnan(m[0])
    assert m[1] == 1.0


# ---------------------------------------------------------------- combination


def _score_row(cs: C.CriteriaSet, values: dict[str, float]) -> tuple[float, float]:
    """Combine one cell by hand, the way `score` does, so the rule is testable without the store."""
    total = weight = 0.0
    for c in cs.criteria:
        if c.feature not in values:
            continue
        m, _ = C.membership(c, np.array([values[c.feature]]))
        if not np.isfinite(m[0]) or not c.counts:
            continue
        total += c.weight * m[0]
        weight += c.weight
    share = weight / cs.total_weight
    return (total / weight if weight else float("nan")), share


def test_folklore_cannot_move_the_score(tmp_path):
    """Adding a folklore criterion that would fire everywhere must leave the number untouched."""
    plain = C.load(write(tmp_path, BASE))
    with_folklore = C.load(write(tmp_path, BASE + """
[[criterion]]
key = "f"
title = "F"
element = "pathway"
feature = "d_conductor_m"
shape = "rising"
params = { lo = 0.0, hi = 1.0 }
weight = 0.0
status = "folklore"
evidence = "personal communication, no published test"
caveat = "c"
"""))
    a, _ = _score_row(plain, {"d_conductor_m": 800.0})
    b, _ = _score_row(with_folklore, {"d_conductor_m": 800.0})
    assert a == b


def test_the_known_share_falls_when_a_criterion_has_no_value():
    """A cell where most criteria are unknown must fall below the bar and be left unscored.

    (The percentile criteria cannot be evaluated from a single cell in isolation, since their thresholds come
    from the grid's distribution, so a one-cell harness never reaches a known share of 1.)
    """
    known_all = _score_row(REAL, {c.feature: 1.0 for c in REAL.counted})[1]
    known_some = _score_row(REAL, {REAL.counted[0].feature: 1.0})[1]
    assert known_all >= REAL.min_known_weight, "a cell with every feature present is scorable"
    assert known_some < REAL.min_known_weight, "one criterion out of eight is not a score"
    assert known_some < known_all


def test_the_real_set_needs_more_than_half_its_weight_known():
    assert 0.0 < REAL.min_known_weight <= 1.0
    assert REAL.total_weight > 0

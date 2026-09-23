"""The spec is the benchmark: it loads strictly, and the shipped v1 says what the plan says."""

from __future__ import annotations

import tomllib

import pytest

from uranium_explorer.bench import spec as S


def test_v1_loads_with_the_agreed_numbers() -> None:
    spec = S.load_spec("v1")
    assert spec.version == "v1" and spec.seed == 20260920
    assert (spec.strata.deposit, spec.strata.occurrence, spec.strata.negative, spec.strata.probe) == (40, 40, 80, 20)
    assert spec.strata.thin_block_m == 10_000 and spec.strata.min_holes == 5
    assert spec.fold_km == 30 and spec.n_folds == 5 and spec.held_out_share == 0.20
    assert spec.card.window_km == 20 and spec.card.size_px == 1000 and spec.card.drillholes is False
    assert spec.card.drillholes_variant is True   # a second card set with holes, so an arm can switch effort on
    assert set(spec.card.layers) == set(S.CARD_LAYERS)
    # the packs carry every part; an arm removes what it does not use, and never adds
    assert spec.pack.switches() == {"label_context": True, "oof_scores": True, "effort_features": True}
    assert spec.blind.radius_km == 10 and spec.retrieval.k == 6 and spec.retrieval.radius_km == 40
    assert spec.as_dict()["strata"]["negative"] == 80


def raw() -> dict:
    return tomllib.loads(S.spec_path("v1").read_text())


def test_an_unknown_key_anywhere_is_an_error() -> None:
    bad = raw()
    bad["held_out_shar"] = 0.2
    with pytest.raises(S.SpecError, match="unknown top-level"):
        S.parse_spec(bad, "v1")
    bad = raw()
    bad["card"]["basemap"] = True
    with pytest.raises(S.SpecError, match=r"\[card\]: unknown"):
        S.parse_spec(bad, "v1")


def test_a_label_layer_is_refused_on_a_card() -> None:
    bad = raw()
    bad["card"]["layers"] = ["em_conductors", "uranium_deposit_footprints"]
    with pytest.raises(S.SpecError, match="never drawn"):
        S.parse_spec(bad, "v1")


def test_the_version_must_match_the_file_and_shares_must_be_shares() -> None:
    with pytest.raises(S.SpecError, match="version"):
        S.parse_spec(raw(), "v2")
    bad = raw()
    bad["held_out_share"] = 1.5
    with pytest.raises(S.SpecError, match="held_out_share"):
        S.parse_spec(bad, "v1")


def test_a_missing_spec_says_so() -> None:
    with pytest.raises(S.SpecError, match="no spec"):
        S.load_spec("v999")


def test_the_later_pack_switches_leave_an_older_spec_as_it_was_and_are_carried_when_on() -> None:
    v2 = S.load_spec("v2")
    assert v2.pack.switches() == {"label_context": True, "oof_scores": True, "effort_features": True}
    assert v2.as_dict()["pack"] == v2.pack.switches() and not v2.pack.later()
    v3 = S.load_spec("v3")
    assert v3.pack.later() and {"evidence", "region", "extended_features"} <= set(v3.pack.switches())
    # v3 is v2's benchmark with richer packs: the same seed, strata, folds and cards
    assert (v3.seed, v3.strata, v3.fold_km, v3.n_folds, v3.card) == (v2.seed, v2.strata, v2.fold_km, v2.n_folds, v2.card)

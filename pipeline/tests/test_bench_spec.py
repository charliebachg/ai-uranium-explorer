"""The spec is the benchmark: it loads strictly, and the shipped v1 says what the plan says."""

from __future__ import annotations

import tomllib

import pytest

from legacy_reader.bench import spec as S


def test_v1_loads_with_the_agreed_numbers() -> None:
    spec = S.load_spec("v1")
    assert spec.version == "v1" and spec.seed == 20260920
    assert (spec.strata.deposit, spec.strata.occurrence, spec.strata.negative, spec.strata.probe) == (40, 40, 80, 20)
    assert spec.strata.thin_block_m == 10_000 and spec.strata.min_holes == 5
    assert spec.fold_km == 30 and spec.n_folds == 5 and spec.held_out_share == 0.20
    assert spec.card.window_km == 20 and spec.card.size_px == 1000 and spec.card.drillholes is False
    assert set(spec.card.layers) == set(S.CARD_LAYERS)
    assert spec.pack.switches() == {"label_context": False, "oof_scores": False, "effort_features": False}
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

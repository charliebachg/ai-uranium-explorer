"""The arm files: every one loads, the headline has every switch off, and each ablation changes one thing."""

from __future__ import annotations

import tomllib
from dataclasses import asdict

import pytest

from legacy_reader.analyst import arms as A

ALL = ["v0", "v0-text", "v0-card", "v0-sonnet", "v0-holes", "v0-labels", "v0-scores", "v0-retrieval"]


def test_every_arm_file_loads_and_says_which_question_it_answers() -> None:
    assert A.list_arms() == sorted(ALL)
    notes = set()
    for name in ALL:
        arm = A.load_arm(name)
        assert arm.name == name and arm.agent == "v0" and arm.notes.strip()
        notes.add(arm.notes)
    assert len(notes) == len(ALL), "two arms with the same notes line answer the same question twice"


def test_the_headline_is_opus_on_card_and_pack_with_every_switch_off() -> None:
    v0 = A.load_arm("v0")
    assert v0.model == "claude-opus-5"
    assert asdict(v0.inputs) == {"card": True, "pack": True, "passages": False}
    assert v0.switches.on() == ()


def _diff(a: A.ArmConfig, b: A.ArmConfig) -> dict[str, tuple]:
    da, db = asdict(a), asdict(b)
    out = {}
    for k in da:
        if k in ("name", "notes"):
            continue
        if isinstance(da[k], dict):
            for kk in da[k]:
                if da[k][kk] != db[k][kk]:
                    out[f"{k}.{kk}"] = (da[k][kk], db[k][kk])
        elif da[k] != db[k]:
            out[k] = (da[k], db[k])
    return out


@pytest.mark.parametrize("name, changed", [
    ("v0-text", {"inputs.card"}),
    ("v0-card", {"inputs.pack"}),
    ("v0-sonnet", {"model"}),
    ("v0-holes", {"switches.drillholes", "switches.effort_features"}),
    ("v0-labels", {"switches.label_context"}),
    ("v0-scores", {"switches.oof_scores"}),
    ("v0-retrieval", {"inputs.passages"}),
])
def test_each_ablation_differs_from_the_headline_in_exactly_the_stated_way(name: str, changed: set[str]) -> None:
    assert set(_diff(A.load_arm("v0"), A.load_arm(name))) == changed
    assert A.load_arm("v0-sonnet").model == "claude-sonnet-5"


def test_an_arm_that_forgets_a_switch_or_adds_a_key_is_refused(tmp_path) -> None:
    raw = tomllib.loads((A.arms_dir() / "v0.toml").read_text())
    del raw["arm"]["switches"]["oof_scores"]
    with pytest.raises(ValueError, match="oof_scores"):
        A.parse_arm(raw)
    raw = tomllib.loads((A.arms_dir() / "v0.toml").read_text())
    raw["arm"]["temperature"] = 0.2
    with pytest.raises(ValueError, match="temperature"):
        A.parse_arm(raw)
    raw = tomllib.loads((A.arms_dir() / "v0.toml").read_text())
    raw["arm"]["effort"] = "extreme"
    with pytest.raises(ValueError, match="effort"):
        A.parse_arm(raw)


def test_a_copied_file_cannot_masquerade_under_another_name(tmp_path) -> None:
    (tmp_path / "v9.toml").write_text((A.arms_dir() / "v0.toml").read_text())
    with pytest.raises(ValueError, match="name"):
        A.load_arm("v9", path=tmp_path / "v9.toml")
    with pytest.raises(FileNotFoundError):
        A.load_arm("nope", path=tmp_path / "nope.toml")

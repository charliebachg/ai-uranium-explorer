"""The arm files: every one loads, the headline has every switch off, and each ablation changes one thing."""

from __future__ import annotations

import tomllib
from dataclasses import asdict

import pytest

from legacy_reader.analyst import arms as A

V0 = ["v0", "v0-text", "v0-card", "v0-sonnet", "v0-holes", "v0-labels", "v0-scores", "v0-retrieval", "v0-features"]
V1 = ["v1", "v1-noverify", "v1-K3", "v1-strong", "v1-triage", "v1-modelplanner", "v1-cumulative"]
ALL = V0 + V1


def test_every_arm_file_loads_and_says_which_question_it_answers() -> None:
    assert A.list_arms() == sorted(ALL)
    notes = set()
    for name in ALL:
        arm = A.load_arm(name)
        assert arm.name == name and arm.agent == ("v1" if name in V1 else "v0") and arm.notes.strip()
        notes.add(arm.notes)
    assert len(notes) == len(ALL), "two arms with the same notes line answer the same question twice"


def test_the_headline_is_opus_on_card_and_pack_with_every_switch_off() -> None:
    v0 = A.load_arm("v0")
    assert v0.model == "claude-opus-5"
    assert asdict(v0.inputs) == {"card": True, "pack": True, "passages": False}
    assert v0.switches.on() == ("criteria",)   # the criteria table is the one part shown by default


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
    ("v0-features", {"switches.criteria"}),
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


# ---------------------------------------------------------------- the v1 arms: the staged loop's switches


def test_the_v1_headline_is_a_cheap_executor_under_an_opus_verifier_with_v0s_switches() -> None:
    v1, v0 = A.load_arm("v1"), A.load_arm("v0")
    assert v1.agent == "v1" and v1.loop is not None and v0.loop is None
    assert v1.loop.executor_model == "claude-sonnet-5" and v1.loop.verifier_model == "claude-opus-5"
    assert v1.loop.adjudicator_model == "claude-opus-5" and v1.loop.planner == "template"
    assert (v1.loop.verifier, v1.loop.rounds, v1.loop.triage, v1.loop.executor_context, v1.loop.decider) == \
        ("skeptic", 1, False, "independent", "both")
    assert asdict(v1.inputs) == {"card": True, "pack": False, "passages": False}, "the pack is served by the session's tools"
    assert v1.switches == v0.switches, "the evidence rules are v0's, so the two agents are compared on the same evidence"
    cfg = v1.loop.config(v1.effort, v1.prompt_version)
    assert cfg.models() == {"executor": "claude-sonnet-5", "verifier": "claude-opus-5", "adjudicator": "claude-opus-5"}
    assert cfg.effort == "medium" and cfg.prompt_version == "analyst/v1/v1"


@pytest.mark.parametrize("name, changed", [
    ("v1-noverify", {"loop.verifier"}),
    ("v1-K3", {"loop.rounds"}),
    ("v1-strong", {"loop.executor_model"}),
    ("v1-triage", {"loop.triage", "switches.oof_scores"}),
    ("v1-modelplanner", {"loop.planner"}),
    ("v1-cumulative", {"loop.executor_context"}),
])
def test_each_v1_ablation_differs_from_the_v1_headline_in_exactly_the_stated_way(name: str, changed: set[str]) -> None:
    assert set(_diff(A.load_arm("v1"), A.load_arm(name))) == changed


def test_a_loop_table_on_a_v0_arm_and_a_v1_arm_without_one_are_refused(tmp_path) -> None:
    import tomllib

    v1 = tomllib.loads((A.arms_dir() / "v1.toml").read_text())
    v1["arm"]["name"] = "x"
    without = {**v1, "arm": {k: v for k, v in v1["arm"].items() if k != "loop"}}
    with pytest.raises(ValueError, match="loop"):
        A.parse_arm(without)
    v0 = tomllib.loads((A.arms_dir() / "v0.toml").read_text())
    v0["arm"]["loop"] = v1["arm"]["loop"]
    with pytest.raises(ValueError, match="loop"):
        A.parse_arm(v0)
    typed = {**v1, "arm": {**v1["arm"], "loop": {**v1["arm"]["loop"], "rounds": "3"}}}
    with pytest.raises(ValueError, match="rounds"):
        A.parse_arm(typed)

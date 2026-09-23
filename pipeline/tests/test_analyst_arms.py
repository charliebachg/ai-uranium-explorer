"""The arm files: every one loads, the headline has every switch off, and each ablation changes one thing."""

from __future__ import annotations

import tomllib
from dataclasses import asdict

import pytest

from uranium_explorer.analyst import arms as A

V0 = ["v0", "v0-text", "v0-card", "v0-sonnet", "v0-holes", "v0-labels", "v0-scores", "v0-retrieval", "v0-features", "v0-qwen38"]
V1 = ["v1", "v1-noverify", "v1-K1", "v1-strong", "v1-triage", "v1-modelplanner", "v1-cumulative", "v1-cheap", "v1-openrouter", "v1-anthropic-or",
      "v1-skipunmeasured", "v1-batch", "v1-scoped", "v1-scoped-batch"]
#: the information ladder: D1 is a v0 arm, D2 the family-staged v2 agent; on v3, D3 adds the report-text reader
#: and D3-swap is its placebo
LADDER = {"d1": "v0", "d2": "v2", "d3": "v2", "d3-swap": "v2"}
ALL = V0 + V1 + list(LADDER)


def test_every_arm_file_loads_and_says_which_question_it_answers() -> None:
    assert A.list_arms() == sorted(ALL)
    notes = set()
    for name in ALL:
        arm = A.load_arm(name)
        agent = LADDER.get(name) or ("v1" if name in V1 else "v0")
        assert arm.name == name and arm.agent == agent and arm.notes.strip()
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
    ("v0-qwen38", {"model"}),
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
        ("skeptic", 3, False, "independent", "both"), "K = 3 by default since F7: one round discards the verifier's feedback"
    assert asdict(v1.inputs) == {"card": True, "pack": False, "passages": False}, "the pack is served by the session's tools"
    assert v1.switches == v0.switches, "the evidence rules are v0's, so the two agents are compared on the same evidence"
    cfg = v1.loop.config(v1.effort, v1.prompt_version)
    assert cfg.models() == {"executor": "claude-sonnet-5", "verifier": "claude-opus-5", "adjudicator": "claude-opus-5"}
    assert cfg.effort == "medium" and cfg.prompt_version == "analyst/v1/v1"


@pytest.mark.parametrize("name, changed", [
    ("v1-noverify", {"loop.verifier"}),
    ("v1-K1", {"loop.rounds"}),
    ("v1-strong", {"loop.executor_model"}),
    ("v1-triage", {"loop.triage", "switches.oof_scores"}),
    ("v1-modelplanner", {"loop.planner"}),
    ("v1-cumulative", {"loop.executor_context"}),
    ("v1-cheap", {"loop.executor_model"}),
    ("v1-openrouter", {"model", "max_budget_usd_per_call", "loop.executor_model", "loop.verifier_model", "loop.adjudicator_model", "loop.planner_model"}),
    ("v1-anthropic-or", {"model", "loop.executor_model", "loop.verifier_model", "loop.adjudicator_model", "loop.planner_model"}),
    ("v1-skipunmeasured", {"loop.skip_unmeasured"}),
    ("v1-batch", {"loop.executor_batch"}),
    ("v1-scoped", {"loop.segment_scoped"}),
    ("v1-scoped-batch", {"loop.executor_batch", "loop.segment_scoped"}),
])
def test_each_v1_ablation_differs_from_the_v1_headline_in_exactly_the_stated_way(name: str, changed: set[str]) -> None:
    assert set(_diff(A.load_arm("v1"), A.load_arm(name))) == changed


def test_the_three_cost_switches_are_off_in_the_headline_and_stated_in_every_v1_arm() -> None:
    v1 = A.load_arm("v1")
    assert v1.loop is not None and (v1.loop.skip_unmeasured, v1.loop.executor_batch, v1.loop.segment_scoped) == (False, False, False)
    cfg = v1.loop.config(v1.effort, v1.prompt_version)
    assert (cfg.skip_unmeasured, cfg.executor_batch, cfg.segment_scoped) == (False, False, False)
    skip, batch, scoped = A.load_arm("v1-skipunmeasured"), A.load_arm("v1-batch"), A.load_arm("v1-scoped")
    assert skip.loop is not None and batch.loop is not None and scoped.loop is not None
    assert skip.loop.config(skip.effort, skip.prompt_version).skip_unmeasured is True
    assert batch.loop.config(batch.effort, batch.prompt_version).executor_batch is True
    assert scoped.loop.config(scoped.effort, scoped.prompt_version).segment_scoped is True
    both = A.load_arm("v1-scoped-batch")
    assert both.loop is not None and (both.loop.segment_scoped, both.loop.executor_batch) == (True, True)
    raw = tomllib.loads((A.arms_dir() / "v1.toml").read_text())
    for key in ("skip_unmeasured", "executor_batch", "segment_scoped"):
        without = {**raw, "arm": {**raw["arm"], "loop": {k: v for k, v in raw["arm"]["loop"].items() if k != key}}}
        with pytest.raises(ValueError, match=key):
            A.parse_arm(without)
        typed = {**raw, "arm": {**raw["arm"], "loop": {**raw["arm"]["loop"], key: "false"}}}
        with pytest.raises(ValueError, match=key):
            A.parse_arm(typed)


def test_the_scoped_view_moves_the_manifest_hash_and_nothing_else() -> None:
    """The run manifest's config is the arm as a dict; two arms that differ in one switch hash apart."""
    from uranium_explorer.ids import sha256_json

    v1, scoped, both = A.load_arm("v1"), A.load_arm("v1-scoped"), A.load_arm("v1-scoped-batch")
    hashes = {sha256_json(a.as_dict()) for a in (v1, scoped, both)}
    assert len(hashes) == 3
    assert _diff(v1, scoped) == {"loop.segment_scoped": (False, True)}
    assert _diff(scoped, both) == {"loop.executor_batch": (False, True)}
    assert scoped.switches == v1.switches and scoped.inputs == v1.inputs, "the evidence rules are the headline's"
    assert "high potential" not in scoped.notes and "drill target" not in scoped.notes


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


def test_a_later_switch_may_be_left_out_of_a_file_and_is_off_when_it_is() -> None:
    v0 = A.load_arm("v0")
    assert (v0.switches.evidence, v0.switches.region, v0.switches.extended_features) == (False, False, False)
    # the cache key sees exactly the five switches every arm had before the later ones, so no cached answer moves
    assert v0.switches.keyed() == {"drillholes": False, "label_context": False, "oof_scores": False,
                                   "effort_features": False, "criteria": True}


def test_the_ladder_differs_from_the_headline_in_the_information_and_d2_from_d1_in_the_agent() -> None:
    later = {f"switches.{k}" for k in A.LATER_SWITCHES}
    assert set(_diff(A.load_arm("v0"), A.load_arm("d1"))) == later | {"prompt_version"}
    assert set(_diff(A.load_arm("d1"), A.load_arm("d2"))) == {"agent", "prompt_version"}
    d1 = A.load_arm("d1")
    assert d1.switches.keyed()["evidence"] is True and d1.model == A.load_arm("v0").model


def test_an_unknown_switch_is_still_refused_when_the_later_ones_are_optional(tmp_path) -> None:
    raw = tomllib.loads((A.arms_dir() / "v0.toml").read_text())
    raw["arm"]["switches"]["imagery"] = True
    with pytest.raises(ValueError, match="unknown"):
        A.parse_arm(raw)


def test_d3_is_d2_with_report_text_and_its_placebo_differs_only_in_whose_text() -> None:
    d2, d3, swap = A.load_arm("d2"), A.load_arm("d3"), A.load_arm("d3-swap")
    assert d3.switches == d2.switches and d3.model == d2.model and d3.effort == d2.effort
    assert asdict(d3.inputs) == {**asdict(d2.inputs), "passages": True}
    assert (d3.passages_view, swap.passages_view) == ("own", "swapped")
    assert swap.inputs == d3.inputs and swap.switches == d3.switches
    assert d2.passages_view == "own", "an arm file without the key reads its own passages"

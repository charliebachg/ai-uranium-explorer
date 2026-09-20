"""The v1 prompts: closed-book, every role told to cite value ids, pinned and hashable, and the executor's
staged files named under the `{STAGE_DIR}` placeholder."""

from __future__ import annotations

import pytest

from legacy_reader.analyst import prompts as PR
from legacy_reader.analyst import v0 as V0
from legacy_reader.analyst import wire as W
from legacy_reader.analyst.v0 import VERDICTS, place_names_in
from legacy_reader.prospect.memo import CRITERIA_FILE, HANDBOOK
from legacy_reader.prospect.tools import TOOL_HELP

COND, FAULT = "b:b01:cell:d_conductor_m", "b:b01:cell:d_fault_m"
SEGMENT = W.Segment(segment_id="s01", kind="criterion", criterion="conductor_proximity",
                    purpose="Establish whether the conductor criterion holds here.",
                    tool_calls=[{"tool": "cell_features", "args": {"cell_id": "$cell"}}])
CROSS = W.Segment(segment_id="s09", kind="crosscheck", criterion="conductor_fault",
                  purpose="Establish whether the conductor and the fault corridor coincide.",
                  tool_calls=[{"tool": "crosscheck", "args": {"cell_id": "$cell"}}], depends_on=["s01", "s03"])
NODES = [W.Node("n01", "s01", "criterion", "conductor_proximity", "met", 4, [COND],
                "The nearest mapped conductor is 820 m away.", published=True),
         W.Node("n03", "s03", "criterion", "fault_proximity", "not_met", 1, [FAULT],
                "The nearest fault is 4,800 m away.", published=True)]
CHAIN_TEXT = "\n".join(["chain ch-1 for cell b01 (benchmark)", *(n.line() for n in NODES)])
EFFORT = "effort score 0.81 [b:b01:score:effort], learned 0.44 [b:b01:score:learned]"


def flat(text: str) -> str:
    """The prompt unwrapped and lower-cased, so a phrase check does not depend on where a line breaks."""
    return " ".join(text.split()).lower()


def every_prompt() -> dict[str, str]:
    return {
        "executor_system": PR.executor_system(HANDBOOK.read_text(), CRITERIA_FILE.read_text()),
        "executor_user": PR.executor_user(SEGMENT, ["tool_01_cell_features.json"], card=True, prior_nodes=[]),
        "executor_user_cross": PR.executor_user(CROSS, ["tool_01_crosscheck.json"], card=False, prior_nodes=NODES),
        "verifier_system": PR.verifier_system(),
        "verifier_user": PR.verifier_user(CHAIN_TEXT, EFFORT),
        "adjudicator_system": PR.adjudicator_system(),
        "adjudicator_user": PR.adjudicator_user(CHAIN_TEXT, EFFORT),
        "planner_system": PR.planner_system(),
        "planner_user": PR.planner_user(V0._criteria(CRITERIA_FILE.read_text()), list(TOOL_HELP.values()),
                                        {"d_conductor_m": "measured", "water_u_max_ppm": "unmeasured"}),
    }


def test_no_prompt_names_a_place_and_every_prompt_mentions_value_ids() -> None:
    for name, text in every_prompt().items():
        assert place_names_in(text) == [], name
        low = flat(text)
        for leak in ("athabasca", "saskatchewan", "mcarthur", "cigar", "patterson"):
            assert leak not in low, (name, leak)
        assert "value id" in low, name
        assert "high potential" not in low and "drill target" not in low, name
        assert "place" in low, f"{name} must say no places"


def test_a_prompt_that_would_name_a_place_is_refused() -> None:
    handbook = HANDBOOK.read_text().replace("- **Trap** — structure", "- **Trap** — structure, as at McArthur River")
    with pytest.raises(ValueError, match="McArthur"):
        PR.executor_system(handbook, CRITERIA_FILE.read_text())
    leaky = NODES[0].line().replace("820 m away", "820 m away, near Cigar Lake")
    with pytest.raises(ValueError, match="Cigar Lake"):
        PR.verifier_user(leaky, EFFORT)
    with pytest.raises(ValueError, match="Arrow"):
        PR.executor_user(SEGMENT, ["tool_01.json"], card=False,
                         prior_nodes=[W.Node("n02", "s02", "criterion", "fault_proximity", "met", 2, [FAULT],
                                             "As at Arrow, the fault is 4,800 m away.")])
    with pytest.raises(ValueError, match="Wollaston"):
        PR.planner_user([], [], {"d_conductor_m": "measured in the Wollaston domain"})


def test_the_executor_system_prompt_states_the_one_step_rules() -> None:
    text = every_prompt()["executor_system"]
    low = flat(text)
    for phrase in ("one step", "only the staged evidence", "no place names", "never compute", "unknown is not absent",
                   "met or not_met", "strength", "expert_ids", "unknown_reason", "folklore", "never recommend drilling"):
        assert phrase in low, phrase
    assert "conductor_proximity" in text and "em_bright_spot" in text, "the criteria lines are v0's"
    assert "Detection: is there a geochemical or radiometric anomaly" in text, "the frame is v0's"
    assert "met | not_met | unknown" in text
    assert PR.default_executor_system() == text


def test_the_executor_user_prompt_lists_the_staged_files_under_the_placeholder() -> None:
    text = PR.executor_user(SEGMENT, ["tool_01_cell_features.json", "tool_02_coverage.json"], card=True, prior_nodes=[])
    assert text.startswith("Segment s01: criterion conductor_proximity.")
    assert "Purpose: Establish whether the conductor criterion holds here." in text
    assert f"{{STAGE_DIR}}/{V0.CARD_FILE}" in text
    assert "{STAGE_DIR}/tool_01_cell_features.json" in text and "{STAGE_DIR}/tool_02_coverage.json" in text
    assert "Prior nodes" not in text, "a criterion executor sees only its segment"
    no_card = PR.executor_user(SEGMENT, ["tool_01_cell_features.json"], card=False, prior_nodes=[])
    assert V0.CARD_FILE not in no_card
    assert "nothing is staged" in PR.executor_user(SEGMENT, [], card=False, prior_nodes=[])


def test_the_executor_user_prompt_renders_prior_nodes_with_their_ids_only_when_given() -> None:
    text = PR.executor_user(CROSS, ["tool_01_crosscheck.json"], card=False, prior_nodes=NODES)
    assert text.startswith("Segment s09: crosscheck conductor_fault.")
    assert "Prior nodes you may build on" in text and "depends_on" in text
    for n in NODES:
        assert n.line() in text and all(v in text for v in n.value_ids)
    assert "return one node as JSON for conductor_fault" in text


def test_the_verifier_brief_is_the_skeptics_and_records_its_own_label() -> None:
    text = PR.verifier_system()
    low = flat(text)
    for phrase in ("does a verdict follow", "contradict", "effort null", "folklore", "known deposit", "really unmeasured",
                   "stop at the first decisive", "recorded", "nothing acts on them", "candidate_label",
                   "candidate_probability", "faulty"):
        assert phrase in low, phrase
    for verdict in VERDICTS:
        assert verdict in text
    user = PR.verifier_user(CHAIN_TEXT, EFFORT)
    assert CHAIN_TEXT in user and EFFORT in user
    assert "(not available)" in PR.verifier_user(CHAIN_TEXT, "")


def test_the_adjudicator_rules_are_v0s_over_the_nodes() -> None:
    text = PR.adjudicator_system()
    low = flat(text)
    for phrase in ("unknown is not absent", "never compute", "next_observation", "unknown_criteria", "absent_criteria",
                   "not a probability that ore is present", "effort null", "folklore", "gate:"):
        assert phrase in low, phrase
    for verdict in VERDICTS:
        assert verdict in text
    assert f"at most {V0.RATIONALE_MAX} characters" in text, "the answer is v0's schema, so v0's limit"
    user = PR.adjudicator_user(CHAIN_TEXT, EFFORT)
    assert CHAIN_TEXT in user and "unknown criteria from" in user


def test_the_planner_prompts_name_the_rules_the_tools_and_the_coverage() -> None:
    system = PR.planner_system()
    low = flat(system)
    for phrase in ("reorder", "retrieval", "drop a criterion", "unmeasured", "may not add a tool", "folklore", "$cell",
                   "depends_on", "segments"):
        assert phrase in low, phrase
    user = every_prompt()["planner_user"]
    assert "conductor_proximity" in user and "water_u_max_ppm: unmeasured" in user
    for line in TOOL_HELP.values():
        assert line in user
    assert "(no coverage flags)" in PR.planner_user([], [], {})


def test_prompt_hashes_are_stable_and_pinned() -> None:
    a, b = PR.prompt_hashes(), PR.prompt_hashes()
    assert a == b
    assert set(a) == {"executor_system", "verifier_system", "adjudicator_system", "planner_system"}
    assert all(len(h) == 64 for h in a.values()) and len(set(a.values())) == 4
    assert PR.PROMPT_VERSION == "analyst/v1/v1"

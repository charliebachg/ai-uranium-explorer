"""Analyst_v0: the closed-book prompt, what an arm stages, the switches, and the gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from uranium_explorer.analyst import arms as A
from uranium_explorer.analyst import v0 as V0
from uranium_explorer.prospect.memo import CRITERIA_FILE, HANDBOOK

from fake_bench import AnalystBackend, bad_answer, good_answer, ids_for, make_pack

PACK = make_pack("b01")
IDS = ids_for("b01")


@pytest.fixture
def card(tmp_path: Path) -> Path:
    p = tmp_path / "card.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"card" * 8)
    return p


# ---------------------------------------------------------------- the prompt

def test_the_system_prompt_states_the_closed_book_rules_and_names_no_place() -> None:
    prompt = V0.system_prompt(HANDBOOK.read_text(), CRITERIA_FILE.read_text())
    low = prompt.lower()
    for phrase in ("only the evidence provided", "no place names", "do not try to recognise the map", "value id",
                   "unknown is not absent", "never compute", "never recommend drilling", "folklore",
                   "not a probability that ore is present"):
        assert phrase in low, phrase
    for verdict in V0.VERDICTS:
        assert verdict in prompt
    assert V0.place_names_in(prompt) == []
    for name in ("Athabasca", "Saskatchewan", "McArthur", "Cigar", "Patterson"):
        assert name.lower() not in low, name
    assert "conductor_proximity" in prompt and "em_bright_spot" in prompt, "the criteria are listed by key"
    assert "Detection: is there a geochemical or radiometric anomaly" in prompt
    assert "sampling?" in prompt, "a wrapped handbook bullet is unwrapped, not cut"


def test_a_handbook_that_names_a_place_in_the_frame_is_refused() -> None:
    handbook = HANDBOOK.read_text().replace("- **Trap** — structure", "- **Trap** — structure, as at McArthur River")
    with pytest.raises(ValueError, match="McArthur"):
        V0.system_prompt(handbook, CRITERIA_FILE.read_text())


def test_place_names_match_whole_words_only() -> None:
    assert V0.place_names_in("566 samples over a narrow band") == []
    assert V0.place_names_in("the Arrow deposit") == ["Arrow"]


# ---------------------------------------------------------------- what is staged

def test_build_request_stages_the_card_only_when_the_arm_says_so(tmp_path: Path, card: Path) -> None:
    passages = [{"file": "74H09-0039", "page": 3, "tier": "read", "text": "A passage."}]
    v0 = V0.build_request(PACK, card, passages, A.load_arm("v0"), stage=tmp_path / "s0")
    assert v0.images == (card,) and [n for _p, n in v0.stage_files] == [V0.PACK_FILE]
    assert v0.task == "analyst_v0" and v0.model == "claude-opus-5" and v0.schema == V0.ANSWER_SCHEMA
    text = V0.build_request(PACK, card, passages, A.load_arm("v0-text"), stage=tmp_path / "s1")
    assert text.images == () and [n for _p, n in text.stage_files] == [V0.PACK_FILE]
    only_card = V0.build_request(PACK, card, passages, A.load_arm("v0-card"), stage=tmp_path / "s2")
    assert only_card.images == (card,) and only_card.stage_files == ()
    retrieval = V0.build_request(PACK, card, passages, A.load_arm("v0-retrieval"), stage=tmp_path / "s3")
    assert [n for _p, n in retrieval.stage_files] == [V0.PACK_FILE, V0.PASSAGES_FILE]
    assert "A passage." in (tmp_path / "s3" / V0.PASSAGES_FILE).read_text()
    for req in (v0, text, only_card, retrieval):
        assert not any("key" in n for _p, n in req.stage_files), "the answer key is never staged"
        assert f"{{STAGE_DIR}}/{V0.PACK_FILE}" in req.user_prompt or not req.stage_files
    assert f"{{STAGE_DIR}}/{V0.CARD_FILE}" in v0.user_prompt and V0.CARD_FILE not in text.user_prompt


def test_a_card_only_arm_with_no_card_has_nothing_to_stage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nothing to stage"):
        V0.build_request(PACK, None, [], A.load_arm("v0-card"), stage=tmp_path)


def test_the_cache_key_changes_with_the_switches_and_the_inputs(tmp_path: Path, card: Path) -> None:
    keys = {}
    for name in ("v0", "v0-labels", "v0-holes", "v0-scores", "v0-text", "v0-sonnet"):
        req = V0.build_request(PACK, card, [], A.load_arm(name), stage=tmp_path / name)
        keys[name] = req.cache_key("claude_cli")
    assert len(set(keys.values())) == len(keys)
    again = V0.build_request(PACK, card, [], A.load_arm("v0"), stage=tmp_path / "again").cache_key("claude_cli")
    assert again == keys["v0"], "the same arm over the same evidence is one cached answer"
    # the switches are in the context hash even when they remove nothing from this pack
    bare = {"bench_id": "b01", "values": {}, "rows": {"cell_features": []}}
    a = V0.build_request(bare, card, [], A.load_arm("v0"), stage=tmp_path / "bare0")
    b = V0.build_request(bare, card, [], A.load_arm("v0-labels"), stage=tmp_path / "bare1")
    assert (tmp_path / "bare0" / V0.PACK_FILE).read_text() == (tmp_path / "bare1" / V0.PACK_FILE).read_text()
    assert a.cache_key("claude_cli") != b.cache_key("claude_cli")


# ---------------------------------------------------------------- the switches

def test_switches_off_remove_rows_values_and_the_prerendered_text() -> None:
    shown = V0.apply_switches(PACK, A.load_arm("v0").switches)
    assert set(shown["tools"]) == {"cell_features", "criteria_breakdown", "coverage"}
    assert [r["feature"] for r in shown["tools"]["cell_features"]["rows"]] == ["d_conductor_m", "water_u_max_ppm"]
    assert [r["feature"] for r in shown["tools"]["coverage"]["rows"]] == ["d_conductor_m"]
    assert shown["tools"]["cell_features"]["note"] == "provincial survey 1975-1978", "the tool's note survives"
    assert set(shown["values"]) == {IDS["cond"], IDS["crit"]}
    assert "text" not in shown and shown["switches"] == PACK["switches"]
    for rendered in (V0.pack_text(shown), V0.render_pack(shown)):
        for leak in ("holes_n", "12.4", "0.61", "nearest label", "learned"):
            assert leak not in rendered, leak
        assert IDS["cond"] in rendered and "820" in rendered


def test_a_plain_rows_pack_is_filtered_the_same_way() -> None:
    plain = {"bench_id": "x", "values": {"c:cell:0001_0001:holes_n": {"value": 3}, "c:near:0001_0001:0": {"value": 2.0},
                                          "c:cell:0001_0001:d_fault_m": {"value": 40.0}},
             "rows": {"cell_features": [{"feature": "holes_n", "value": 3}, {"feature": "d_fault_m", "value": 40.0}],
                      "label_context": [{"km": 2.0}]}}
    shown = V0.apply_switches(plain, A.load_arm("v0").switches)
    assert shown["rows"] == {"cell_features": [{"feature": "d_fault_m", "value": 40.0}]}
    assert set(shown["values"]) == {"c:cell:0001_0001:d_fault_m"}


def test_switches_on_leave_the_pack_alone() -> None:
    everything = A.Switches(drillholes=True, label_context=True, oof_scores=True, effort_features=True, criteria=True)
    assert V0.apply_switches(PACK, everything) == PACK


def test_each_switch_admits_its_own_part() -> None:
    holes = V0.apply_switches(PACK, A.load_arm("v0-holes").switches)
    assert IDS["holes"] in holes["values"] and "holes_n" in [r["feature"] for r in holes["tools"]["coverage"]["rows"]]
    assert "label_context" not in holes["tools"] and IDS["near"] not in holes["values"]
    labels = V0.apply_switches(PACK, A.load_arm("v0-labels").switches)
    assert "label_context" in labels["tools"] and IDS["near"] in labels["values"] and IDS["holes"] not in labels["values"]
    scores = V0.apply_switches(PACK, A.load_arm("v0-scores").switches)
    assert "cell_scores" in scores["tools"] and IDS["score"] in scores["values"]


# ---------------------------------------------------------------- the gate

def test_the_gate_accepts_a_cited_number_and_rejects_an_uncited_one() -> None:
    shown = V0.apply_switches(PACK, A.load_arm("v0").switches)
    assert V0.gate(good_answer("b01"), shown) == []
    problems = V0.gate(bad_answer("b01"), shown)
    assert any("2.4" in p for p in problems)


def test_the_gate_holds_the_answer_to_what_the_arm_showed() -> None:
    """A value the switches removed cannot be cited: the model never saw it."""
    a = good_answer("b01")
    a["claims"] = [{"text": "There are 12 holes here.", "value_ids": [IDS["holes"]]}]
    assert any("no tool returned" in p for p in V0.gate(a, V0.apply_switches(PACK, A.load_arm("v0").switches)))
    assert V0.gate(a, V0.apply_switches(PACK, A.load_arm("v0-holes").switches)) == []


def test_the_gate_checks_the_rationale_and_lets_quoted_strings_and_the_probability_through() -> None:
    shown = V0.apply_switches(PACK, A.load_arm("v0").switches)
    a = good_answer("b01", probability=0.3)
    a["rationale"] = "The 1975-1978 survey covers this ground; I put it at 0.3."
    assert V0.gate(a, shown) == []
    a["rationale"] = "The unconformity here is 512.7 m deep."
    assert any(p.startswith("rationale:") and "512.7" in p for p in V0.gate(a, shown))


def test_the_gate_schema_checks() -> None:
    shown = V0.apply_switches(PACK, A.load_arm("v0").switches)
    assert any("verdict" in p for p in V0.gate({**good_answer("b01"), "verdict": "drill it"}, shown))
    assert any("probability" in p for p in V0.gate({**good_answer("b01"), "probability": 1.4}, shown))
    assert any("probability" in p for p in V0.gate({**good_answer("b01"), "probability": "high"}, shown))
    assert any("no claims" in p for p in V0.gate({**good_answer("b01"), "claims": []}, shown))
    assert V0.gate({**good_answer("b01", verdict="insufficient"), "claims": []}, shown) == []
    assert any("unknown_criteria" in p for p in V0.gate({**good_answer("b01"), "unknown_criteria": None}, shown))
    assert any("observation" in p for p in V0.gate({**good_answer("b01"), "next_observation": " "}, shown))
    assert any("rationale is" in p for p in V0.gate({**good_answer("b01"), "rationale": "x" * 601}, shown))


def test_the_answer_schema_is_what_the_prompt_promises() -> None:
    props = V0.ANSWER_SCHEMA["properties"]
    assert set(V0.ANSWER_SCHEMA["required"]) == set(props) == {
        "verdict", "probability", "claims", "unknown_criteria", "absent_criteria", "next_observation", "rationale"}
    assert props["verdict"]["enum"] == list(V0.VERDICTS)
    assert props["probability"]["minimum"] == 0 and props["probability"]["maximum"] == 1
    assert props["rationale"]["maxLength"] == 600


# ---------------------------------------------------------------- one cell

def test_run_cell_returns_the_row_and_cleans_its_stage(card: Path) -> None:
    backend = AnalystBackend()
    row = V0.run_cell(backend, PACK, card, [], A.load_arm("v0"))
    assert set(row) == {"bench_id", "answer", "problems", "published", "cost_usd", "duration_s", "model_resolved",
                        "cache_key", "from_cache"}
    assert row["bench_id"] == "b01" and row["published"] is True and row["problems"] == []
    assert row["cost_usd"] == 0.05 and row["from_cache"] is False and row["model_resolved"] == "claude-opus-5"
    req = backend.requests[0]
    assert not req.stage_files[0][0].exists(), "the stage directory is removed after the call"
    rejected = V0.run_cell(AnalystBackend(script={"b01": bad_answer("b01")}), PACK, card, [], A.load_arm("v0"))
    assert rejected["published"] is False and any("2.4" in p for p in rejected["problems"])
    assert rejected["answer"]["verdict"] == "supports_closer_look", "the answer is kept beside its problems"


def test_the_criteria_switch_removes_the_criteria_table_and_its_ids() -> None:
    pack = {"bench_id": "b-0001", "tools": {
        "cell_features": {"rows": [{"feature": "d_fault_m", "value": 40.0, "value_id": "b:b-0001:cell:d_fault_m"}]},
        "criteria_breakdown": {"rows": [{"criterion": "fault_proximity", "membership": 1.0, "membership_id": "b:b-0001:crit:fault_proximity"}]}},
        "values": {"b:b-0001:cell:d_fault_m": {"id": "b:b-0001:cell:d_fault_m", "value": 40.0},
                   "b:b-0001:crit:fault_proximity": {"id": "b:b-0001:crit:fault_proximity", "value": 1.0}}}
    shown = V0.apply_switches(pack, A.load_arm("v0-features").switches)
    assert "criteria_breakdown" not in shown["tools"] and set(shown["values"]) == {"b:b-0001:cell:d_fault_m"}
    kept = V0.apply_switches(pack, A.load_arm("v0").switches)
    assert "criteria_breakdown" in kept["tools"] and "b:b-0001:crit:fault_proximity" in kept["values"]


def _later_pack() -> dict:
    """The fake pack plus what a v2 pack carries: an evidence tool, the region and an extended feature."""
    pack = make_pack("b01")
    ev_id, rg_id, xf_id = "b:b01:ev:b0:cps", "b:b01:ev:cov:dist_to_cover_edge_m", "b:b01:cell:sed_u_th_max"
    pack["tools"]["evidence_boulders"] = {"note": "", "rows": [{"kind": "boulder", "cps": 900.0, "cps_id": ev_id}]}
    pack["tools"]["region"] = {"note": "", "rows": [{"kind": "cover", "dist_to_cover_edge_m": 2000.0,
                                                     "dist_to_cover_edge_m_id": rg_id}]}
    pack["tools"]["cell_features"]["rows"].append({"feature": "sed_u_th_max", "value": 3.1, "value_id": xf_id})
    pack["values"] |= {ev_id: {"id": ev_id, "value": 900.0}, rg_id: {"id": rg_id, "value": 2000.0},
                       xf_id: {"id": xf_id, "value": 3.1}}
    return pack


def test_the_later_switches_remove_the_evidence_the_region_and_the_extended_features_when_off() -> None:
    pack = _later_pack()
    off = V0.apply_switches(pack, A.load_arm("v0").switches)
    assert "evidence_boulders" not in off["tools"] and "region" not in off["tools"]
    assert "sed_u_th_max" not in [r["feature"] for r in off["tools"]["cell_features"]["rows"]]
    assert not {"b:b01:ev:b0:cps", "b:b01:ev:cov:dist_to_cover_edge_m", "b:b01:cell:sed_u_th_max"} & set(off["values"])
    on = V0.apply_switches(pack, A.load_arm("d1").switches)
    assert {"evidence_boulders", "region"} <= set(on["tools"]) and "b:b01:ev:b0:cps" in on["values"]
    assert "sed_u_th_max" in [r["feature"] for r in on["tools"]["cell_features"]["rows"]]
    # a pack built before the later switches has nothing for them to remove: v0 sees what it always saw
    assert V0.apply_switches(PACK, A.load_arm("v0").switches) == V0.apply_switches(make_pack("b01"),
                                                                                  A.load_arm("v0").switches)


def test_the_evidence_guide_is_added_only_for_an_arm_shown_the_evidence() -> None:
    plain = V0.system_for(A.load_arm("v0").switches)
    rich = V0.system_for(A.load_arm("d1").switches)
    assert plain == V0.default_system_prompt()
    assert rich.startswith(plain) and V0.EVIDENCE_GUIDE in rich and not V0.place_names_in(rich)


def test_a_sample_past_the_first_is_its_own_call_and_sample_0_keeps_the_key_it_had() -> None:
    from dataclasses import asdict

    from uranium_explorer.ids import sha256_json, short

    arm = A.load_arm("v0")
    before = short(sha256_json({"bench_id": "b01", "inputs": asdict(arm.inputs), "switches": arm.switches.keyed()}))
    assert V0.context_hash("b01", arm) == V0.context_hash("b01", arm, 0) == before
    assert len({V0.context_hash("b01", arm, s) for s in range(5)}) == 5

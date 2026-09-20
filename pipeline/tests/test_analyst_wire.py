"""The Analyst v1 wire protocol: every record round-trips, validation names the field, and the chain renders
for the verifier with every number beside its ids."""

from __future__ import annotations

import json
import re

import pytest

from legacy_reader.analyst import wire as W
from legacy_reader.analyst.v0 import ABSTAIN, POSITIVE, VERDICTS
from legacy_reader.prospect.memo import BARE_OK, numbers_in
from legacy_reader.prospect.tools import TOOL_HELP

COND, FAULT, CRIT = "b:b01:cell:d_conductor_m", "b:b01:cell:d_fault_m", "b:b01:crit:conductor_proximity"


def segment(sid: str = "s01", kind: str = "criterion", criterion: str | None = "conductor_proximity",
            depends_on: list[str] | None = None, **kw) -> W.Segment:
    fields = dict(segment_id=sid, kind=kind, criterion=criterion, purpose="Establish the conductor distance.",
                  tool_calls=[{"tool": "cell_features", "args": {"cell_id": "$cell"}}], depends_on=depends_on or [])
    fields.update(kw)
    return W.Segment(**fields)


def plan() -> W.Plan:
    return W.Plan(planner="template", prompt_version="analyst/v1/template/v1", segments=[
        segment("s01"), segment("s02", criterion="fault_proximity"),
        segment("s03", kind="crosscheck", criterion="conductor_fault", depends_on=["s01", "s02"])])


def node(nid: str = "n01", sid: str = "s01", status: str = "met", strength: int = 4, ids: list[str] | None = None,
         text: str = "The nearest mapped conductor is 820 m away.", **kw) -> W.Node:
    fields = dict(node_id=nid, segment_id=sid, kind="criterion", criterion="conductor_proximity", status=status,
                  strength=strength, value_ids=[COND] if ids is None else ids, text=text, published=True)
    fields.update(kw)
    return W.Node(**fields)


def verdict(round: int = 0, valid: bool = True, **kw) -> W.VerifierVerdict:
    fields = dict(round=round, valid=valid, faulty=[] if valid else [{"node_id": "n01", "reason": "rests on an unknown"}],
                  feedback="" if valid else "The conductor node reads a gap as a distance.",
                  candidate_label=POSITIVE, candidate_probability=0.7, rationale="The pathway is there.")
    fields.update(kw)
    return W.VerifierVerdict(**fields)


def decision(**kw) -> W.Decision:
    fields = dict(final_verdict=POSITIVE, final_probability=0.7, weighted_score=0.6, weights_version="oof/fold0",
                  adjudicator={"verdict": POSITIVE, "probability": 0.7, "claims": [], "unknown_criteria": [],
                               "absent_criteria": [], "next_observation": "A ground EM line.", "rationale": "ok"})
    fields.update(kw)
    return W.Decision(**fields)


def chain(**kw) -> W.Chain:
    fields = dict(chain_id="ch-0001", cell="b01", purpose="benchmark", plan=plan(),
                  nodes=[node(), node("n02", "s02", ids=[FAULT], text="The nearest fault is 1,400 m away.",
                                     criterion="fault_proximity"),
                         node("n03", "s03", kind="crosscheck", criterion="conductor_fault", strength=3,
                              ids=[COND, FAULT], depends_on=["n01", "n02"],
                              text="Conductor at 820 m and fault at 1,400 m: the corridor pairing holds.")],
                  verdicts=[verdict()], decision=decision(), published=True, run_id="bench-001",
                  manifest_sha256="a" * 64)
    fields.update(kw)
    return W.Chain(**fields)


# ---------------------------------------------------------------- round trips

def test_every_record_round_trips_through_its_dict() -> None:
    for rec, cls in ((segment(), W.Segment), (plan(), W.Plan), (node(), W.Node), (verdict(), W.VerifierVerdict),
                     (decision(), W.Decision), (chain(), W.Chain)):
        d = rec.as_dict()
        again = cls.from_dict(json.loads(json.dumps(d)))
        assert again == rec, cls.__name__
        assert again.as_dict() == d
    assert chain().as_dict()["schema_version"] == W.SCHEMA_VERSION == "1.0.0"


def test_from_dict_refuses_unknown_fields_by_name() -> None:
    with pytest.raises(ValueError, match="verdict"):
        W.Node.from_dict({**node().as_dict(), "verdict": "drill it"})


# ---------------------------------------------------------------- validation names the field

@pytest.mark.parametrize("bad, field", [
    (dict(node_id="node-1"), "node_id"),
    (dict(segment_id="seg1"), "segment_id"),
    (dict(kind="hunch"), "kind"),
    (dict(status="maybe"), "status"),
    (dict(strength=6), "strength"),
    (dict(strength=2.5), "strength"),
    (dict(text="x" * 301), "text"),
    (dict(text=""), "text"),
    (dict(expert_ids=["b:b01:cell:holes_n"]), "expert_ids"),
    (dict(attempt=4), "attempt"),
    (dict(attempt=0), "attempt"),
    (dict(round=-1), "round"),
    (dict(depends_on=["n00"]), "depends_on"),
    (dict(kind="crosscheck", criterion="conductor_proximity"), "criterion"),
    (dict(kind="retrieval", criterion="conductor_proximity"), "criterion"),
    (dict(criterion=None), "criterion"),
])
def test_node_validation_names_the_field(bad: dict, field: str) -> None:
    n = node(**bad)
    with pytest.raises(ValueError, match=f"^{field}"):
        n.validate()
    assert any(p.startswith(field) for p in n.check())


def test_strength_must_be_zero_when_unknown() -> None:
    with pytest.raises(ValueError, match="strength: must be 0 when the status is unknown"):
        node(status="unknown", strength=2, ids=[], text="No lake-water sample here.").validate()
    ok = node(status="unknown", strength=0, ids=[], text="No lake-water sample here.", unknown_reason="unmeasured")
    assert ok.check() == []


@pytest.mark.parametrize("bad, field", [
    (dict(round=-1), "round"),
    (dict(valid="yes"), "valid"),
    (dict(faulty=[{"node_id": "n01"}]), "faulty"),
    (dict(faulty=[{"node_id": "one", "reason": "x"}]), "faulty"),
    (dict(feedback="x" * 601), "feedback"),
    (dict(candidate_label="high potential"), "candidate_label"),
    (dict(candidate_probability=1.2), "candidate_probability"),
    (dict(rationale="x" * 401), "rationale"),
    (dict(valid=False, faulty=[], feedback=""), "feedback"),
])
def test_verifier_verdict_validation_names_the_field(bad: dict, field: str) -> None:
    with pytest.raises(ValueError, match=f"^{field}"):
        verdict(**bad).validate()


@pytest.mark.parametrize("bad, field", [
    (dict(final_verdict="drill target"), "final_verdict"),
    (dict(final_probability=-0.1), "final_probability"),
    (dict(weighted_score=0.6, weights_version=None), "weights_version"),
    (dict(adjudicator={"verdict": POSITIVE}), "adjudicator"),
    (dict(adjudicator={**decision().adjudicator, "verdict": "maybe"}), "adjudicator.verdict"),
    (dict(majority_label="maybe"), "majority_label"),
    (dict(abstained_reason="never validated", final_verdict=POSITIVE), "abstained_reason"),
])
def test_decision_validation_names_the_field(bad: dict, field: str) -> None:
    with pytest.raises(ValueError, match=f"^{field}"):
        decision(**bad).validate()


def test_an_abstention_publishes_insufficient_with_its_reason() -> None:
    d = decision(final_verdict=ABSTAIN, final_probability=0.5, weighted_score=None, weights_version=None,
                 adjudicator=None, abstained_reason="no round validated and the rounds disagree")
    assert d.check() == [] and d.final_verdict == "insufficient"


@pytest.mark.parametrize("bad, field", [
    (dict(segment_id="one"), "segment_id"),
    (dict(kind="guess"), "kind"),
    (dict(purpose=""), "purpose"),
    (dict(tool_calls=[{"tool": "dig", "args": {}}]), "tool_calls[0].tool"),
    (dict(tool_calls=["cell_features"]), "tool_calls[0]"),
    (dict(depends_on=["s00"]), "depends_on"),
])
def test_segment_validation_names_the_field(bad: dict, field: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(field)}"):
        segment(**bad).validate()


def test_plan_validation_needs_earlier_segments_and_unique_ids() -> None:
    p = plan()
    p.segments[2].depends_on = ["s09"]
    with pytest.raises(ValueError, match=r"segments\[s03\]\.depends_on"):
        p.validate()
    p = plan()
    p.segments[1].segment_id = "s01"
    with pytest.raises(ValueError, match="appears twice"):
        p.validate()
    with pytest.raises(ValueError, match="^planner"):
        W.Plan(planner="oracle", segments=[], prompt_version="v").validate()


def test_chain_validation_ties_nodes_to_the_plan() -> None:
    c = chain()
    c.nodes[0].segment_id = "s07"
    with pytest.raises(ValueError, match=r"nodes\[n01\]\.segment_id"):
        c.validate()
    c = chain()
    c.nodes[0].criterion = "fault_proximity"
    with pytest.raises(ValueError, match=r"nodes\[n01\]\.criterion"):
        c.validate()
    c = chain()
    c.verdicts[0].faulty = [{"node_id": "n09", "reason": "x"}]
    with pytest.raises(ValueError, match=r"verdicts\[0\]\.faulty"):
        c.validate()
    c = chain()
    c.nodes[0].published = False
    with pytest.raises(ValueError, match=r"nodes\[n01\]\.published: a rejected attempt is not stored"):
        c.validate()
    c = chain(decision=None)
    with pytest.raises(ValueError, match="^published"):
        c.validate()
    with pytest.raises(ValueError, match="^purpose"):
        chain(purpose="fun").validate()


# ---------------------------------------------------------------- the schemas

def test_the_model_schemas_cover_only_what_a_model_returns() -> None:
    assert set(W.NODE_SCHEMA["required"]) == {"criterion", "status", "strength", "value_ids", "text"}
    assert set(W.NODE_SCHEMA["properties"]) == set(W.NODE_SCHEMA["required"]) | {"expert_ids", "unknown_reason"}
    for harness_field in ("node_id", "published", "round", "attempt", "cost_usd", "problems"):
        assert harness_field not in W.NODE_SCHEMA["properties"]
    assert W.NODE_SCHEMA["properties"]["status"]["enum"] == ["met", "not_met", "unknown"]
    assert W.NODE_SCHEMA["properties"]["strength"]["maximum"] == 5
    assert W.NODE_SCHEMA["properties"]["text"]["maxLength"] == 300
    assert set(W.VERIFIER_SCHEMA["required"]) == {"valid", "faulty", "feedback", "candidate_label",
                                                  "candidate_probability", "rationale"}
    assert W.VERIFIER_SCHEMA["properties"]["candidate_label"]["enum"] == list(VERDICTS)
    assert W.VERIFIER_SCHEMA["properties"]["feedback"]["maxLength"] == 600
    assert W.ADJUDICATOR_SCHEMA["properties"]["verdict"]["enum"] == list(VERDICTS)
    tools = W.PLAN_SCHEMA["properties"]["segments"]["items"]["properties"]["tool_calls"]["items"]["properties"]["tool"]
    assert set(tools["enum"]) == set(TOOL_HELP) | {"nearby", "crosscheck"} == set(W.ALLOWED_TOOLS)
    assert len(W.ALLOWED_TOOLS) == len(set(W.ALLOWED_TOOLS))
    assert "high potential" not in json.dumps([W.NODE_SCHEMA, W.VERIFIER_SCHEMA, W.PLAN_SCHEMA]).lower()


def test_a_node_from_the_model_takes_the_segment_identity_and_refuses_another_criterion() -> None:
    payload = {"criterion": "conductor_proximity", "status": "met", "strength": 4, "value_ids": [COND],
               "text": "The nearest mapped conductor is 820 m away.", "expert_ids": []}
    n = W.Node.from_model(payload, "n01", segment(), round=1, attempt=2)
    assert (n.node_id, n.segment_id, n.kind, n.criterion, n.round, n.attempt) == ("n01", "s01", "criterion", "conductor_proximity", 1, 2)
    assert n.published is False and n.problems == [] and n.check() == []
    with pytest.raises(ValueError, match="^criterion"):
        W.Node.from_model({**payload, "criterion": "fault_proximity"}, "n01", segment())
    raw = W.Node.from_model({"status": "unknown", "strength": 3, "value_ids": [], "text": "?"}, "n02", segment())
    assert any(p.startswith("strength") for p in raw.check()), "the gate lists the problem instead of raising"
    v = W.VerifierVerdict.from_model({"valid": False, "faulty": [{"node_id": "n01", "reason": "x"}], "feedback": "fix",
                                      "candidate_label": ABSTAIN, "candidate_probability": 0.4, "rationale": ""}, round=2)
    assert v.round == 2 and v.faulty_ids() == ["n01"] and v.check() == []


# ---------------------------------------------------------------- the chain

def test_current_nodes_picks_the_latest_stored_node_per_segment_across_rounds() -> None:
    c = chain()
    repaired = node("n04", "s01", strength=2, round=1, attempt=2, text="The nearest conductor is 820 m away; one line.")
    recorded = node("n05", "s02", criterion="fault_proximity", status="unknown", strength=0, ids=[], round=1,
                    attempt=3, published=False, text="gate: the number # is not backed by any value this claim cites",
                    problems=["ids: the number 1400 is not backed by any value this claim cites"])
    c.nodes += [repaired, recorded]
    c.validate()
    current = c.current_nodes()
    assert [n.node_id for n in current] == ["n04", "n05", "n03"], "plan order, latest (round, attempt) per segment"
    assert c.rounds() == 2
    assert [n.node_id for n in chain().current_nodes()] == ["n01", "n02", "n03"]


def test_chain_text_renders_numbers_only_beside_ids() -> None:
    text = chain().chain_text()
    assert text.startswith("chain for cell b01 (benchmark)")
    lines = text.splitlines()
    assert "n01 | s01 criterion conductor_proximity | met | strength 4 | ids: " + COND in lines[2]
    assert "depends_on: n01, n02" in lines[4]
    for line in lines:
        numbers = [t for t in numbers_in(line) if t.strip(".,") not in BARE_OK]
        if numbers:
            node_id = line.split(" | ")[0]
            n = next(n for n in chain().nodes if n.node_id == node_id)
            assert n.value_ids and all(v in line for v in n.value_ids), line
    unknown = node("n09", "s01", status="unknown", strength=0, ids=[], text="No conductor mapped within reach.",
                   unknown_reason="no rows")
    assert unknown.line().endswith("| ids: - | depends_on: - | No conductor mapped within reach. (unknown_reason: no rows)")


def test_parse_id_reads_both_schemes_and_the_grid_wide_kinds() -> None:
    assert W.parse_id("b:b01:cell:d_conductor_m") == ("b01", "cell", ("d_conductor_m",))
    assert W.parse_id("c:cell:0001_0001:d_conductor_m:n_obs") == ("0001_0001", "cell", ("d_conductor_m", "n_obs"))
    assert W.parse_id("c:crit:0001_0001:conductor_proximity") == ("0001_0001", "crit", ("conductor_proximity",))
    assert W.parse_id("c:nb:0001_0001:em_conductors:5000:count") == ("0001_0001", "nb", ("em_conductors", "5000", "count"))
    assert W.parse_id("c:cov:d_conductor_m") == (None, "cov", ("d_conductor_m",))
    assert W.parse_id("c:pass:0:page") == (None, "pass", ("0", "page"))
    assert W.parse_id("c:metric:learned.spatial.auc") == (None, "metric", ("learned.spatial.auc",))
    assert W.parse_id("820") == (None, "", ("820",))


def test_a_verifier_verdict_with_prose_over_the_limit_is_clipped_not_refused() -> None:
    """A provider that ignores a schema's maxLength once returned 672 characters of feedback and the whole
    chain was voided for it; the prose is guidance, so it is cut and the verdict stands."""
    from legacy_reader.analyst.wire import FEEDBACK_MAX, RATIONALE_MAX, VerifierVerdict

    v = VerifierVerdict.from_model({"valid": False, "faulty": [{"node_id": "n02", "reason": "cover"}], "feedback": "x" * 900,
                                    "candidate_label": "insufficient", "candidate_probability": 0.3, "rationale": "y" * 500}, round=0)
    assert len(v.feedback) == FEEDBACK_MAX and v.feedback.endswith("…") and len(v.rationale) == RATIONALE_MAX
    assert v.check() == [] and v.faulty_ids() == ["n02"]

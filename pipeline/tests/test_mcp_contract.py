"""The tool contract on its own (PRD §E.3, §E.5): the catalogue, the schemas, and the shaping of a tool's rows
into values with ids where unknown and absent are two types and never a null."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from uranium_explorer.mcp import TOOL_VERSION
from uranium_explorer.mcp import contract as C
from uranium_explorer.mcp.auth import SCOPES
from uranium_explorer.prospect.tools import REGISTRY
from uranium_explorer.values import stat

from mcp_world import numbers_outside_vals


def test_the_catalogue_is_the_prd_table_in_order() -> None:
    assert list(C.CATALOGUE) == ["open_session", "cell_features", "cell_scores", "criteria_breakdown", "label_context",
                                 "nearby", "coverage", "crosscheck", "hole_crosscheck", "retrieve", "check_claims",
                                 "abstain", "record_insight", "run_analyst", "job_status"]
    assert set(C.REGISTRY_TOOLS) == set(REGISTRY), "the eight reads are exactly the deterministic tools the loops call"
    assert TOOL_VERSION == "prospect/tools/v2"
    kinds = {name: spec.kind for name, spec in C.CATALOGUE.items()}
    assert kinds["run_analyst"] == "task" and kinds["abstain"] == "action" and kinds["record_insight"] == "action"
    assert kinds["job_status"] == "read", "polling a job is a read"
    assert {spec.scope for spec in C.CATALOGUE.values()} <= set(SCOPES)
    assert C.CATALOGUE["record_insight"].scope == "record" and C.CATALOGUE["run_analyst"].scope == "run"
    assert all(spec.scope == "read" for name, spec in C.CATALOGUE.items() if name not in ("record_insight", "run_analyst"))
    assert C.CATALOGUE["run_analyst"].output["required"][:2] == ["job_id", "status"], "a job id, not a blocked call"


def test_every_read_tool_is_annotated_read_only_over_a_closed_store() -> None:
    for name, spec in C.CATALOGUE.items():
        tool = spec.tool()
        a = tool.annotations
        assert a is not None and a.open_world_hint is False, name
        if spec.kind == "read":
            assert a.read_only_hint is True, f"{name} is a read"
        else:
            assert a.read_only_hint is False and a.idempotent_hint is True, f"{name} is idempotent by handle"
        assert tool.input_schema["properties"].get("session_id") is not None or name == "open_session"
        Draft202012Validator.check_schema(tool.input_schema)
        Draft202012Validator.check_schema(tool.output_schema)


def test_unknown_and_absent_are_distinct_types_in_every_output_schema() -> None:
    for name, spec in C.CATALOGUE.items():
        defs = spec.output["$defs"]
        assert defs["Unknown"]["properties"]["state"] == {"const": "unknown"}, name
        assert defs["Absent"]["properties"]["state"] == {"const": "absent"}, name
        assert defs["Unknown"]["additionalProperties"] is False and defs["Absent"]["additionalProperties"] is False
        assert "null" not in json.dumps(defs["Unknown"]) and "null" not in json.dumps(defs["Absent"])
    unknown, absent = C.unknown("nobody measured it"), C.absent("mapped, nothing there")
    v = Draft202012Validator({"$ref": "#/$defs/Absent", "$defs": C.DEFS})
    assert v.is_valid(absent) and not v.is_valid(unknown)
    v = Draft202012Validator({"$ref": "#/$defs/Unknown", "$defs": C.DEFS})
    assert v.is_valid(unknown) and not v.is_valid(absent)
    assert not v.is_valid(None) and not Draft202012Validator({"$ref": "#/$defs/Field", "$defs": C.DEFS}).is_valid(None)


def test_a_bare_number_never_validates_as_a_field() -> None:
    field = Draft202012Validator({"$ref": "#/$defs/Field", "$defs": C.DEFS})
    assert not field.is_valid(820.0) and not field.is_valid(3)
    assert field.is_valid({"id": "c:cell:0000_0000:x", "value": 820.0}) and field.is_valid("a name") and field.is_valid(True)
    assert not field.is_valid([1, 2]) and field.is_valid([{"id": "c:a"}, "text"])
    row = Draft202012Validator(C.result_schema("cell_features", C.ROWS["cell_features"]))
    base = {"tool": "cell_features", "session_id": "s", "cell": "0000_0000", "note": "", "values": {}}
    assert not row.is_valid({**base, "rows": [{"feature": "x", "value": 820.0}]}), "a bare number in a row fails"
    assert row.is_valid({**base, "rows": [{"feature": "x", "value": {"id": "c:v", "value": 820.0}}]})
    assert row.is_valid({**base, "rows": [{"feature": "x", "value": C.unknown("not here")}]})


def test_rows_are_shaped_into_values_by_id_and_minted_where_the_tool_gave_none() -> None:
    cell = "0000_0000"
    values = {f"c:cell:{cell}:d": stat(f"c:cell:{cell}:d", 820.0, fmt="m1", unit="m"),
              f"c:cell:{cell}:s:nearest_m": stat(f"c:cell:{cell}:s:nearest_m", 4200.0, fmt="m1", unit="m")}
    rows = [
        {"feature": "d", "value": 820.0, "value_id": f"c:cell:{cell}:d", "unit": "m", "observations": 3, "is_effort": False},
        {"feature": "s", "value": None, "missing": "no observation; this is not a low value",
         "nearest_observation_id": f"c:cell:{cell}:s:nearest_m", "observations": 0, "unit": None},
    ]
    shaped = C.shape_rows("cell_features", rows, values, C.id_base("cell_features", cell, False))
    assert shaped[0]["value"] == values[f"c:cell:{cell}:d"], "a number beside its id becomes the value record"
    assert shaped[0]["observations"]["id"] == f"c:cell_features:{cell}:0:observations", "a number with no id is minted one"
    assert values[f"c:cell_features:{cell}:0:observations"]["value"] == 3, "and the minted id joins the registry"
    assert shaped[1]["value"]["state"] == "unknown" and shaped[1]["value"]["nearest_observation"]["value"] == 4200.0
    assert "unit" not in shaped[1], "a null that says nothing is not served"
    assert shaped[1]["observations"]["value"] == 0, "a zero observation count is a number like any other"
    assert numbers_outside_vals({"rows": shaped, "values": values}) == []
    schema = Draft202012Validator(C.result_schema("cell_features", C.ROWS["cell_features"]))
    assert schema.is_valid({"tool": "cell_features", "session_id": "s", "cell": cell, "note": "", "rows": shaped, "values": values})


def test_benchmark_ids_are_minted_in_the_bench_scheme() -> None:
    assert C.id_base("nearby", "b-0001", True) == "b:b-0001:nearby"
    assert C.id_base("nearby", "0000_0000", False) == "c:nearby:0000_0000"
    values: dict = {}
    shaped = C.shape_rows("label_context", [{"rank": 1, "tier": "occurrence", "distance_km": None}], values, "b:b-0001:label_context")
    assert shaped[0]["rank"]["id"] == "b:b-0001:label_context:0:rank" and shaped[0]["distance_km"]["state"] == "unknown"


def test_a_hole_id_is_a_field_and_not_a_value_reference() -> None:
    values: dict = {"d:f:off_1": stat("d:f:off_1", 36.2, fmt="m1", unit="m")}
    row = {"kind": "match", "file": "f", "hole_id": "KL101", "dataset": "geods", "name_score": 100.0,
           "offset_m": 36.2, "offset_m_id": "d:f:off_1", "bearing_deg": None,
           "differences": {"total_depth_m": -1.5, "dip_deg": None}}
    shaped = C.shape_rows("hole_crosscheck", [row], values, "c:hole_crosscheck:0000_0000")[0]
    assert shaped["hole_id"] == "KL101" and "hole" not in shaped
    assert shaped["offset_m"] == values["d:f:off_1"] and shaped["bearing_deg"]["state"] == "absent"
    assert shaped["differences"]["total_depth_m"]["value"] == -1.5 and shaped["differences"]["dip_deg"]["state"] == "unknown"
    assert shaped["name_score"]["id"] == "c:hole_crosscheck:0000_0000:0:name_score"
    assert Draft202012Validator(C.result_schema("hole_crosscheck", C.ROWS["hole_crosscheck"])).is_valid(
        {"tool": "hole_crosscheck", "session_id": "s", "cell": "0000_0000", "note": "", "rows": [shaped], "values": values})


def test_prose_puts_every_id_beside_its_number() -> None:
    values: dict = {}
    shaped = C.shape_rows("coverage", [{"feature": "d", "coverage": 0.91, "thin": False, "is_effort": False}], values, "c:coverage:x")
    text = C.prose("coverage", "0000_0000", "sid", "note here", shaped)
    assert "coverage 0.91 [c:coverage:x:0:coverage]" in text and "thin: no" in text and "Unknown is not absent" in text
    assert "(no rows)" in C.prose("coverage", "0000_0000", "sid", "", [])


@pytest.mark.parametrize("name", list(C.CATALOGUE))
def test_every_tool_description_says_what_it_is_for(name: str) -> None:
    spec = C.CATALOGUE[name]
    assert len(spec.description) > 40 and spec.tool().title == name.replace("_", " ")

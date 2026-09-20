"""The model-facing contract: the JSON schema the CLI is given, and the prompt version in every cache key."""

from __future__ import annotations

import json

import pytest

from uranium_explorer import wire
from uranium_explorer.prompts import (
    CarryItem,
    assert_clean,
    prompt_version,
    system_prompt,
    user_prompt,
)


def walk(node, path="$"):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


def test_schema_has_no_open_objects_and_requires_every_property():
    schema = wire.json_schema()
    for path, node in walk(schema):
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, f"{path} allows extra properties"
            assert set(node.get("properties", {})) == set(node.get("required", [])), \
                f"{path} does not require every property"


def test_schema_has_no_refs_defs_or_constraints():
    schema = wire.json_schema()
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    for banned in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "pattern"):
        assert f'"{banned}"' not in text, f"{banned} survived hardening"
    assert wire.audit(schema) == []


def test_nullable_fields_use_anyof_with_null():
    schema = wire.json_schema()
    cell = schema["properties"]["tables"]["items"]["properties"]["rows"]["items"]["properties"]["cells"]["items"]
    value = cell["properties"]["value_as_printed"]
    assert value["anyOf"] == [{"type": "string"}, {"type": "null"}]
    # a non-nullable enum stays a plain enum
    assert set(cell["properties"]["printed"]["enum"]) == {"printed", "not_printed", "illegible"}


def test_printed_and_illegible_are_reachable_answers():
    schema = json.dumps(wire.json_schema())
    for token in ("not_printed", "illegible", "prev_page_header", "printed_row_count", "row_quote",
                  "truncated", "unit_source"):
        assert token in schema


def test_harden_refuses_recursion():
    recursive = {"$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
                 "type": "object", "properties": {"root": {"$ref": "#/$defs/Node"}}}
    with pytest.raises(wire.SchemaError, match="recursive"):
        wire.harden(recursive)


def test_parse_rejects_a_missing_row_count():
    good = {
        "page_level": {
            "page_kind": "assay_table",
            **{k: {"value_as_printed": None, "unit_as_printed": None, "printed": "not_printed",
                   "source": "not_printed", "quote": None}
               for k in ("hole_id", "datum", "utm_zone", "depth_unit", "unit_notes", "species", "basis", "method")},
            "coordinate_kind": "not_printed",
        },
        "tables": [], "legibility_notes": None,
    }
    assert wire.parse(good).page_level.page_kind == "assay_table"
    bad = json.loads(json.dumps(good))
    bad["tables"] = [{"table_index": 0, "kind": "assay", "title_as_printed": None,
                      "column_headers_as_printed": [], "continued_from_prev": False,
                      "continues_on_next": False, "truncated": False, "rows": []}]
    with pytest.raises(Exception):
        wire.parse(bad)


def test_prompt_version_is_stable_and_covers_both_templates():
    assert prompt_version() == prompt_version()
    assert len(prompt_version()) == 12
    from uranium_explorer.ids import sha256_bytes, short
    from uranium_explorer.prompts import SYSTEM_TEMPLATE, USER_TEMPLATE, prompts_dir

    expected = short(sha256_bytes(((prompts_dir() / SYSTEM_TEMPLATE).read_text()
                                   + (prompts_dir() / USER_TEMPLATE).read_text()).encode()))
    assert prompt_version() == expected


def test_prompt_version_does_not_change_with_the_page():
    a = user_prompt(["assay_table"])
    b = user_prompt(["lith_log"], [CarryItem("hole identifier", "R-78-27", 4, "HOLE NO - R-78-27")])
    assert a != b
    assert prompt_version() == prompt_version()  # the version follows the template, not the rendering


def test_system_prompt_states_the_rules_the_eval_depends_on():
    s = system_prompt().lower()
    for rule in ("read tool", "printed_row_count", "not_printed", "illegible", "row_quote",
                 "no unit conversions", "ditto", "never infer"):
        assert rule in s, f"the system prompt does not state: {rule}"


def test_user_prompt_carries_the_stage_path_and_the_carry_text():
    carry = [CarryItem("hole identifier", "R-78-27", 4, "HOLE NO - R-78-27"),
             CarryItem("column headers", "FROM | TO | DESCRIPTION", 4, "FOOTAGE")]
    p = user_prompt(["lith_log", "assay_table"], carry)
    assert "{STAGE_DIR}/page.png" in p
    assert "lith_log, assay_table" in p
    assert "R-78-27" in p and "printed on page 4" in p
    assert "prev_page_header" in p


def test_user_prompt_never_carries_the_file_identity():
    p = user_prompt(["assay_table"])
    assert_clean(p, "74H09-0039")
    leaky = p + "\nThis is assessment file 74H09-0039 by Conwest."
    import pytest as _pytest

    with _pytest.raises(ValueError, match="leaks"):
        assert_clean(leaky, "74H09-0039")


def test_schema_version_tracks_the_schema():
    assert wire.SCHEMA_VERSION == wire.short(wire.sha256_json(wire.json_schema()))

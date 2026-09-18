"""The model-facing wire schema: what one `claude -p --json-schema` call returns for one page.

Two rules shape it:

1. **Transcription, not interpretation.** Every leaf that could be a number carries `value_as_printed`
   (the exact characters), a `quote` (verbatim printed text containing it) and a `printed` verdict
   (`printed` | `not_printed` | `illegible`). Units, datums, zones, species and bases are separate
   fields with their own `printed` flag and a `source` saying where on the page they were read.
2. **Countable omissions.** Each table reports `printed_row_count` (counted before transcribing) and a
   `truncated` flag, so a dropped row is a disagreement between two numbers instead of silence.

`json_schema()` post-processes pydantic's output for the CLI's `--json-schema`: no `$ref`/`$defs`
(inlined), `additionalProperties: false` on every object, every property required, nullability as
`anyOf [T, null]`, and no numeric/length/pattern constraints (the CLI's constrained decoder rejects
several of them, and a length cap would silently truncate a table).
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field

from .ids import sha256_json, short

WIRE_VERSION = "wire/v1"

# Printed verdict for any value. "not_printed" and "illegible" are expected answers, never failures.
Printed = Literal["printed", "not_printed", "illegible"]

# Where a unit / datum / zone / species / basis was read. "not_printed" means: nowhere on this page.
Source = Literal[
    "cell", "column_header", "table_title", "page_note", "prev_page_header", "not_printed"
]

PageKind = Literal[
    "collar_table", "lith_log", "assay_table", "probe_log", "certificate", "map", "text", "other"
]

TableKind = Literal["collar", "assay", "lith", "probe", "other"]

# The closed field vocabulary. assemble.py maps these onto Collar / LithInterval / AssayInterval.
CellField = Literal[
    "hole_id",
    "from_depth",
    "to_depth",
    "interval_width",
    "core_recovery",
    "sample_id",
    "sample_from_depth",
    "sample_to_depth",
    "sample_width",
    "sample_recovery",
    "description",
    "lith_code",
    "grade",
    "sulfides",
    "easting",
    "northing",
    "latitude",
    "longitude",
    "grid_x",
    "grid_y",
    "utm_zone",
    "datum",
    "elevation",
    "azimuth",
    "dip",
    "total_depth",
    "date",
    "other",
]


class PageField(BaseModel):
    """A single page-level statement (datum, zone, a unit note, species, basis, method)."""

    value_as_printed: str | None = Field(description="Exact printed characters, or null if not printed.")
    unit_as_printed: str | None = Field(description="Unit printed beside the value, or null.")
    printed: Printed = Field(description="printed, not_printed or illegible.")
    source: Source = Field(description="Where on the page this was read.")
    quote: str | None = Field(description="Verbatim printed text containing the value, or null.")


class Cell(BaseModel):
    """One transcribed cell of one row."""

    field: CellField = Field(description="Which field this cell holds.")
    value_as_printed: str | None = Field(
        description="Exact printed characters including qualifiers such as <, tr, nil. Null if the cell is blank."
    )
    unit_as_printed: str | None = Field(description="Unit as printed, or null if no unit is printed anywhere for it.")
    unit_source: Source = Field(description="Where the unit was read; not_printed if no unit is printed.")
    analyte_as_printed: str | None = Field(
        description="For a grade cell: the analyte exactly as printed in its column header (e.g. 'U ppm', 'U3O8 %')."
    )
    quote: str | None = Field(description="Verbatim printed text containing this value.")
    printed: Printed = Field(description="printed if the cell has characters, not_printed if blank, illegible if unreadable.")
    uncertain: bool = Field(description="True if you are unsure of any character in value_as_printed.")


class Row(BaseModel):
    row_index: int = Field(description="0-based index of this row within the table, top to bottom.")
    hole_id_as_printed: str | None = Field(description="Hole identifier printed in this row, or null.")
    row_quote: str | None = Field(
        description="Verbatim printed text of the whole row, containing every cell value of this row."
    )
    cells: list[Cell] = Field(description="Every cell of this row that holds a value or is a printed but blank cell.")


class Table(BaseModel):
    table_index: int = Field(description="0-based index of this table on the page.")
    kind: TableKind = Field(description="collar, assay, lith, probe or other.")
    title_as_printed: str | None = Field(description="Table title or column-group heading as printed, or null.")
    column_headers_as_printed: list[str] = Field(description="Column headers exactly as printed, left to right.")
    printed_row_count: int = Field(
        description="How many data rows are printed in this table. Count them before transcribing."
    )
    continued_from_prev: bool = Field(description="True if this table continues one from an earlier page.")
    continues_on_next: bool = Field(description="True if this table continues onto the next page.")
    truncated: bool = Field(description="True if you did not transcribe every printed row. Never drop rows silently.")
    rows: list[Row] = Field(description="The transcribed rows, in printed order.")


class PageLevel(BaseModel):
    page_kind: PageKind = Field(description="What this page is.")
    hole_id: PageField = Field(description="Hole identifier printed in the page header.")
    datum: PageField = Field(description="Geodetic datum, only if printed (e.g. 'NAD 27').")
    utm_zone: PageField = Field(description="UTM zone, only if printed.")
    coordinate_kind: Literal["utm", "geographic", "local_grid", "not_printed"] = Field(
        description="What kind of collar coordinates the page prints, if any."
    )
    depth_unit: PageField = Field(description="Unit the depths are printed in (feet, metres), only if printed.")
    unit_notes: PageField = Field(description="Any page note that states units for the whole page.")
    species: PageField = Field(description="Grade species as printed (U, U3O8, eU3O8), only if printed.")
    basis: PageField = Field(description="Whether grades are chemical assays or probe/radiometric equivalents, only if printed.")
    method: PageField = Field(description="Analytical or logging method as printed, only if printed.")


class PageResult(BaseModel):
    """The whole answer for one page."""

    page_level: PageLevel
    tables: list[Table] = Field(description="Every table of drillhole data printed on the page.")
    legibility_notes: str | None = Field(description="Anything printed you could not read, or null.")


# --------------------------------------------------------------------------- schema post-processing

_STRIP = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format",
    "minItems", "maxItems", "uniqueItems", "minContains", "maxContains",
    "minProperties", "maxProperties", "default", "examples", "title", "$comment",
    "propertyNames", "patternProperties", "dependentRequired", "readOnly", "writeOnly",
)


class SchemaError(ValueError):
    """The generated schema cannot be hardened (recursion, unresolvable reference)."""


def _inline(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    """Inline $ref, drop constraints, close every object. Raises on recursion."""
    if isinstance(node, list):
        return [_inline(n, defs, stack) for n in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        ref = node["$ref"]
        name = ref.rsplit("/", 1)[-1]
        if name in stack:
            raise SchemaError(f"recursive schema: {' -> '.join((*stack, name))}")
        if name not in defs:
            raise SchemaError(f"unresolvable reference {ref}")
        merged = {k: v for k, v in node.items() if k != "$ref"}
        return _inline({**copy.deepcopy(defs[name]), **merged}, defs, (*stack, name))

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _STRIP or key == "$defs":
            continue
        if key in ("properties", "$defs"):
            out[key] = {k: _inline(v, defs, stack) for k, v in value.items()}
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            out[key] = [_inline(v, defs, stack) for v in value]
        elif key in ("items", "additionalProperties", "not"):
            out[key] = _inline(value, defs, stack)
        else:
            out[key] = value

    # A single-member allOf is pydantic's way of attaching a description to a $ref: flatten it.
    if "allOf" in out and len(out["allOf"]) == 1:
        inner = out.pop("allOf")[0]
        out = {**inner, **out}

    # const is a one-member enum; the CLI decoder handles enum everywhere.
    if "const" in out:
        out["enum"] = [out.pop("const")]

    if out.get("type") == "object" or "properties" in out:
        props = out.setdefault("properties", {})
        out["additionalProperties"] = False
        out["required"] = list(props)
        out.setdefault("type", "object")
    return out


def harden(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a pydantic JSON schema safe for `claude -p --json-schema`."""
    defs = schema.get("$defs", {})
    hardened = _inline(copy.deepcopy(schema), defs, ())
    hardened.pop("$defs", None)
    hardened["$schema"] = "http://json-schema.org/draft-07/schema#"
    return hardened


def audit(schema: Any, path: str = "$") -> list[str]:
    """Problems a reviewer (or a test) should see: open objects, optional properties, leftover refs."""
    problems: list[str] = []
    if isinstance(schema, list):
        for i, n in enumerate(schema):
            problems += audit(n, f"{path}[{i}]")
        return problems
    if not isinstance(schema, dict):
        return problems
    if "$ref" in schema:
        problems.append(f"{path}: $ref survived hardening")
    if "$defs" in schema:
        problems.append(f"{path}: $defs survived hardening")
    for key in _STRIP:
        if key in schema and key != "title":
            problems.append(f"{path}: constraint {key} survived hardening")
    if schema.get("type") == "object" or "properties" in schema:
        if schema.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties is not false")
        props = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        if props != required:
            problems.append(f"{path}: not every property is required ({sorted(props - required)})")
    for key, value in schema.items():
        if key in ("properties",):
            for k, v in value.items():
                problems += audit(v, f"{path}.{k}")
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            problems += audit(value, f"{path}.{key}")
        elif key in ("items", "additionalProperties", "not") and isinstance(value, dict):
            problems += audit(value, f"{path}.{key}")
    return problems


def json_schema() -> dict[str, Any]:
    return harden(PageResult.model_json_schema())


WIRE_SCHEMA = json_schema()
SCHEMA_VERSION = short(sha256_json(WIRE_SCHEMA))


def parse(structured: dict[str, Any]) -> PageResult:
    """Validate a model answer. Raises pydantic ValidationError, which the scheduler treats as schema-invalid."""
    return PageResult.model_validate(structured)

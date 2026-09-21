"""The tool contract: the catalogue v1, and the shape every result takes.

Every number a tool returns reaches the client as a `Val` with an id, inside `structuredContent`; the
`content` text block is the prose a model may quote back. Unknown and absent are two object types,
`{"state": "unknown", "reason": ...}` and `{"state": "absent", "reason": ...}`, never a null, so a client
tells "nobody measured it" from "it was mapped and there is nothing there" in the schema rather than in prose.
Every tool's `outputSchema` declares the three types under `$defs`, and the SDK's client validates every
non-error result against it, so a bare number in a row fails at the client before a model reads it.

The rows are the tools' own (`prospect.tools`), reshaped: a number beside its `<key>_id` becomes the value
record the id names; a null in a measurement key becomes an Unknown carrying the reason the tool gave and,
where the tool gave one, the nearest observation; a number the tool returned without an id (a rank, a fold, a
zero observation count) is minted an id here and registered in the session like any other, because a number
the model can read is a number it may cite. The reshaping is a pure function over the payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import mcp.types as types

from ..analyst.session import PURPOSES
from ..prospect.tools import NEARBY_LAYERS, TOOL_HELP
from ..values import stat
from ..api.jobs import STATUSES as JOB_STATUSES
from .auth import SCOPES
from .sessions import ABSTAIN_REASONS

UNKNOWN, ABSENT = "unknown", "absent"

#: the read tools the session dispatches to `prospect.tools`, in catalogue order
REGISTRY_TOOLS: tuple[str, ...] = ("cell_features", "cell_scores", "criteria_breakdown", "label_context",
                                   "nearby", "coverage", "crosscheck", "retrieve")

# ---------------------------------------------------------------- the three types


def unknown(reason: str, nearest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Nobody measured it here: not a low value, not a zero."""
    out: dict[str, Any] = {"state": UNKNOWN, "reason": reason}
    if nearest is not None:
        out["nearest_observation"] = nearest
    return out


def absent(reason: str) -> dict[str, Any]:
    """Measured or mapped, and nothing there: a real answer."""
    return {"state": ABSENT, "reason": reason}


def is_val(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("id"), str)


#: per tool, the keys whose null means "not measured here", with the reason served when the row gives none
MEASURES: dict[str, dict[str, str]] = {
    "cell_features": {"value": "no observation here; this is not a low value"},
    "cell_scores": {"score": "too little is known here to score honestly",
                    "known_share": "no known share recorded"},
    "criteria_breakdown": {"membership": "no value for this criterion here: unknown, not absent"},
    "label_context": {"distance_km": "no distance computed"},
    "retrieve": {"distance_km": "the file has no position in the provincial index"},
    "nearby": {"reading": "no reading recorded for this feature", "nearest_m": "no distance computed"},
    "coverage": {"coverage": "coverage not computed for this feature"},
    "crosscheck": {"d_conductor_m": "no conductor observation; unknown, not far", "d_fault_m": "no fault observation; unknown, not far",
                   "sed_u_max_ppm": "no lake-sediment sample within reach; unknown, not low",
                   "sed_samples_n": "no sample count within reach", "min_sep_m": "no separation computed",
                   "thin_sampling": "no sample count within reach, so the thin-sampling rule cannot be applied"},
}
#: per tool, the nulls that mean "does not apply" rather than "not measured": an Absent
ABSENTS: dict[str, dict[str, str]] = {
    "retrieve": {"page": "the provincial index row has no page"},
    "hole_crosscheck": {"offset_m": "no position for this collar, so there is no offset to measure",
                        "bearing_deg": "no position for this collar, so there is no bearing to measure"},
}


def is_value_id(text: Any) -> bool:
    """A value id has a scheme prefix (`c:`, `b:`, `d:`, `p:`, `x:`); a hole id or a record id does not, so a
    `hole_id` key is a plain field and not a reference to the registry."""
    return isinstance(text, str) and ":" in text


def _fmt_for(value: float) -> str:
    return "int" if float(value).is_integer() else "m2"


def _mint(values: dict[str, dict[str, Any]], id_base: str, i: int, key: str, value: float, tool: str) -> dict[str, Any]:
    vid = f"{id_base}:{i}:{key}"
    if vid not in values:
        values[vid] = stat(vid, value, fmt=_fmt_for(value), note=f"{key} of row {i} from {tool}")
    return values[vid]


def _val(values: dict[str, dict[str, Any]], vid: str, value: float) -> dict[str, Any]:
    rec = values.get(vid)
    return rec if rec is not None else {"id": vid, "value": value}


def _shape_node(node: Any, values: dict[str, dict[str, Any]], id_base: str, i: int, key: str, tool: str) -> Any:
    """A nested value (a thresholds table, a differences table): numbers by id or minted, nulls unknown."""
    if isinstance(node, dict):
        if isinstance(node.get("value"), (int, float)) and not isinstance(node.get("value"), bool) \
                and isinstance(node.get("value_id"), str):
            return _val(values, node["value_id"], node["value"])
        return {k: _shape_node(v, values, id_base, i, f"{key}.{k}", tool) for k, v in node.items()
                if not k.endswith("_id")}
    if isinstance(node, list):
        return [_shape_node(v, values, id_base, i, f"{key}.{j}", tool) for j, v in enumerate(node)]
    if isinstance(node, bool) or isinstance(node, str):
        return node
    if isinstance(node, (int, float)):
        return _mint(values, id_base, i, key, node, tool)
    if node is None:
        return unknown(f"{key.rsplit('.', 1)[-1]} is not available")
    return str(node)


def _shape_row(tool: str, row: dict[str, Any], i: int, values: dict[str, dict[str, Any]], id_base: str) -> dict[str, Any]:
    ids = {k[:-3]: v for k, v in row.items() if k.endswith("_id") and is_value_id(v)}
    nearest = {k[: -len("_nearest_m_id")]: v for k, v in row.items()
               if k.endswith("_nearest_m_id") and is_value_id(v)}
    measures, absents = MEASURES.get(tool, {}), ABSENTS.get(tool, {})
    missing = row.get("missing") if isinstance(row.get("missing"), str) else None
    out: dict[str, Any] = {}
    for k, v in row.items():
        if (k.endswith("_id") and k[:-3] in ids) or k in ("missing", "absent"):
            continue
        if isinstance(v, bool) or isinstance(v, str):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = _val(values, ids[k], v) if k in ids else _mint(values, id_base, i, k, v, tool)
        elif v is None:
            if k in absents:
                out[k] = absent(absents[k])
            elif k in measures:
                near_id = nearest.get(k) or (ids.get("nearest_observation") if k == "value" else None)
                out[k] = unknown(missing or measures[k], values.get(near_id) if near_id else None)
            # any other null (a unit, a note) says nothing and is not served
        else:
            out[k] = _shape_node(v, values, id_base, i, k, tool)
    for base, vid in ids.items():
        # an id with no number beside it is a reference: retrieval's extracted value, a nearest observation
        if base not in row and base not in out and base != "nearest_observation" and not base.endswith("_nearest_m"):
            out[base] = values.get(vid) or {"id": vid}
    if isinstance(row.get("absent"), str):
        out["absent"] = absent(row["absent"])
    if tool == "criteria_breakdown" and "membership" not in out:
        note = row.get("note") if row.get("state") == UNKNOWN and isinstance(row.get("note"), str) else None
        out["membership"] = unknown(note or measures["membership"])
    if tool == "nearby":
        if "n_within" in row and "nearest_m" not in row:
            out["nearest_m"] = absent("no feature of this layer inside the radius")
        if "n_within" not in row and missing:
            out["reading"] = unknown(missing)
    return out


def shape_rows(tool: str, rows: list[dict[str, Any]], values: dict[str, dict[str, Any]], id_base: str) -> list[dict[str, Any]]:
    """The rows as the contract serves them. `values` gains every id minted on the way, so the caller registers
    the same dict in the session and the client receives it as the result's registry."""
    return [_shape_row(tool, row, i, values, id_base) for i, row in enumerate(rows)]


def id_base(tool: str, shown: str, benchmark: bool) -> str:
    """Where minted ids go: `c:<tool>:<cell>` or, in the id scheme a benchmark model sees, `b:<bench>:<tool>`."""
    return f"b:{shown}:{tool}" if benchmark else f"c:{tool}:{shown}"


# ---------------------------------------------------------------- prose


def _value_text(v: dict[str, Any]) -> str:
    value = v.get("value")
    if value is None:
        return "(reference)"
    text = f"{value:g}" if isinstance(value, float) else str(value)
    return f"{text} {v['unit']}" if v.get("unit") else text


def field_text(key: str, v: Any) -> str:
    if is_val(v):
        return f"{key} {_value_text(v)} [{v['id']}]"
    if isinstance(v, dict) and v.get("state") == UNKNOWN:
        near = v.get("nearest_observation")
        tail = f"; nearest observation {_value_text(near)} [{near['id']}]" if is_val(near) else ""
        return f"{key} unknown ({v.get('reason')}{tail})"
    if isinstance(v, dict) and v.get("state") == ABSENT:
        return f"{key} absent ({v.get('reason')})"
    if isinstance(v, dict):
        return f"{key}: " + ", ".join(field_text(k, x) for k, x in v.items())
    if isinstance(v, list):
        return f"{key}: " + ", ".join(field_text(str(j), x) for j, x in enumerate(v))
    if isinstance(v, bool):
        return f"{key}: {'yes' if v else 'no'}"
    return f"{key}: {v}"


def cited_ids(node: Any) -> set[str]:
    """Every value id a shaped row (or a whole result) points at."""
    out: set[str] = set()
    if is_val(node):
        out.add(node["id"])
    elif isinstance(node, dict):
        for v in node.values():
            out |= cited_ids(v)
    elif isinstance(node, list):
        for v in node:
            out |= cited_ids(v)
    return out


def prose(tool: str, shown: str, session_id: str, note: str, rows: list[dict[str, Any]],
          values: dict[str, dict[str, Any]] | None = None) -> str:
    """The text block: every number beside its id, so a model that reads only the text still has the ids. A
    value the tool registered without a row (the model metrics beside the scores) is listed after the rows,
    because a number the model can read is a number it may cite."""
    lines = [f"{tool} for cell {shown} (session {session_id})."]
    if note:
        lines.append(note)
    for i, row in enumerate(rows, start=1):
        lines.append(f"{i}. " + "; ".join(field_text(k, v) for k, v in row.items()))
    if not rows:
        lines.append("(no rows)")
    seen = cited_ids(rows)
    extra = [v for vid, v in (values or {}).items() if vid not in seen and is_val(v)]
    if extra:
        lines.append("Also in the registry: " + "; ".join(
            field_text(str(v.get("note") or v["id"]), v) for v in extra))
    lines.append("Every number above carries its value id in brackets: a claim must cite the id of each number it "
                 "states. Unknown is not absent.")
    return "\n".join(lines)


# ---------------------------------------------------------------- schemas

VAL_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["id"],
    "description": "a value the tools returned, named by its id; the number a claim may state by citing the id",
    "properties": {
        "id": {"type": "string"}, "value": {"type": ["number", "string", "null"]},
        "unit": {"type": ["string", "null"]}, "fmt": {"type": ["string", "null"]},
        "note": {"type": ["string", "null"]}, "kind": {"type": ["string", "null"]},
        "tier": {"type": ["string", "null"], "description": "expert when a geologist stated it (B19)"},
    },
    "additionalProperties": True,
}
UNKNOWN_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["state", "reason"],
    "description": "nobody measured this here: not a low value, not a zero",
    "properties": {"state": {"const": UNKNOWN}, "reason": {"type": "string"},
                   "nearest_observation": {"$ref": "#/$defs/Val"}},
    "additionalProperties": False,
}
ABSENT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["state", "reason"],
    "description": "measured or mapped, and nothing there: a real answer",
    "properties": {"state": {"const": ABSENT}, "reason": {"type": "string"}},
    "additionalProperties": False,
}
FIELD_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {"$ref": "#/$defs/Val"}, {"$ref": "#/$defs/Unknown"}, {"$ref": "#/$defs/Absent"},
        {"type": "string"}, {"type": "boolean"},
        {"type": "array", "items": {"$ref": "#/$defs/Field"}},
        {"type": "object", "additionalProperties": {"$ref": "#/$defs/Field"}},
    ],
}
DEFS: dict[str, Any] = {"Val": VAL_SCHEMA, "Unknown": UNKNOWN_SCHEMA, "Absent": ABSENT_SCHEMA, "Field": FIELD_SCHEMA}

S: dict[str, Any] = {"type": "string"}
B: dict[str, Any] = {"type": "boolean"}
V: dict[str, Any] = {"$ref": "#/$defs/Val"}
U: dict[str, Any] = {"$ref": "#/$defs/Unknown"}
A: dict[str, Any] = {"$ref": "#/$defs/Absent"}
CELL: dict[str, Any] = {"type": "string", "pattern": r"^\d{4}_\d{4}$", "description": "a grid cell id, like 0123_0045"}
SID: dict[str, Any] = {"type": "string", "minLength": 1, "description": "the handle open_session returned"}


def one_of(*refs: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": list(refs)}


def row_schema(**props: dict[str, Any]) -> dict[str, Any]:
    """A row: the named fields typed precisely, anything else a Field (never a bare number)."""
    return {"type": "object", "properties": props, "additionalProperties": {"$ref": "#/$defs/Field"}}


def result_schema(tool: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object", "required": ["tool", "session_id", "cell", "note", "rows", "values"],
        "properties": {
            "tool": {"const": tool}, "session_id": S, "cell": S, "note": S,
            "rows": {"type": "array", "items": row},
            "values": {"type": "object", "additionalProperties": V,
                       "description": "every value the rows cite, by id: the session registers the same ids"},
        },
        "additionalProperties": False, "$defs": DEFS,
    }


def plain_schema(required: list[str], **props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": props, "additionalProperties": False, "$defs": DEFS}


def input_schema(required: list[str], **props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": props, "additionalProperties": False}


ROWS: dict[str, dict[str, Any]] = {
    "cell_features": row_schema(feature=S, title=S, bears_on=S, is_effort=B, observations=V, value=one_of(V, U),
                                unit=S, text=S),
    "cell_scores": row_schema(model=S, in_area_of_applicability=B, score=one_of(V, U), known_share=one_of(V, U),
                              fold=V, out_of_fold=B, model_note=S),
    "criteria_breakdown": row_schema(criterion=S, title=S, element=S, status=S, weight=V, evidence=S, caveat=S,
                                     thresholds={"type": "object", "additionalProperties": V},
                                     state={"enum": ["met", "not met", UNKNOWN]}, membership=one_of(V, U), note=S),
    "label_context": row_schema(rank=V, tier=S, name=S, distance_km=one_of(V, U)),
    "nearby": row_schema(layer=S, is_effort=B, n_within=V, nearest_m=one_of(V, A, U), dist_m=V, reading=U, text=S),
    "coverage": row_schema(feature=S, coverage=one_of(V, U), thin=B, is_effort=B),
    "crosscheck": row_schema(pair=S, state={"enum": ["known", ABSENT, UNKNOWN]}, radius_m=V,
                             d_conductor_m=one_of(V, U), d_fault_m=one_of(V, U), crossings_n=V, min_sep_m=one_of(V, U),
                             absent=A, sed_u_max_ppm=one_of(V, U), sed_samples_n=one_of(V, U), min_samples=V,
                             thin_sampling=one_of(B, U)),
    "retrieve": row_schema(tier=S, citation=S, file=S, page=one_of(V, A), distance_km=one_of(V, U), text=S,
                           quotable=B, numbers_allowed=B, value=V, expert=B),
    "hole_crosscheck": row_schema(kind={"enum": ["summary", "match"]}, file=S, hole_id=S, dataset=S,
                                  provincial_name=S, name_match=S, name_score=V, offset_m=one_of(V, A),
                                  bearing_deg=one_of(V, A), offset_independent=B, position_source=S,
                                  datum_shift_signature=B, adjudication=S,
                                  differences={"type": "object", "additionalProperties": one_of(V, U)},
                                  matches_n=V, queue_n=V, signatures_n=V, computed_at=S),
}

CLAIMS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "object", "required": ["text", "value_ids"],
              "properties": {"text": S, "value_ids": {"type": "array", "items": S}},
              "additionalProperties": True},
}


# ---------------------------------------------------------------- the catalogue


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: str          # read | action | task
    scope: str
    description: str
    input: dict[str, Any]
    output: dict[str, Any]

    def tool(self) -> types.Tool:
        read = self.kind == "read"
        return types.Tool(
            name=self.name, title=self.name.replace("_", " "), description=self.description,
            inputSchema=self.input, outputSchema=self.output,
            annotations=types.ToolAnnotations(readOnlyHint=read, destructiveHint=False, idempotentHint=True,
                                              openWorldHint=False),
        )


def _read(name: str, description: str, required: list[str] | None = None, **props: dict[str, Any]) -> ToolSpec:
    return ToolSpec(name, "read", "read", description,
                    input_schema(["session_id", *(required or [])], session_id=SID, **props),
                    result_schema(name, ROWS[name]))


_IDS = " Every number is a value with an id; unknown and absent arrive as distinct types."

CATALOGUE: dict[str, ToolSpec] = {spec.name: spec for spec in (
    ToolSpec(
        "open_session", "action", "read",
        "Open a session over one cell and get the handle every other tool takes. Purpose dashboard shows the "
        "record as it is; scored and benchmark sessions serve out-of-fold scores only (B18), mask the cell's own "
        "label (B30) and blind-list the files within 10 km of it (B17); a benchmark session shows the cell under "
        "a bench id and refuses any real cell id. Idempotent by handle.",
        input_schema(["cell_id", "purpose"], cell_id=CELL, purpose={"enum": list(PURPOSES)},
                     fold={"type": "integer", "description": "the spatial fold whose out-of-fold scores a "
                           "blinded session serves; read from the store when the cell sits in exactly one"},
                     bench_id={"type": "string", "description": "the id a benchmark session shows the cell under"}),
        plain_schema(["session_id", "cell", "purpose", "fold", "blind_list_hash", "expires_at", "run_id", "tool_contract"],
                     session_id=S, cell=S, purpose={"enum": list(PURPOSES)}, fold=one_of(V, A),
                     blind_list_hash=S, expires_at=S, run_id=S, tool_contract=S),
    ),
    _read("cell_features", TOOL_HELP["cell_features"] + "." + _IDS, cell_id=CELL),
    _read("cell_scores", TOOL_HELP["cell_scores"] + ". Out-of-fold only in a scored or benchmark session." + _IDS,
          cell_id=CELL),
    _read("criteria_breakdown", TOOL_HELP["criteria_breakdown"] + "." + _IDS, cell_id=CELL),
    _read("label_context", TOOL_HELP["label_context"] + ". The evaluated cell's own label is masked in a scored "
          "or benchmark session." + _IDS, cell_id=CELL,
          radius_km={"type": "number", "minimum": 1, "maximum": 200, "default": 25.0}),
    _read("nearby", TOOL_HELP["nearby"] + "." + _IDS, ["layer"], cell_id=CELL, layer={"enum": list(NEARBY_LAYERS)},
          radius_m={"type": "number", "minimum": 500, "maximum": 20000, "default": 5000.0},
          k={"type": "integer", "minimum": 0, "maximum": 50, "default": 5}),
    _read("coverage", TOOL_HELP["coverage"] + "." + _IDS, feature_key=S),
    _read("crosscheck", TOOL_HELP["crosscheck"] + "." + _IDS, cell_id=CELL),
    ToolSpec(
        "hole_crosscheck", "read", "read",
        "The extraction crosscheck over hole positions: for the assessment files read in a cell (or one file, or "
        "one hole), each extracted collar against the province's two compilations: name match, offset and bearing "
        "with their ids, the datum-shift signature and whether the pair is queued for adjudication. Dashboard "
        "sessions only: it names files, holes and positions." + _IDS,
        input_schema(["session_id"], session_id=SID, cell_id=CELL, hole_id=S, file_num=S,
                     radius_km={"type": "number", "minimum": 0.5, "maximum": 25, "default": 2.0,
                                "description": "how far from the cell a file's holes may sit to count as the cell's"}),
        result_schema("hole_crosscheck", ROWS["hole_crosscheck"]),
    ),
    _read("retrieve", TOOL_HELP["retrieve"] + ". Blind-list enforced in a scored or benchmark session." + _IDS,
          ["query"], query={"type": "string", "minLength": 1}, cell_id=CELL,
          k={"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
          radius_km={"type": "number", "minimum": 1, "maximum": 500, "default": 40.0}),
    ToolSpec(
        "check_claims", "read", "read",
        "The gate as a callable: every number in a claim must cite, in value_ids, a value this session returned "
        "(or quote one the tools returned as text). Returns the problems and the ids that resolved, so a client "
        "checks itself before answering.",
        input_schema(["session_id", "claims"], session_id=SID, claims=CLAIMS_SCHEMA),
        plain_schema(["ok", "problems", "resolved", "unresolved", "session_id"], ok=B,
                     problems={"type": "array", "items": S}, resolved={"type": "array", "items": S},
                     unresolved={"type": "array", "items": S}, session_id=S),
    ),
    ToolSpec(
        "abstain", "action", "read",
        "Record that the question cannot be answered from the tools, and why: not_measured, outside_grid, "
        "no_value or out_of_scope. Refusal becomes measurable; the session's manifest carries it.",
        input_schema(["session_id", "reason"], session_id=SID, reason={"enum": list(ABSTAIN_REASONS)},
                     detail={"type": "string", "default": ""}),
        plain_schema(["abstain_id", "session_id", "reason", "detail", "recorded_at"], abstain_id=S, session_id=S,
                     reason={"enum": list(ABSTAIN_REASONS)}, detail=S, recorded_at=S),
    ),
    ToolSpec(
        "record_insight", "action", "record",
        "Record a geologist's insight about the cell in the store (expert.insight, the expert tier). Every "
        "number in the text is minted an expert-tier value id, so a claim that leans on it says so (B19). The "
        "client should confirm with the author before calling.",
        input_schema(["session_id", "text", "author"], session_id=SID, cell_id=CELL,
                     text={"type": "string", "minLength": 1, "maxLength": 4000},
                     author={"type": "string", "minLength": 1, "maxLength": 200}),
        plain_schema(["expert_id", "session_id", "cell", "author", "values", "value_ids", "recorded_at"],
                     expert_id=S, session_id=S, cell=S, author=S,
                     values={"type": "object", "additionalProperties": V},
                     value_ids={"type": "array", "items": S}, recorded_at=S),
    ),
    ToolSpec(
        "run_analyst", "task", "run",
        "Run the staged analyst over the session's cell as a background job: returns "
        "a job id at once; poll job_status until it is done, failed or cancelled. Dashboard sessions on an "
        "enabled cell only; the chain lands in the agent tier as `ue arm chain` publishes one, and the "
        "job's result names its chain id, verdict and cost. `config` is an arm name (default v1-openrouter); "
        "`budget_usd` is this job's ceiling (default 0.50, at most 2.00) inside the process's session budget.",
        input_schema(["session_id"], session_id=SID, cell_id=CELL,
                     config={"type": "string", "description": "the arm to run; a v1 arm under configs/arms/"},
                     budget_usd={"type": "number", "minimum": 0},
                     reason={"type": "string", "maxLength": 2000,
                             "description": "why the analyst is invoked; recorded on the job"},
                     expert_ids={"type": "array", "items": S,
                                 "description": "expert-tier ids the invocation rests on, recorded on the job"}),
        plain_schema(["job_id", "status", "session_id", "cell"], job_id=S, status={"enum": list(JOB_STATUSES)},
                     session_id=S, cell=S),
    ),
    ToolSpec(
        "job_status", "read", "read",
        "One background job's row: its status (queued, running, done, failed, cancelled), who asked, the "
        "stages that have run, and when done its result: for an analyst job the chain id, the verdict and "
        "the cost as a value with an id. A job that was running when the server restarted is failed with "
        "that reason.",
        input_schema(["session_id", "job_id"], session_id=SID, job_id={"type": "string", "minLength": 1}),
        plain_schema(["job_id", "kind", "status", "requested_by", "created_at", "started_at", "finished_at",
                      "progress", "result", "error", "run_id", "session_id"],
                     job_id=S, kind=S, cell=S, status={"enum": list(JOB_STATUSES)}, requested_by=S, created_at=S,
                     started_at=one_of(S, A), finished_at=one_of(S, A),
                     progress={"type": "array", "items": {"type": "object", "required": ["at", "event"],
                                                          "properties": {"at": S, "event": S, "detail": S},
                                                          "additionalProperties": False}},
                     result=one_of(A, {"type": "object", "required": ["chain_id", "verdict", "published", "cost_usd"],
                                       "properties": {"chain_id": S, "verdict": S, "published": B, "cost_usd": V,
                                                      "run_id": S, "arm": S, "reason": S},
                                       "additionalProperties": False}),
                     error=one_of(S, A), run_id=one_of(S, A), session_id=S),
    ),
)}

assert all(spec.scope in SCOPES for spec in CATALOGUE.values())

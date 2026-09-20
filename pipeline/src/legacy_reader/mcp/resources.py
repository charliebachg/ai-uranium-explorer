"""Resources hold data (PRD §E.3, principle 2): seven of them, each served from what already exists.

`lr://handbook` and `lr://criteria` are the two knowledge files the panel stages for every role;
`lr://cell/{id}/evidence` is the record the dashboard draws (`prospect.serve.evidence`); `lr://cell/{id}/chains`
the analyst chains stored for the cell, with the values they print through (`prospect.serve._chains`);
`lr://run/{id}/manifest` a run's manifest (`runtime.manifest.Manifest`, an MCP session's included);
`lr://readiness/gate` the five-column verdict `lr prospect gate` last wrote; `lr://reading/inventory` the
data inventory with every source's licence flag, which is what the public-safe build reads to decide what it
may serve.

A resource carries no session: the evidence record and the chains are the dashboard's unblinded view, so a
benchmark harness must not read them for the cell it is scoring (a session-scoped view is backlog).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import mcp.types as types
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from ..bench.pack import CELL_ID
from ..paths import PATHS
from ..prospect import inventory as INV
from ..prospect import serve as S
from ..prospect.memo import CRITERIA_FILE, HANDBOOK
from ..runtime.manifest import Manifest
from ..store import connect

#: the spec's code for a resource the server does not have
RESOURCE_NOT_FOUND = -32002

RESOURCES: list[types.Resource] = [
    types.Resource(uri="lr://handbook", name="handbook", title="The handbook",
                   description="What the public record supports about unconformity-related uranium in the basin, "
                               "and what it does not: the frame every role reads first.", mimeType="text/markdown"),
    types.Resource(uri="lr://criteria", name="criteria", title="The criteria table",
                   description="The targeting criteria with thresholds, weights, status (published, assumed, "
                               "folklore) and caveats.", mimeType="application/toml"),
    types.Resource(uri="lr://readiness/gate", name="readiness_gate", title="The data readiness gate",
                   description="The five-column verdict (present, licensed, covers, servable, versioned) per "
                               "dataset, as `lr prospect gate` last scored it.", mimeType="application/json"),
    types.Resource(uri="lr://reading/inventory", name="reading_inventory", title="The data inventory",
                   description="Every source the pipeline may use: tier, role, licence and whether it is "
                               "redistributable, verification date, and the recorded gaps.",
                   mimeType="application/json"),
]
TEMPLATES: list[types.ResourceTemplate] = [
    types.ResourceTemplate(uriTemplate="lr://cell/{id}/evidence", name="cell_evidence", title="A cell's evidence record",
                           description="Scores, features, criteria and label context with every value by id, plus "
                                       "the memos and chains stored for the cell: the dashboard's unblinded view.",
                           mimeType="application/json"),
    types.ResourceTemplate(uriTemplate="lr://cell/{id}/chains", name="cell_chains", title="A cell's analyst chains",
                           description="The staged analyst's stored chains for the cell, newest first, with the "
                                       "values they print through.", mimeType="application/json"),
    types.ResourceTemplate(uriTemplate="lr://run/{id}/manifest", name="run_manifest", title="A run manifest",
                           description="What a run was asked, against which store, with which prompts: an MCP "
                                       "session's run id names its own.", mimeType="application/json"),
]

_CELL_URI = re.compile(r"^lr://cell/([^/]+)/(evidence|chains)$")
_RUN_URI = re.compile(r"^lr://run/([^/]+)/manifest$")
_RUN_ID = re.compile(r"^[\w.-]+$")


def gate_path() -> Path:
    return PATHS.out / "prospect" / "gate.json"


def _not_found(uri: str, hint: str) -> MCPError:
    return MCPError(code=RESOURCE_NOT_FOUND, message=f"{uri}: {hint}")


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=str)


def inventory_document() -> dict[str, Any]:
    inv = INV.load()
    return {
        "summary": INV.summary(inv),
        "region_bbox": list(inv.region_bbox), "region_note": inv.region_note,
        "sources": [{
            "key": s.key, "title": s.title, "tier": s.tier, "role": s.role, "bears_on": s.bears_on,
            "access": s.access, "collection": s.collection, "verified": s.verified, "verified_at": s.verified_at,
            "record_count": s.record_count,
            "licence": {"key": s.licence.key, "name": s.licence.name, "url": s.licence.url,
                        "redistributable": s.licence.redistributable},
        } for s in inv.sources],
        "gaps": [asdict(g) for g in inv.gaps],
    }


def _chains(cell_id: str) -> dict[str, Any]:
    con = connect(read_only=True)
    try:
        pairs = S._chains(con, cell_id)
    finally:
        con.close()
    values: dict[str, Any] = {}
    for _record, vals in pairs:
        values |= vals
    return {"cell_id": cell_id, "chains": [record for record, _v in pairs], "values": values}


def read(uri: str, *, runs_dir: Path | None = None) -> types.TextResourceContents:
    """One resource, or an MCPError naming why not. Store reads are read-only."""
    runs = runs_dir or PATHS.runs
    if uri == "lr://handbook":
        return types.TextResourceContents(uri=uri, mimeType="text/markdown", text=HANDBOOK.read_text())
    if uri == "lr://criteria":
        return types.TextResourceContents(uri=uri, mimeType="application/toml", text=CRITERIA_FILE.read_text())
    if uri == "lr://readiness/gate":
        path = gate_path()
        if not path.is_file():
            raise _not_found(uri, "no gate verdict on disk; run `lr prospect gate` first")
        return types.TextResourceContents(uri=uri, mimeType="application/json", text=path.read_text())
    if uri == "lr://reading/inventory":
        return types.TextResourceContents(uri=uri, mimeType="application/json", text=_json(inventory_document()))
    m = _CELL_URI.match(uri)
    if m:
        cell_id, what = m.group(1), m.group(2)
        if not CELL_ID.fullmatch(cell_id):
            raise MCPError(code=INVALID_PARAMS, message=f"{uri}: a cell id looks like 0123_0045")
        doc = S.evidence(cell_id) if what == "evidence" else _chains(cell_id)
        return types.TextResourceContents(uri=uri, mimeType="application/json", text=_json(doc))
    m = _RUN_URI.match(uri)
    if m:
        run_id = m.group(1)
        if not _RUN_ID.fullmatch(run_id):
            raise MCPError(code=INVALID_PARAMS, message=f"{uri}: a run id has no path separators")
        try:
            manifest = Manifest.read(runs / run_id)
        except FileNotFoundError:
            raise _not_found(uri, f"no manifest under {runs / run_id}") from None
        return types.TextResourceContents(uri=uri, mimeType="application/json", text=_json(manifest.as_dict()))
    raise _not_found(uri, "not a resource this server has; see resources/list and resources/templates/list")


Reader = Callable[[str], types.TextResourceContents]

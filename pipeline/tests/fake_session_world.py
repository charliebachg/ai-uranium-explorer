"""A tool registry the session tests can see through.

Every fake records the arguments it received and, on purpose, ignores the leakage keywords the session passes:
`label_context` still returns the cell's own label at 0.0 km whatever `mask_cell` says, and `retrieve` still
returns a passage from a blind-listed file whatever `exclude_files` says. That is how the tests prove both
layers of each rule separately: the keyword reached the tool, and the session dropped the row anyway.

The rows carry everything a pack must not: company and property names, a hole name, a file number, a
coordinate and the cell id, in the same shapes the real tools use, so a leak through the session shows up as
a string in a written file.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from legacy_reader.prospect.tools import ToolResult
from legacy_reader.values import stat

#: the cell the tests open sessions on: a deposit in the synthetic store, with file 64L05-0060 placed on it
CELL = "0000_0000"
BENCH = "b-0001"
#: the file the store's blind list carries for CELL, and one far enough away to survive it
BLIND_FILE = "64L05-0060"
FAR_FILE = "MAW00509"
#: the served (every-label) score the fake `cell_scores` hands out, which no blinded session may ever see
SERVED_SCORE = 0.99


@dataclass
class FakeWorld:
    """What the fakes saw, and what they were allowed to do."""

    received: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    #: the served `cell_scores` raises unless a test says a dashboard may have it
    served_scores_allowed: bool = False


def fake_registry(world: FakeWorld) -> dict[str, Callable[..., ToolResult]]:
    """The tools, each closing over `world` so a test can read what it was asked."""

    def cell_features(cell_id: str) -> ToolResult:
        world.received["cell_features"].append({"cell_id": cell_id})
        cond, obs, holes = (f"c:cell:{cell_id}:d_conductor_m", f"c:cell:{cell_id}:d_conductor_m:n_obs",
                            f"c:cell:{cell_id}:holes_n")
        out = ToolResult("cell_features", {"cell_id": cell_id})
        out.rows = [
            {"feature": "d_conductor_m", "is_effort": False, "observations": 3, "observations_id": obs,
             "value": 820.0, "unit": "m", "value_id": cond, "note": f"nearest conductor to cell {cell_id}"},
            {"feature": "holes_n", "is_effort": True, "observations": 1, "value": 12, "unit": None, "value_id": holes},
        ]
        out.values = {
            cond: stat(cond, 820.0, fmt="m1", unit="m", note=f"d_conductor_m for cell {cell_id}"),
            obs: stat(obs, 3, note=f"observations behind d_conductor_m at cell {cell_id}"),
            holes: stat(holes, 12, note=f"holes_n for cell {cell_id}"),
        }
        out.note = "Effort features describe where people looked, not what is in the rock."
        return out

    def cell_scores(cell_id: str) -> ToolResult:
        world.received["cell_scores"].append({"cell_id": cell_id})
        if not world.served_scores_allowed:
            raise RuntimeError("the served cell_scores table was asked for; it saw every label")
        vid = f"c:score:{cell_id}:learned"
        out = ToolResult("cell_scores", {"cell_id": cell_id})
        out.rows = [{"model": "learned", "score": SERVED_SCORE, "score_id": vid, "in_area_of_applicability": True}]
        out.values = {vid: stat(vid, SERVED_SCORE, fmt="ratio3", note=f"learned score for cell {cell_id}")}
        out.note = "Fitted on every cell."
        return out

    def criteria_breakdown(cell_id: str) -> ToolResult:
        world.received["criteria_breakdown"].append({"cell_id": cell_id})
        mem, weight = f"c:crit:{cell_id}:conductor_proximity", f"c:crit:{cell_id}:conductor_proximity:weight"
        fmem, fweight, fhi = (f"c:crit:{cell_id}:fault_proximity", f"c:crit:{cell_id}:fault_proximity:weight",
                              f"c:crit:{cell_id}:fault_proximity:hi")
        out = ToolResult("criteria_breakdown", {"cell_id": cell_id})
        out.rows = [{"criterion": "conductor_proximity", "state": "met", "membership": 0.9, "membership_id": mem,
                     "weight": 3.0, "weight_id": weight, "status": "assumed",
                     "evidence": "Jefferson et al. 2007 on the McArthur River camp"},
                    # a second criterion, its threshold nested the way the live tool nests them
                    {"criterion": "fault_proximity", "state": "not met", "membership": 0.2, "membership_id": fmem,
                     "weight": 2.0, "weight_id": fweight, "status": "assumed",
                     "thresholds": {"hi": {"value": 3000.0, "value_id": fhi}},
                     "evidence": "Thomas et al. 2000 on fault corridors"}]
        out.values = {mem: stat(mem, 0.9, fmt="ratio3"), weight: stat(weight, 3.0, fmt="m2"),
                      fmem: stat(fmem, 0.2, fmt="ratio3"), fweight: stat(fweight, 2.0, fmt="m2"),
                      fhi: stat(fhi, 3000.0, fmt="m1", unit="m")}
        return out

    def label_context(cell_id: str, radius_km: float = 25.0, mask_cell: str | None = None) -> ToolResult:
        # the mask is recorded and then ignored: the session's own filter has to catch the 0.0 km row
        world.received["label_context"].append({"cell_id": cell_id, "radius_km": radius_km,
                                                **({"mask_cell": mask_cell} if mask_cell is not None else {})})
        own, near = f"c:near:{cell_id}:0", f"c:near:{cell_id}:1"
        args: dict[str, Any] = {"cell_id": cell_id, "radius_km": radius_km}
        if mask_cell:
            args["mask_cell"] = mask_cell
        out = ToolResult("label_context", args)
        out.rows = [
            {"rank": 1, "tier": "deposit", "name": "Cigar Lake", "distance_km": 0.0, "distance_km_id": own},
            {"rank": 2, "tier": "occurrence", "name": "Rabbit Lake", "distance_km": 12.4, "distance_km_id": near},
        ]
        out.values = {own: stat(own, 0.0, fmt="m2", unit="km", note=f"distance from cell {cell_id} to deposit Cigar Lake"),
                      near: stat(near, 12.4, fmt="m2", unit="km", note=f"distance from cell {cell_id} to occurrence Rabbit Lake")}
        return out

    def retrieve(query: str, cell_id: str | None = None, k: int = 6, radius_km: float = 40.0,
                 exclude_files: list[str] | None = None) -> ToolResult:
        # the exclusion is recorded and then ignored: the blind-listed passage comes back regardless
        world.received["retrieve"].append({"query": query, "cell_id": cell_id, "k": k, "radius_km": radius_km,
                                           **({"exclude_files": list(exclude_files)} if exclude_files is not None else {})})
        args: dict[str, Any] = {"query": query, "cell_id": cell_id, "k": k, "radius_km": radius_km}
        if exclude_files:
            args["exclude_files"] = list(exclude_files)
        expert = f"c:expert:{cell_id}:1"
        out = ToolResult("retrieve", args)
        out.rows = [
            {"tier": "page", "citation": f"{BLIND_FILE} p.12", "file": BLIND_FILE, "page": 12, "distance_km": 0.4,
             "text": "Drilling by Asamera on the Cluff Lake property tested the graphitic conductor; hole Q6-1 "
                     "intersected pitchblende with clay alteration near 58.3510 N.",
             "page_id": "c:pass:0:page", "distance_km_id": "c:pass:0:km"},
            {"tier": "page", "citation": f"{FAR_FILE} p.7", "file": FAR_FILE, "page": 7, "distance_km": 31.2,
             "text": "Cameco's drilling at McArthur River followed the P2 conductor along the unconformity; hole "
                     "MC-361 returned mineralization with strong alteration.",
             "page_id": "c:pass:1:page", "distance_km_id": "c:pass:1:km"},
            {"tier": "expert", "text": "A geologist notes the conductor is offset 3 times along strike.",
             "value_id": expert},
        ]
        out.values = {
            "c:pass:0:page": stat("c:pass:0:page", 12, note=f"page of file {BLIND_FILE} this passage is on"),
            "c:pass:0:km": stat("c:pass:0:km", 0.4, fmt="m2", unit="km"),
            "c:pass:1:page": stat("c:pass:1:page", 7, note=f"page of file {FAR_FILE} this passage is on"),
            "c:pass:1:km": stat("c:pass:1:km", 31.2, fmt="m2", unit="km"),
            expert: {**stat(expert, 3, note="offsets counted by the geologist"), "tier": "expert"},
        }
        return out

    def nearby(cell_id: str, layer: str, radius_m: float = 5000.0, k: int = 5) -> ToolResult:
        world.received["nearby"].append({"cell_id": cell_id, "layer": layer, "radius_m": radius_m, "k": k})
        vid = f"c:nearby:{cell_id}:{layer}:count"
        out = ToolResult("nearby", {"cell_id": cell_id, "layer": layer, "radius_m": radius_m, "k": k})
        out.rows = [{"layer": layer, "count": 4, "count_id": vid, "is_effort": layer == "compilation"}]
        out.values = {vid: stat(vid, 4, note=f"{layer} features within {radius_m:g} m of cell {cell_id}")}
        return out

    return {"cell_features": cell_features, "cell_scores": cell_scores, "criteria_breakdown": criteria_breakdown,
            "label_context": label_context, "retrieve": retrieve, "nearby": nearby}

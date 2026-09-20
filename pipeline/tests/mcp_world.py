"""The MCP tests' world: the synthetic store, the session tests' fakes with the two spatial tools added, a
crosscheck output on disk, and a server the SDK's in-memory client connects to. No network, no model, and
nothing here touches the live store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from legacy_reader import store as ST
from legacy_reader.mcp import holes as HOLES
from legacy_reader.mcp.server import LrServer
from legacy_reader.prospect.tools import ToolResult
from legacy_reader.values import stat

from bench_store import bench_frame, make_bench_store
from fake_session_world import CELL, FakeWorld, fake_registry

FOLD = 2
#: a second cell of the synthetic store, for the dashboard's compare and the blinded session's refusal
OTHER = "0000_0001"
#: the file the crosscheck fixture names, sitting on CELL, and the hole it matched
CX_FILE = "74H09-0039"
CX_HOLE = "KL101"


def synthetic_store(sandbox: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The synthetic store at the sandbox's path, with out-of-fold scores for CELL: two models in fold 2 and
    a stale row in fold 1, exactly as the session tests build it."""
    db = sandbox.db
    make_bench_store(db, bench_frame(n_dep=4, n_occ=4, n_neg=4, n_probe=4))
    monkeypatch.setattr(ST, "db_path", lambda: db)
    con = ST.connect(db)
    try:
        for model, fold, score in (("learned", FOLD, 0.61), ("effort", FOLD, 0.83), ("criteria", 1, 0.55)):
            con.execute("insert into derived.cell_score_oof values (?, ?, 'spatial', ?, ?, 'run-1', 'now', 'derived')",
                        [CELL, model, fold, score])
    finally:
        con.close()
    return db


def registry(world: FakeWorld) -> dict[str, Callable[..., ToolResult]]:
    """The session tests' fakes, plus `coverage` and `crosscheck`, and a `cell_features` that also returns an
    unmeasured feature with a nearest observation, so the contract's unknown shape has something to shape."""
    fakes = fake_registry(world)
    base_features = fakes["cell_features"]

    def cell_features(cell_id: str) -> ToolResult:
        out = base_features(cell_id)
        near = f"c:cell:{cell_id}:sed_u_max_ppm:nearest_m"
        out.rows.append({"feature": "sed_u_max_ppm", "is_effort": False, "observations": 0, "value": None,
                         "unit": None, "missing": "no observation; this is not a low value",
                         "nearest_observation_id": near})
        out.values[near] = stat(near, 4200.0, fmt="m1", unit="m", note="distance to the nearest sed_u_max_ppm observation")
        return out

    def coverage(feature_key: str | None = None) -> ToolResult:
        world.received["coverage"].append({"feature_key": feature_key})
        out = ToolResult("coverage", {"feature_key": feature_key})
        for key, share, thin in (("d_conductor_m", 0.91, False), ("sed_u_max_ppm", 0.12, True)):
            if feature_key and key != feature_key:
                continue
            vid = f"c:cov:{key}"
            out.values[vid] = stat(vid, share, fmt="ratio3", note=f"share of cells with an observation behind {key}")
            out.rows.append({"feature": key, "coverage_id": vid, "coverage": share, "thin": thin, "is_effort": False})
        out.note = "A thin feature cannot carry a basin-wide argument on its own."
        return out

    def crosscheck(cell_id: str) -> ToolResult:
        world.received["crosscheck"].append({"cell_id": cell_id})
        out = ToolResult("crosscheck", {"cell_id": cell_id})
        radius, crossings, mins = (f"c:x:{cell_id}:conductor_fault:radius_m", f"c:x:{cell_id}:conductor_fault:crossings_n",
                                   f"c:x:{cell_id}:sediment_sampling:min_samples")
        out.values |= {radius: stat(radius, 5000.0, fmt="m1", unit="m"), crossings: stat(crossings, 0),
                       mins: stat(mins, 3)}
        out.rows = [
            {"pair": "conductor_fault", "radius_m": 5000.0, "radius_m_id": radius, "d_conductor_m": 820.0,
             "d_conductor_m_id": f"c:cell:{cell_id}:d_conductor_m", "d_fault_m": None, "crossings_n": 0,
             "crossings_n_id": crossings, "state": "absent", "absent": "no mapped fault within the radius"},
            {"pair": "sediment_sampling", "sed_u_max_ppm": None, "sed_samples_n": None, "min_samples": 3,
             "min_samples_id": mins, "thin_sampling": None, "state": "unknown",
             "missing": "no lake-sediment sample within reach; unknown, not low"},
        ]
        out.values[f"c:cell:{cell_id}:d_conductor_m"] = stat(f"c:cell:{cell_id}:d_conductor_m", 820.0, fmt="m1", unit="m")
        out.note = "Unknown and absent are different answers."
        return out

    return {**fakes, "cell_features": cell_features, "coverage": coverage, "crosscheck": crosscheck}


def crosscheck_fixture(root: Path, monkeypatch: pytest.MonkeyPatch, lonlat: tuple[float, float]) -> None:
    """One crosscheck output and its positions file under `root`, for the hole crosscheck: a hole matched in
    both compilations with an offset and a bearing that carry ids, and one with no position."""
    root.mkdir(parents=True, exist_ok=True)
    values = {
        f"d:{CX_FILE}:off_ab12cd34": {"id": f"d:{CX_FILE}:off_ab12cd34", "kind": "derived", "value": 36.2, "fmt": "m1",
                                      "unit": "m", "derivation": {"op": "geodesic_offset", "inputs": [], "tool": "proj"},
                                      "note": "distance from this extracted collar to the geods record for the same hole"},
        f"d:{CX_FILE}:brg_ab12cd34": {"id": f"d:{CX_FILE}:brg_ab12cd34", "kind": "derived", "value": 289.0, "fmt": "deg1",
                                      "unit": "deg", "derivation": {"op": "geodesic_offset", "inputs": [], "tool": "proj"}},
    }
    doc = {
        "version": "crosscheck/v1", "file_num": CX_FILE, "computed_at": "2026-09-20T00:00:00+00:00",
        "matches": [
            {"hole_id": CX_HOLE, "dataset": "geods", "feature_id": 1, "provincial_name": "KL-101", "lonlat": list(lonlat),
             "offset_m": f"d:{CX_FILE}:off_ab12cd34", "bearing_deg": f"d:{CX_FILE}:brg_ab12cd34", "offset_m_value": 36.2,
             "name_match": "normalised", "name_score": 100.0, "offset_independent": True,
             "position_source": "extracted_utm", "datum_shift_signature": True,
             "differences": {"total_depth_m": -1.5, "dip_deg": None, "azimuth_deg": 0.0}, "adjudication": "needed"},
            {"hole_id": "KL102", "dataset": "compilation", "feature_id": 2, "provincial_name": "KL-102", "lonlat": None,
             "offset_m": None, "bearing_deg": None, "offset_m_value": None, "name_match": "exact", "name_score": 100.0,
             "offset_independent": False, "position_source": None, "datum_shift_signature": False,
             "differences": {"total_depth_m": None, "dip_deg": None, "azimuth_deg": None}, "adjudication": "none"},
        ],
        "values": values,
        "adjudication_queue": [{"reason": "offset 36.2 m is over 100.0 m", "hole_id": CX_HOLE}],
        "provincial_lith": {}, "provincial_lith_values": {},
    }
    (root / f"{CX_FILE}.json").write_text(json.dumps(doc))
    positions = {CX_FILE: {"holes": [{"hole_id": CX_HOLE, "lonlat": list(lonlat)}, {"hole_id": "KL102", "lonlat": None}]}}
    monkeypatch.setattr(HOLES, "crosscheck_path", lambda f: root / f"{f}.json")
    monkeypatch.setattr(HOLES, "read_crosscheck", lambda f: json.loads((root / f"{f}.json").read_text()) if (root / f"{f}.json").is_file() else {})
    monkeypatch.setattr(HOLES, "read_positions", lambda f: positions.get(f, {}))


def make_server(tmp_path: Path, world: FakeWorld, *, environ: dict[str, str] | None = None,
                public_safe: bool = False, clock: Any = None, insight_store: Any = None, jobs: Any = None) -> LrServer:
    """A server over the fakes, its runs under the test's directory, with no key register unless given,
    `record_insight` writing through `insight_store` (a temporary store's connection) when a test needs it,
    and `run_analyst` submitting to `jobs` (a runner over a fake kind) when a test hands one in."""
    from legacy_reader.mcp.handlers import Handlers
    from legacy_reader.mcp.sessions import SessionStore

    store = SessionStore(runs_dir=tmp_path / "runs", tools=registry(world), **({"clock": clock} if clock else {}))
    handlers = Handlers(store, public_safe=public_safe, insight_store=insight_store, jobs=jobs)
    return LrServer(environ=environ if environ is not None else {}, public_safe=public_safe,
                    runs_dir=tmp_path / "runs", store=store, handlers=handlers)


def numbers_outside_vals(node: Any, path: str = "$") -> list[str]:
    """Every bare number in a structured result that is not the `value` of a Val: the contract says none."""
    out: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("id"), str):
            return []  # a Val: its number is the one a claim may cite by id
        for k, v in node.items():
            out += numbers_outside_vals(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += numbers_outside_vals(v, f"{path}[{i}]")
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.append(f"{path} = {node}")
    return out


__all__ = ["CELL", "CX_FILE", "CX_HOLE", "FOLD", "OTHER", "FakeWorld", "crosscheck_fixture", "make_server",
           "numbers_outside_vals", "registry", "synthetic_store"]

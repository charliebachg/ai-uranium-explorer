"""`hole_crosscheck`: the extraction crosscheck (`legacy_reader.crosscheck`) served as a tool.

`lr crosscheck` writes one file per assessment report under `data/out/crosscheck/<file>.json`: every extracted
collar matched against the province's two compilations, with the offset and bearing as derived values that
already carry ids (`d:<file>:off_…`, `d:<file>:brg_…`), the datum-shift signature and the adjudication queue.
This module reads those files and reshapes them into rows; it computes nothing new. The one decision it makes
is which files belong to a cell: a file counts when any of its holes (the extracted or provincially placed
position the crs stage recorded) sits within `radius_km` of the cell centre, because the provincial index
places a file by one point and a drilling programme is wider than that.

Dashboard sessions only: a row names a file, a hole and where it is, which is exactly what a blinded session
scrubs. The server refuses it under any other purpose before this module is reached.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..crosscheck import crosscheck_path, read_crosscheck
from ..normalise import normalise_hole_name
from ..position import read_positions

DEFAULT_RADIUS_KM = 2.0
RADIUS_KM = (0.5, 25.0)


class HoleError(Exception):
    """A request the crosscheck outputs cannot answer, with the reason a client can act on."""


def crosscheck_dir() -> Path:
    return crosscheck_path("x").parent


def crosschecked_files() -> list[str]:
    """Every file `lr crosscheck` has written, sorted so the rows come out in one order."""
    d = crosscheck_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def _km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance, the same formula `label_context` uses in SQL, so the two agree."""
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def hole_positions(file_num: str, doc: dict[str, Any]) -> list[list[float]]:
    """Where a file's holes are: the crs stage's position per hole, else the provincial record a match names."""
    out: list[list[float]] = []
    for hole in read_positions(file_num).get("holes", []):
        if isinstance(hole.get("lonlat"), list) and len(hole["lonlat"]) == 2:
            out.append([float(hole["lonlat"][0]), float(hole["lonlat"][1])])
    for m in doc.get("matches", []):
        if isinstance(m.get("lonlat"), list) and len(m["lonlat"]) == 2:
            out.append([float(m["lonlat"][0]), float(m["lonlat"][1])])
    return out


def files_near(centre: tuple[float, float], radius_km: float) -> list[tuple[str, dict[str, Any]]]:
    """The crosschecked files with a hole within `radius_km` of `centre` (lon, lat), nearest hole first."""
    hits: list[tuple[float, str, dict[str, Any]]] = []
    for file_num in crosschecked_files():
        doc = read_crosscheck(file_num)
        near = [_km(centre[0], centre[1], lon, lat) for lon, lat in hole_positions(file_num, doc)]
        best = min(near) if near else None
        if best is not None and best <= radius_km:
            hits.append((best, file_num, doc))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [(f, d) for _, f, d in hits]


def _same_hole(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a == b or normalise_hole_name(a) == normalise_hole_name(b)


def rows_for(file_num: str, doc: dict[str, Any], hole_id: str | None = None
             ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """One summary row for the file and one row per provincial match, with the crosscheck's own values.

    Every `*_id` key that names a value id is picked up by the contract's row shaping; `hole_id` is a plain
    field. A match with no position has no offset: that is absent, not unknown."""
    matches = [m for m in doc.get("matches", [])
               if hole_id is None or _same_hole(hole_id, m.get("hole_id")) or _same_hole(hole_id, m.get("provincial_name"))]
    values: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = [{
        "kind": "summary", "file": file_num,
        "matches_n": len(doc.get("matches", [])), "queue_n": len(doc.get("adjudication_queue", [])),
        "signatures_n": sum(1 for m in doc.get("matches", []) if m.get("datum_shift_signature")),
        "computed_at": str(doc.get("computed_at") or ""),
    }]
    for m in matches:
        row: dict[str, Any] = {
            "kind": "match", "file": file_num, "hole_id": str(m.get("hole_id") or ""), "dataset": str(m.get("dataset") or ""),
            "provincial_name": m.get("provincial_name"), "name_match": str(m.get("name_match") or ""),
            "name_score": float(m.get("name_score") or 0.0),
            "offset_independent": bool(m.get("offset_independent")), "position_source": m.get("position_source"),
            "datum_shift_signature": bool(m.get("datum_shift_signature")),
            "adjudication": str(m.get("adjudication") or "none"),
            "differences": {k: (float(v) if isinstance(v, (int, float)) else None)
                            for k, v in (m.get("differences") or {}).items()},
        }
        for key in ("offset_m", "bearing_deg"):
            vid = m.get(key)
            val = doc.get("values", {}).get(vid) if isinstance(vid, str) else None
            if val is not None and isinstance(val.get("value"), (int, float)):
                values[vid] = val
                row[key], row[f"{key}_id"] = val["value"], vid
            else:
                row[key] = None
        rows.append(row)
    return rows, values


def hole_crosscheck(*, centre: tuple[float, float] | None, cell_shown: str, hole_id: str | None = None,
                    file_num: str | None = None, radius_km: float = DEFAULT_RADIUS_KM
                    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    """The rows, the values and the note for one request: one file, one hole, or the cell's files."""
    lo, hi = RADIUS_KM
    if not lo <= float(radius_km) <= hi:
        raise HoleError(f"radius_km must be between {lo:g} and {hi:g}, not {radius_km}")
    if file_num:
        if not crosscheck_path(file_num).is_file():
            raise HoleError(f"no crosscheck for file {file_num}: it has not been read, or `lr crosscheck --files "
                            f"{file_num}` has not run")
        chosen = [(file_num, read_crosscheck(file_num))]
    elif hole_id:
        chosen = [(f, d) for f in crosschecked_files() for d in [read_crosscheck(f)]
                  if any(_same_hole(hole_id, m.get("hole_id")) or _same_hole(hole_id, m.get("provincial_name"))
                         for m in d.get("matches", []))]
        if not chosen:
            raise HoleError(f"no crosschecked file matches a hole named {hole_id!r}; the crosscheck covers "
                            f"{len(crosschecked_files())} file(s)")
    else:
        if centre is None:
            raise HoleError(f"cell {cell_shown} has no centre in the store")
        chosen = files_near(centre, float(radius_km))
    rows: list[dict[str, Any]] = []
    values: dict[str, dict[str, Any]] = {}
    for f, doc in chosen:
        r, v = rows_for(f, doc, hole_id)
        rows += r
        values |= v
    what = (f"file {file_num}" if file_num else f"hole {hole_id}" if hole_id
            else f"the files with a hole within {float(radius_km):g} km of cell {cell_shown}")
    note = (f"Extraction crosscheck for {what}: {len(chosen)} file(s), {sum(1 for r in rows if r['kind'] == 'match')} "
            "provincial match(es). An offset is the geodesic distance from the collar as read off the page to the "
            "provincial record for the same hole; where the collar was placed by a provincial name match the "
            "offset is zero by construction and says nothing about the reading (offset_independent false). A "
            "datum-shift signature means the offset matches the local NAD27 to NAD83 shift in size and direction. "
            "Both provincial layers are compilations with their own transcription history: a disagreement is a "
            "question for the adjudication queue, not a verdict.")
    if not chosen:
        note = f"No crosschecked file for {what}; the crosscheck covers {len(crosschecked_files())} file(s)."
    return rows, values, note


def as_json(rows: list[dict[str, Any]], values: dict[str, dict[str, Any]], note: str) -> dict[str, Any]:
    return {"tool": "hole_crosscheck", "note": note, "rows": rows, "values": values}


__all__ = ["DEFAULT_RADIUS_KM", "HoleError", "as_json", "crosschecked_files", "files_near", "hole_crosscheck",
           "rows_for"]

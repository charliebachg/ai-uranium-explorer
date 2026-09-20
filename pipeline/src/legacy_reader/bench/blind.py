"""Blind-lists and passages: what retrieval must not return for a cell, and what it returns once it cannot.

A benchmark cell near a known deposit has assessment reports about it in the corpus, and a retrieval step
that handed those back would be answering the question by looking it up. So every cell carries a blind-list:
the files whose reported holes sit within a radius of it, plus any file this pipeline has read whose collars
fall inside. Retrieval runs with those excluded, and the passages that survive are scrubbed of company,
property, file and hole names before they are frozen. For most cells the list of passages is empty, which is
the honest answer: nothing was written about that ground.

Distances are geodesic on the WGS84 ellipsoid, not a haversine on a sphere, because the radius is a rule and
a rule should not have a percent of slack in it.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pyproj

from ..prospect import retrieve as R
from .pack import forbidden_strings, hole_names, scrub_text
from .spec import BenchSpec

_GEOD = pyproj.Geod(ellps="WGS84")


def distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Geodesic distance on WGS84, in kilometres."""
    return _GEOD.inv(lon1, lat1, lon2, lat2)[2] / 1000.0


def cell_position(cell_id: str, con: Any) -> tuple[float, float]:
    row = con.execute("select lon, lat from derived.cell where cell_id = ?", [cell_id]).fetchone()
    if not row:
        raise KeyError(f"no cell {cell_id!r}")
    return float(row[0]), float(row[1])


def blind_list(cell_id: str, radius_km: float, con: Any) -> list[str]:
    """File numbers within `radius_km` of the cell: every `native.corpus_file` row placed inside it, and any
    `read.report` file with a read collar inside it. Sorted, so the list is stable."""
    lon, lat = cell_position(cell_id, con)
    out: set[str] = set()
    for file_num, flon, flat in con.execute(
            "select file_num, lon, lat from native.corpus_file where lon is not null and lat is not null").fetchall():
        if distance_km(lon, lat, float(flon), float(flat)) <= radius_km:
            out.add(str(file_num))
    try:
        collars = con.execute(
            "select distinct r.file_num, p.lon, p.lat from read.report r join derived.collar_position p using (file_num) "
            "where p.lon is not null and p.lat is not null").fetchall()
    except duckdb.Error:  # a store that has read nothing has neither table
        collars = []
    for file_num, flon, flat in collars:
        if distance_km(lon, lat, float(flon), float(flat)) <= radius_km:
            out.add(str(file_num))
    return sorted(out)


def passages(cell_id: str, spec: BenchSpec, exclude: list[str], con: Any,
             forbidden: set[str] | None = None) -> list[dict[str, Any]]:
    """Retrieved passages about the cell's surroundings with the blind-list excluded, scrubbed and stripped of
    everything that names a file. Empty for most cells."""
    lon, lat = cell_position(cell_id, con)
    # retrieval ranks the whole corpus when nothing is in range, which is right for a question about the
    # basin and would hand a benchmark cell passages about another district; the spatial cut is made here first
    if not set(R.files_near(lon, lat, spec.retrieval.radius_km)) - set(exclude):
        return []
    hits = R.retrieve(spec.retrieval.query, lon=lon, lat=lat, radius_km=spec.retrieval.radius_km,
                      k=spec.retrieval.k, exclude_files=exclude)
    names = set(forbidden) if forbidden is not None else forbidden_strings(con)
    names |= hole_names(con)
    names |= set(exclude)
    out: list[dict[str, Any]] = []
    for i, p in enumerate(hits):
        out.append({
            "passage_id": f"p-{i + 1:02d}", "tier": p.tier, "page": p.page, "distance_km": p.distance_km,
            "quotable": p.quotable, "numbers_allowed": p.carries_numbers, "text": scrub_text(p.text, names),
        })
    return out

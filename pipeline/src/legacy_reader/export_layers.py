"""Put the evidence layers on the map, simplified enough to ship.

The layers the criteria are computed from — conductors, faults, the mapped host, lake geochemistry, boulders,
survey footprints — were pulled, scored against, and then never drawn. That is the wrong way round: a score a
reader cannot see the inputs of is a number to take on trust, which is the one thing this project will not ask
for. So each one becomes a map layer, grouped the way MineTRACE groups its evidence.

Three rules hold here:

* **Only what the licence allows.** A source marked non-redistributable in the register is never written into
  `web/public`, however useful it would look.
* **Geometry is simplified, values are not.** Lines and polygons are thinned with a tolerance stated per layer
  and coordinates are rounded to five decimals (about a metre at this latitude); the properties that carry a
  measurement are copied across untouched. A simplified outline is still an honest outline. A rounded assay is
  a different number.
* **Nothing is invented to fill a hole.** The three datasets this project does not have — magnetics, gravity,
  radiometrics — get no layer. They are carried as named gaps in the register and the rail says so, because a
  missing layer a reader can see is worth more than a smooth map that quietly leaves it out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .paths import PATHS
from .prospect.features import GRAPHITIC_WORDS
from .prospect.inventory import load as load_inventory

#: five decimals is about a metre here; enough for a 2 km cell, and it roughly halves the file
COORD_DP = 5


@dataclass(frozen=True)
class LayerExport:
    """One source layer, thinned for the browser."""

    key: str                              # the register key, so the licence can be checked
    out: str                              # filename under web/public/data/context
    #: the file under data/index, when it is not named after the register key
    source_file: str = ""
    keep: tuple[str, ...] = ()            # properties copied across, verbatim
    rename: dict[str, str] = field(default_factory=dict)
    simplify_deg: float = 0.0             # 0 keeps every vertex
    where: Callable[[dict[str, Any]], bool] | None = None


def _graphitic(props: dict[str, Any]) -> bool:
    text = str(props.get("LITHOLOGY") or "").lower()
    return any(word in text for word in GRAPHITIC_WORDS)


EXPORTS: tuple[LayerExport, ...] = (
    LayerExport("em_conductors", "em_conductors.geojson",
                keep=("CONDUCTOR_TYPE", "SURVEY_TYPE", "YEAR"),
                rename={"CONDUCTOR_TYPE": "kind", "SURVEY_TYPE": "survey", "YEAR": "y"},
                simplify_deg=0.0008),
    LayerExport("faults_250k", "faults.geojson",
                keep=("FEAT_TYPE", "FEAT_NAME"), rename={"FEAT_TYPE": "kind", "FEAT_NAME": "name"},
                simplify_deg=0.0008),
    # the whole 250K bedrock sheet is 33 MB and most of it is not the thing the criterion looks at
    LayerExport("bedrock_250k", "graphitic_host.geojson",
                keep=("LITHOLOGY", "FORMATION"), rename={"LITHOLOGY": "lith", "FORMATION": "fm"},
                simplify_deg=0.0015, where=_graphitic),
    LayerExport("lake_sediment_sgs", "lake_sediment_u.geojson",
                keep=("U_PPM", "YR"), rename={"U_PPM": "u", "YR": "y"}),
    LayerExport("lake_water_sgs", "lake_water_u.geojson",
                keep=("U_PPM", "PH"), rename={"U_PPM": "u", "PH": "ph"}),
    LayerExport("radioactive_boulders", "radioactive_boulders.geojson",
                keep=("CPS", "YEAR", "LITHOLOGY"),
                rename={"CPS": "cps", "YEAR": "y", "LITHOLOGY": "lith"}),
    # both footprint sets are registered under one key; they are the "where people looked" half of the map
    # and the biggest files here, so they are thinned hardest
    LayerExport("assessment_surveys", "surveys_airborne.geojson", source_file="survey_footprints_airborne",
                keep=("YEAR",), rename={"YEAR": "y"}, simplify_deg=0.006),
    LayerExport("assessment_surveys", "surveys_ground.geojson", source_file="survey_footprints_ground",
                keep=("YEAR",), rename={"YEAR": "y"}, simplify_deg=0.006),
)


def _round_coords(node: Any) -> Any:
    if isinstance(node, (int, float)):
        return round(float(node), COORD_DP)
    if isinstance(node, list):
        return [_round_coords(v) for v in node]
    return node


#: below this a simplified shape is a sliver nobody can see: about a metre of line, or a square metre of area
DEGENERATE_DEG = 1e-5


def _simplify(geometry: dict[str, Any], tolerance: float) -> dict[str, Any] | None:
    """Thin a geometry. Anything that simplifies away to nothing is dropped, not drawn as a stub.

    `preserve_topology` keeps shapely from deleting a shape outright, so a polygon smaller than the tolerance
    survives as a sliver with no visible extent. Those are dropped here: they cost bytes, they cannot be seen,
    and a reader clicking one would get a feature that is not really on the map.
    """
    if tolerance <= 0:
        return {**geometry, "coordinates": _round_coords(geometry["coordinates"])}
    from shapely.geometry import mapping, shape

    try:
        geom = shape(geometry).simplify(tolerance, preserve_topology=True)
    except Exception:
        return {**geometry, "coordinates": _round_coords(geometry["coordinates"])}
    if geom.is_empty:
        return None
    areal = geom.geom_type in ("Polygon", "MultiPolygon")
    extent = geom.area if areal else geom.length
    if extent < (DEGENERATE_DEG**2 if areal else DEGENERATE_DEG):
        return None
    return {**mapping(geom), "coordinates": _round_coords(mapping(geom)["coordinates"])}


def export_layer(spec: LayerExport, source: Path, out_dir: Path) -> dict[str, Any]:
    raw = json.loads(source.read_text())
    features: list[dict[str, Any]] = []
    for i, feature in enumerate(raw.get("features") or []):
        props = feature.get("properties") or {}
        if spec.where and not spec.where(props):
            continue
        geometry = feature.get("geometry")
        if not geometry:
            continue
        thinned = _simplify(geometry, spec.simplify_deg)
        if thinned is None:
            continue
        kept = {spec.rename.get(k, k): props[k] for k in spec.keep if props.get(k) not in (None, "")}
        features.append({"type": "Feature", "id": i, "geometry": thinned, "properties": kept})

    out = out_dir / spec.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")))
    return {"key": spec.key, "file": spec.out, "features": len(features),
            "kb": round(out.stat().st_size / 1024)}


def build(log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Write every evidence layer whose licence allows it into the web app's context folder."""
    inv = load_inventory()
    by_key = {s.key: s for s in inv.sources}
    out_dir = PATHS.web_data / "context"
    rows: list[dict[str, Any]] = []
    for spec in EXPORTS:
        source = by_key.get(spec.key)
        if source is None:
            log(f"  skip {spec.key}: not in the register")
            continue
        if not source.redistributable:
            log(f"  skip {spec.key}: {source.licence.name} does not allow redistribution")
            continue
        path = PATHS.index / f"{spec.source_file or spec.key}.geojson"
        if not path.exists():
            log(f"  skip {spec.key}: {path} has not been pulled")
            continue
        row = export_layer(spec, path, out_dir)
        rows.append(row)
        log(f"  {row['file']:28s} {row['features']:>6,} features  {row['kb']:>6,} KB")
    log(f"  {len(rows)} layer(s), {sum(r['kb'] for r in rows) / 1024:.1f} MB total")
    return rows

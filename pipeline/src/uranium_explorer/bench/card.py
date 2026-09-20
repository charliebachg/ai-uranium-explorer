"""Map cards: the evidence layers around a cell, drawn so a model that reads maps sees only the evidence.

No basemap, no labels, no place names: a card is a square window of the projected grid with the layers the
criteria are computed from and nothing that would let a reader recognise the ground. The deposit and
occurrence layers are never drawn, whatever the spec says, because a card that showed them would be the key.

Cards are drawn with Pillow, from the pulled GeoJSON reprojected once into the grid's CRS (EPSG:2957) so a
kilometre on the card is a kilometre on the ground. The PNG carries no metadata, so the same inputs give the
same bytes and the manifest hash means what it says.
"""

from __future__ import annotations

import io
import math
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from shapely import transform as shapely_transform
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry

from .. import index as IX
from ..prospect.features import GRAPHITIC_WORDS
from .spec import CARD_LAYERS, LABEL_LAYERS, BenchSpec

GRID_EPSG = 2957
DRILLHOLE_LAYER = "compilation"

#: the attribute that sizes a lake-sediment point
VALUE_FIELD: dict[str, str] = {"lake_sediment_gsc": "U", "lake_sediment_sgs": "U_PPM"}
#: lake-sediment uranium above this draws no bigger
VALUE_CAP_PPM = 100.0

#: draw order (first is underneath), colour, and the one-word legend key
STYLE: dict[str, dict[str, Any]] = {
    "graphitic_host": {"fill": (232, 220, 200), "outline": (205, 190, 165), "key": "graphitic"},
    "survey_footprints_airborne": {"line": (200, 200, 200), "width": 1, "key": "airborne"},
    "survey_footprints_ground": {"line": (150, 150, 150), "width": 1, "key": "ground"},
    "faults_250k": {"line": (192, 57, 43), "width": 2, "key": "fault"},
    "em_conductors": {"line": (35, 35, 35), "width": 2, "key": "conductor"},
    "lake_sediment_gsc": {"line": (31, 119, 180), "key": "sediment"},
    "lake_sediment_sgs": {"line": (31, 119, 180), "key": "sediment"},
    "radioactive_boulders": {"fill": (230, 126, 34), "key": "boulder"},
    DRILLHOLE_LAYER: {"fill": (0, 0, 0), "key": "hole"},
}
CELL_COLOUR = (0, 0, 0)
LEGEND_H = 34
MARGIN = 14


# ---------------------------------------------------------------- layers


def is_graphitic(props: dict[str, Any]) -> bool:
    """The same rule `export_layers` uses to cut the bedrock sheet down to the graphitic host."""
    text = str(props.get("LITHOLOGY") or "").lower()
    return any(word in text for word in GRAPHITIC_WORDS)


def _number(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def reproject_feature(feature: dict[str, Any], transformer: Transformer, value_field: str | None = None
                      ) -> dict[str, Any] | None:
    """A GeoJSON feature in the grid's CRS, with its bounds and (for points that size by a value) the value."""
    geometry = feature.get("geometry")
    if not geometry:
        return None
    geom = shape(geometry)
    if geom.is_empty:
        return None
    moved = shapely_transform(geom, lambda xy: np.column_stack(transformer.transform(xy[:, 0], xy[:, 1])))
    props: dict[str, Any] = {}
    if value_field:
        props["value"] = _number((feature.get("properties") or {}).get(value_field))
    return {"type": "Feature", "geometry": mapping(moved), "bbox": list(moved.bounds), "properties": props}


def card_layers(spec: BenchSpec, read: Callable[[str], list[dict[str, Any]]] | None = None
                ) -> dict[str, list[dict[str, Any]]]:
    """Every layer the spec draws, loaded once from the pulled files and reprojected to EPSG:2957."""
    read = read or IX.read_features
    tr = Transformer.from_crs(4326, GRID_EPSG, always_xy=True)
    keys = list(spec.card.layers) + ([DRILLHOLE_LAYER] if spec.card.drillholes else [])
    out: dict[str, list[dict[str, Any]]] = {}
    for key in keys:
        if key in LABEL_LAYERS:
            raise ValueError(f"{key} is a label layer and is never drawn")
        source = CARD_LAYERS.get(key, key)
        features = read(source)
        if key == "graphitic_host":
            features = [f for f in features if is_graphitic(f.get("properties") or {})]
        moved = (reproject_feature(f, tr, VALUE_FIELD.get(key)) for f in features)
        out[key] = [m for m in moved if m is not None]
    return out


# ---------------------------------------------------------------- drawing


def _bounds(feature: dict[str, Any]) -> list[float]:
    bb = feature.get("bbox")
    return list(bb) if bb else list(shape(feature["geometry"]).bounds)


def within_window(features: list[dict[str, Any]], window: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """The features whose bounds touch the window, by a vectorised bbox test."""
    if not features:
        return []
    bb = np.array([_bounds(f) for f in features], dtype=float).reshape(-1, 4)
    x0, y0, x1, y1 = window
    hit = (bb[:, 0] <= x1) & (bb[:, 2] >= x0) & (bb[:, 1] <= y1) & (bb[:, 3] >= y0)
    return [f for f, h in zip(features, hit, strict=True) if h]


class _Canvas:
    """Pixel space over a window in metres, with the helpers each symbol needs."""

    def __init__(self, window: tuple[float, float, float, float], size: int) -> None:
        self.x0, self.y0, self.x1, self.y1 = window
        self.size = size
        self.scale = size / (self.x1 - self.x0)          # px per metre
        self.clip = box(*window)
        self.img = Image.new("RGB", (size, size), (255, 255, 255))
        self.draw = ImageDraw.Draw(self.img)

    def px(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.x0) * self.scale, (self.y1 - y) * self.scale)

    def pts(self, coords: Any) -> list[tuple[float, float]]:
        return [self.px(x, y) for x, y, *_ in coords]

    def parts(self, geom: BaseGeometry) -> list[BaseGeometry]:
        cut = geom.intersection(self.clip)
        if cut.is_empty:
            return []
        return list(cut.geoms) if hasattr(cut, "geoms") else [cut]

    def lines(self, geom: BaseGeometry, colour: tuple[int, int, int], width: int) -> None:
        for part in self.parts(geom):
            if part.geom_type == "LineString":
                self.draw.line(self.pts(part.coords), fill=colour, width=width)
            elif part.geom_type == "Polygon":
                self.draw.line(self.pts(part.exterior.coords), fill=colour, width=width)
                for ring in part.interiors:
                    self.draw.line(self.pts(ring.coords), fill=colour, width=width)

    def polygons(self, geom: BaseGeometry, fill: tuple[int, int, int], outline: tuple[int, int, int]) -> None:
        for part in self.parts(geom):
            if part.geom_type != "Polygon" or len(part.exterior.coords) < 3:
                continue
            self.draw.polygon(self.pts(part.exterior.coords), fill=fill, outline=outline)
            for ring in part.interiors:
                if len(ring.coords) >= 3:
                    self.draw.polygon(self.pts(ring.coords), fill=(255, 255, 255), outline=outline)

    def points(self, geom: BaseGeometry) -> list[tuple[float, float]]:
        parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
        out = []
        for p in parts:
            if p.geom_type == "Point" and self.x0 <= p.x <= self.x1 and self.y0 <= p.y <= self.y1:
                out.append(self.px(p.x, p.y))
        return out

    def circle(self, at: tuple[float, float], r: float, outline: tuple[int, int, int]) -> None:
        x, y = at
        self.draw.ellipse([x - r, y - r, x + r, y + r], outline=outline, width=2)

    def triangle(self, at: tuple[float, float], r: float, fill: tuple[int, int, int]) -> None:
        x, y = at
        self.draw.polygon([(x, y - r), (x - r, y + r * 0.8), (x + r, y + r * 0.8)], fill=fill)

    def dot(self, at: tuple[float, float], r: float, fill: tuple[int, int, int]) -> None:
        x, y = at
        self.draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _font(size: int = 13) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # an older Pillow without a sized default
        return ImageFont.load_default()


def point_radius(value: float | None, cap: float = VALUE_CAP_PPM) -> float:
    """Circle radius in px for a lake-sediment uranium value: log scale, capped, never below a visible dot."""
    if value is None:
        return 3.0
    return 3.0 + 6.0 * math.log10(1.0 + min(value, cap)) / math.log10(1.0 + cap)


def scale_bar_km(window_km: float) -> float:
    """The largest round length that fits a quarter of the window."""
    for km in (50.0, 20.0, 10.0, 5.0, 2.0, 1.0, 0.5):
        if km <= window_km / 4:
            return km
    return window_km / 4


def render_card(cell_id: str, spec: BenchSpec, layers: dict[str, list[dict[str, Any]]], cell_geom: BaseGeometry,
                drillholes: bool = False) -> Image.Image:
    """The card for one cell: a `window_km` square centred on it at `size_px`, layers in a fixed order, the
    cell as a bold outline, a scale bar and north arrow bottom-left, and a legend strip along the bottom.

    `cell_id` names the cell for the caller's bookkeeping only; nothing writes it on the card."""
    if not cell_id:
        raise ValueError("a card needs a cell")
    half = spec.card.window_km * 500.0
    c = cell_geom.centroid
    window = (c.x - half, c.y - half, c.x + half, c.y + half)
    cv = _Canvas(window, spec.card.size_px)
    drawn: list[str] = []
    for key in STYLE:
        if key not in layers or key in LABEL_LAYERS:
            continue
        if key == DRILLHOLE_LAYER and not drillholes:
            continue
        feats = within_window(layers[key], window)
        style = STYLE[key]
        for f in feats:
            geom = shape(f["geometry"])
            if key == "graphitic_host":
                cv.polygons(geom, style["fill"], style["outline"])
            elif key in ("survey_footprints_airborne", "survey_footprints_ground", "faults_250k", "em_conductors"):
                cv.lines(geom, style["line"], style["width"])
            elif key in ("lake_sediment_gsc", "lake_sediment_sgs"):
                r = point_radius((f.get("properties") or {}).get("value"))
                for at in cv.points(geom):
                    cv.circle(at, r, style["line"])
            elif key == "radioactive_boulders":
                for at in cv.points(geom):
                    cv.triangle(at, 5.0, style["fill"])
            elif key == DRILLHOLE_LAYER:
                for at in cv.points(geom):
                    cv.dot(at, 2.5, style["fill"])
        drawn.append(key)
    # the cell itself, last so nothing covers it
    cv.lines(cell_geom, CELL_COLOUR, 4)
    _scale_and_north(cv, spec.card.window_km)
    _legend(cv, drawn)
    return cv.img


def _scale_and_north(cv: _Canvas, window_km: float) -> None:
    km = scale_bar_km(window_km)
    length = km * 1000.0 * cv.scale
    x, y = MARGIN, cv.size - LEGEND_H - MARGIN - 6
    cv.draw.line([(x, y), (x + length, y)], fill=(0, 0, 0), width=3)
    cv.draw.line([(x, y - 6), (x, y + 6)], fill=(0, 0, 0), width=2)
    cv.draw.line([(x + length, y - 6), (x + length, y + 6)], fill=(0, 0, 0), width=2)
    cv.draw.text((x, y - 24), f"{km:g} km", fill=(0, 0, 0), font=_font(13))
    nx = x + length + 34
    cv.draw.polygon([(nx, y - 24), (nx - 7, y), (nx + 7, y)], fill=(0, 0, 0))
    cv.draw.text((nx - 4, y + 2), "N", fill=(0, 0, 0), font=_font(12))


def _legend(cv: _Canvas, drawn: list[str]) -> None:
    top = cv.size - LEGEND_H
    cv.draw.rectangle([0, top, cv.size, cv.size], fill=(255, 255, 255))
    cv.draw.line([(0, top), (cv.size, top)], fill=(180, 180, 180), width=1)
    font = _font(12)
    x, y = MARGIN, top + 10
    entries: list[tuple[str, tuple[int, int, int]]] = []
    seen: set[str] = set()
    for key in drawn:
        style = STYLE[key]
        word = style["key"]
        if word in seen:
            continue
        seen.add(word)
        entries.append((word, style.get("line") or style.get("fill") or (0, 0, 0)))
    entries.append(("cell", CELL_COLOUR))
    for word, colour in entries:
        cv.draw.rectangle([x, y, x + 14, y + 12], fill=colour, outline=colour)
        cv.draw.text((x + 20, y - 2), word, fill=(0, 0, 0), font=font)
        x += 20 + int(cv.draw.textlength(word, font=font)) + 18


def png_bytes(img: Image.Image) -> bytes:
    """The card as PNG with no metadata, so equal inputs give equal bytes."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()

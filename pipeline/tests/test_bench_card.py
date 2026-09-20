"""Cards: deterministic bytes, clipped to the window, no label layer, and the legend's words are single."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from PIL import Image
from shapely.geometry import box, mapping

from uranium_explorer.bench import card as C
from uranium_explorer.bench import spec as S


def spec(**card) -> S.BenchSpec:
    base = {"window_km": 20, "size_px": 400, "layers": ("em_conductors", "graphitic_host", "lake_sediment_sgs")}
    return S.BenchSpec(version="t", seed=1, strata=S.Strata(1, 1, 1, 1), fold_km=30, n_folds=5,
                       held_out_share=0.2, card=S.CardSpec(**{**base, **card}))


CX, CY = 500_000.0, 6_400_000.0
CELL = box(CX - 1000, CY - 1000, CX + 1000, CY + 1000)


def feature(geom, **props) -> dict:
    return {"type": "Feature", "geometry": mapping(geom), "bbox": list(geom.bounds), "properties": props}


def layers() -> dict[str, list[dict]]:
    from shapely.geometry import LineString, Point

    return {
        "em_conductors": [feature(LineString([(CX - 8000, CY - 6000), (CX + 7000, CY + 5000)]))],
        "graphitic_host": [feature(box(CX - 4000, CY + 1500, CX + 3000, CY + 6000))],
        "lake_sediment_sgs": [feature(Point(CX + 5000, CY - 4000), value=40.0)],
    }


def digest(img: Image.Image) -> str:
    return hashlib.sha256(C.png_bytes(img)).hexdigest()


def test_two_renders_of_the_same_inputs_are_byte_identical_and_draw_something() -> None:
    a = C.render_card("cell", spec(), layers(), CELL)
    b = C.render_card("cell", spec(), layers(), CELL)
    assert digest(a) == digest(b)
    assert a.size == (400, 400)
    arr = np.asarray(a)
    assert (arr < 250).any(axis=2).mean() > 0.01, "lines, a fill, a point and the cell were drawn"
    assert tuple(arr[200, 200]) != (0, 0, 0), "the cell interior is not painted over"
    # the conductor crosses the window diagonally: dark pixels near its path, none in the far corner above it
    assert (arr[:60, 340:, :] >= 250).all(), "nothing but white ground in the top-right corner"


def test_nothing_outside_the_window_is_drawn_and_a_far_feature_does_not_change_the_bytes() -> None:
    from shapely.geometry import LineString, Point

    base = layers()
    far = {k: list(v) for k, v in base.items()}
    far["em_conductors"].append(feature(LineString([(CX + 40_000, CY), (CX + 60_000, CY + 3000)])))
    far["lake_sediment_sgs"].append(feature(Point(CX - 30_000, CY), value=90.0))
    assert digest(C.render_card("cell", spec(), base, CELL)) == digest(C.render_card("cell", spec(), far, CELL))
    # a line that starts outside and ends inside is clipped: it reaches the pixels it should and no further
    crossing = {**base, "em_conductors": [feature(LineString([(CX - 30_000, CY + 5000), (CX - 2000, CY + 5000)]))]}
    img = C.render_card("cell", spec(), crossing, CELL)
    row = np.asarray(img)[100, :, :]                     # y = CY + 5000 is 100 px from the top
    dark = (row < 100).all(axis=1)
    assert dark[:150].any() and not dark[170:].any(), "ink up to the line's end at x = 160 px, none beyond"


def test_label_layers_are_never_drawn_and_drillholes_only_when_asked() -> None:
    from shapely.geometry import Point

    with_labels = {**layers(), "uranium_deposit_footprints": [feature(box(CX - 500, CY - 500, CX + 500, CY + 500))]}
    assert digest(C.render_card("cell", spec(), with_labels, CELL)) == digest(C.render_card("cell", spec(), layers(), CELL))
    holes = {**layers(), "compilation": [feature(Point(CX + 2500, CY + 2500))]}
    off = C.render_card("cell", spec(), holes, CELL, drillholes=False)
    on = C.render_card("cell", spec(), holes, CELL, drillholes=True)
    assert digest(off) == digest(C.render_card("cell", spec(), layers(), CELL))
    assert digest(on) != digest(off)


def test_card_layers_reprojects_filters_the_host_and_carries_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    pulled = {
        "em_conductors": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-105.0, 57.5], [-104.9, 57.6]]},
                           "properties": {"CONDUCTOR_TYPE": "EM"}}],
        "bedrock_250k": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[-105, 57.5], [-104.9, 57.5], [-104.9, 57.6], [-105, 57.5]]]},
             "properties": {"LITHOLOGY": "graphitic pelitic gneiss"}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[-105, 57.5], [-104.9, 57.5], [-104.9, 57.6], [-105, 57.5]]]},
             "properties": {"LITHOLOGY": "granite"}},
        ],
        "lake_sediment_sgs": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-105.0, 57.5]},
                               "properties": {"U_PPM": 12.5}},
                              {"type": "Feature", "geometry": None, "properties": {"U_PPM": 1.0}}],
    }
    out = C.card_layers(spec(), read=lambda key: pulled[key])
    assert set(out) == {"em_conductors", "graphitic_host", "lake_sediment_sgs"}
    assert len(out["graphitic_host"]) == 1, "only the graphitic polygon survives"
    x, y = out["lake_sediment_sgs"][0]["geometry"]["coordinates"]
    assert 300_000 < x < 700_000 and 6_300_000 < y < 6_500_000, "UTM zone 13N metres"
    assert out["lake_sediment_sgs"][0]["properties"]["value"] == 12.5
    assert len(out["lake_sediment_sgs"]) == 1, "a feature with no geometry is dropped"
    assert len(out["em_conductors"][0]["bbox"]) == 4


def test_point_radius_is_log_scaled_and_capped_and_the_scale_bar_is_round() -> None:
    assert C.point_radius(None) == 3.0
    assert C.point_radius(0.0) == 3.0
    assert C.point_radius(10.0) < C.point_radius(100.0) == C.point_radius(10_000.0)
    assert C.scale_bar_km(20) == 5.0 and C.scale_bar_km(50) == 10.0 and C.scale_bar_km(2) == 0.5

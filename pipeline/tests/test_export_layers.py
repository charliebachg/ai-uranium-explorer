"""Exporting the evidence layers for the map: what gets thinned, and what never leaves the machine.

The layers are the inputs the criteria are computed from, so the point of drawing them is that a reader can
check a score against the thing it came from. Two rules make that safe to ship: a licence that forbids
redistribution is honoured whatever the layer would add, and simplification touches geometry only — a
measurement is copied across as it was recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uranium_explorer import export_layers as E


def line(*coords: tuple[float, float]) -> dict:
    return {"type": "LineString", "coordinates": [list(c) for c in coords]}


def collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def write(path: Path, features: list[dict]) -> Path:
    path.write_text(json.dumps(collection(features)))
    return path


# ---------------------------------------------------------------- what is kept


def test_a_measurement_is_copied_across_untouched(tmp_path: Path) -> None:
    """Geometry may be thinned. A reading may not: a rounded assay is a different number."""
    src = write(tmp_path / "in.geojson", [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-105.123456789, 57.987654321]},
        "properties": {"U_PPM": 228.4567, "YR": 1978, "OBJECTID": 11},
    }])
    spec = E.LayerExport("lake", "out.geojson", keep=("U_PPM", "YR"), rename={"U_PPM": "u", "YR": "y"})
    E.export_layer(spec, src, tmp_path)
    out = json.loads((tmp_path / "out.geojson").read_text())["features"][0]
    assert out["properties"] == {"u": 228.4567, "y": 1978}, "the reading is untouched and OBJECTID is dropped"


def test_coordinates_are_rounded_but_the_shape_survives(tmp_path: Path) -> None:
    src = write(tmp_path / "in.geojson", [{
        "type": "Feature",
        "geometry": line((-105.123456789, 57.1), (-105.2, 57.2)),
        "properties": {},
    }])
    E.export_layer(E.LayerExport("x", "out.geojson"), src, tmp_path)
    out = json.loads((tmp_path / "out.geojson").read_text())["features"][0]
    assert out["geometry"]["coordinates"][0][0] == pytest.approx(-105.12346, abs=1e-6)
    assert len(out["geometry"]["coordinates"]) == 2


def test_a_filter_keeps_only_the_units_the_criterion_looks_at(tmp_path: Path) -> None:
    src = write(tmp_path / "in.geojson", [
        {"type": "Feature", "geometry": line((0, 0), (1, 1)),
         "properties": {"LITHOLOGY": "graphitic pelitic gneiss"}},
        {"type": "Feature", "geometry": line((0, 0), (1, 1)), "properties": {"LITHOLOGY": "sandstone"}},
    ])
    spec = E.LayerExport("bedrock", "out.geojson", where=E._graphitic)
    row = E.export_layer(spec, src, tmp_path)
    assert row["features"] == 1


def test_simplifying_drops_a_shape_rather_than_drawing_a_stub(tmp_path: Path) -> None:
    src = write(tmp_path / "in.geojson", [{
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1e-6], [1e-6, 1e-6], [0, 0]]]},
        "properties": {},
    }])
    row = E.export_layer(E.LayerExport("x", "out.geojson", simplify_deg=0.01), src, tmp_path)
    assert row["features"] == 0


def test_every_vertex_is_kept_when_no_tolerance_is_set(tmp_path: Path) -> None:
    src = write(tmp_path / "in.geojson", [{
        "type": "Feature",
        "geometry": line((0, 0), (0.5, 0.001), (1, 0)),
        "properties": {},
    }])
    E.export_layer(E.LayerExport("x", "out.geojson"), src, tmp_path)
    out = json.loads((tmp_path / "out.geojson").read_text())["features"][0]
    assert len(out["geometry"]["coordinates"]) == 3


# ---------------------------------------------------------------- what never ships


def test_a_source_that_may_not_be_redistributed_is_never_written(tmp_path: Path, monkeypatch) -> None:
    """The register, not convenience, decides. A layer that would look good is still not ours to publish."""

    class Licence:
        name = "No licence stated on the service"

    class Source:
        key = "secret"
        redistributable = False
        licence = Licence()

    class Inventory:
        sources = [Source()]

    monkeypatch.setattr(E, "load_inventory", lambda: Inventory())
    monkeypatch.setattr(E, "EXPORTS", (E.LayerExport("secret", "secret.geojson"),))
    messages: list[str] = []
    rows = E.build(log=messages.append)
    assert rows == []
    assert any("does not allow redistribution" in m for m in messages)
    assert not (tmp_path / "secret.geojson").exists()


def test_a_layer_that_was_never_pulled_is_reported_not_guessed(tmp_path: Path, monkeypatch) -> None:
    class Source:
        key = "absent"
        redistributable = True

    class Inventory:
        sources = [Source()]

    monkeypatch.setattr(E, "load_inventory", lambda: Inventory())
    monkeypatch.setattr(E, "EXPORTS", (E.LayerExport("absent", "absent.geojson"),))
    messages: list[str] = []
    assert E.build(log=messages.append) == []
    assert any("has not been pulled" in m for m in messages)


def test_the_geophysics_this_ground_lacks_is_not_exported_as_an_empty_layer() -> None:
    """Magnetics, gravity and radiometrics are carried as named gaps, not as layers with nothing in them."""
    keys = {spec.key for spec in E.EXPORTS}
    assert not {"aeromagnetic_grids", "magnetics", "gravity", "radiometrics"} & keys

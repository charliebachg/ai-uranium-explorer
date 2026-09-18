"""Labels and folds: what counts as a positive, and how ground is held out."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box

from legacy_reader.prospect import labels as L

EPSG = 2957


def cells(n: int = 4, size: int = 2000) -> gpd.GeoDataFrame:
    geoms = [box(i * size, 0, (i + 1) * size, size) for i in range(n)]
    return gpd.GeoDataFrame({"cell_id": [f"{i:04d}_0000" for i in range(n)]}, geometry=geoms,
                            crs=f"EPSG:{EPSG}")


@pytest.fixture
def patch_layers(monkeypatch):
    store: dict[str, gpd.GeoDataFrame] = {}
    monkeypatch.setattr(L, "_layer", lambda key: store[key])
    return store


def test_a_deposit_footprint_labels_the_cell_it_covers(patch_layers, monkeypatch):
    c = cells(3)
    monkeypatch.setattr(L, "load_cells", lambda grid_id=None: c)
    patch_layers["uranium_deposit_footprints"] = gpd.GeoDataFrame(
        {"DEPOSIT": ["Hurricane"]}, geometry=[box(100, 100, 900, 900)], crs=f"EPSG:{EPSG}"
    )
    patch_layers["mineral_deposits_uranium"] = gpd.GeoDataFrame(
        {"NAME": []}, geometry=[], crs=f"EPSG:{EPSG}"
    )
    out = L.label_cells(log=lambda *_: None)
    assert list(out["cell_id"]) == ["0000_0000"]
    assert out["label_tier"].iloc[0] == "deposit" and out["label_name"].iloc[0] == "Hurricane"


def test_a_footprint_that_only_abuts_a_cell_does_not_label_it(patch_layers, monkeypatch):
    c = cells(2)
    monkeypatch.setattr(L, "load_cells", lambda grid_id=None: c)
    patch_layers["uranium_deposit_footprints"] = gpd.GeoDataFrame(
        {"DEPOSIT": ["Edge"]}, geometry=[box(2000, 0, 2800, 800)], crs=f"EPSG:{EPSG}"
    )
    patch_layers["mineral_deposits_uranium"] = gpd.GeoDataFrame({"NAME": []}, geometry=[],
                                                                crs=f"EPSG:{EPSG}")
    out = L.label_cells(log=lambda *_: None)
    assert list(out["cell_id"]) == ["0001_0000"], "only the cell the footprint actually covers"


def test_a_deposit_outranks_an_occurrence_in_the_same_cell(patch_layers, monkeypatch):
    c = cells(2)
    monkeypatch.setattr(L, "load_cells", lambda grid_id=None: c)
    patch_layers["uranium_deposit_footprints"] = gpd.GeoDataFrame(
        {"DEPOSIT": ["Deposit"]}, geometry=[box(100, 100, 900, 900)], crs=f"EPSG:{EPSG}"
    )
    patch_layers["mineral_deposits_uranium"] = gpd.GeoDataFrame(
        {"NAME": ["Showing"]}, geometry=[Point(1000, 1000)], crs=f"EPSG:{EPSG}"
    )
    out = L.label_cells(log=lambda *_: None).set_index("cell_id")
    assert out.loc["0000_0000", "label_tier"] == "deposit"


def test_an_occurrence_labels_only_the_cell_it_is_near(patch_layers, monkeypatch):
    c = cells(3)
    monkeypatch.setattr(L, "load_cells", lambda grid_id=None: c)
    patch_layers["uranium_deposit_footprints"] = gpd.GeoDataFrame({"DEPOSIT": []}, geometry=[],
                                                                  crs=f"EPSG:{EPSG}")
    patch_layers["mineral_deposits_uranium"] = gpd.GeoDataFrame(
        {"NAME": ["Showing"]}, geometry=[Point(1000, 1000)], crs=f"EPSG:{EPSG}"
    )
    out = L.label_cells(log=lambda *_: None)
    assert list(out["cell_id"]) == ["0000_0000"]
    assert out["label_tier"].iloc[0] == "occurrence"


def test_camps_group_neighbours_and_separate_districts():
    c = cells(40, size=2000)
    pos = pd.DataFrame({"cell_id": ["0000_0000", "0001_0000", "0039_0000"],
                        "label_tier": ["deposit"] * 3, "label_name": [""] * 3})
    camp = L.camps(pos, c, eps_km=10)
    assert camp["0000_0000"] == camp["0001_0000"], "2 km apart is one camp"
    assert camp["0039_0000"] != camp["0000_0000"], "78 km away is a different camp"


def test_blocks_come_from_position_alone_and_are_stable():
    c = cells(40, size=2000)
    a = L.blocks(c, block_km=30)
    b = L.blocks(c, block_km=30)
    assert (a == b).all(), "folds must not depend on anything but where a cell is"
    assert a.nunique() >= 2, "78 km of cells spans more than one 30 km block"
    assert a["0000_0000"] == a["0001_0000"]

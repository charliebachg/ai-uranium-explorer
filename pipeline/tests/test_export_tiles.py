"""Tiles: the arguments that keep every feature, the manifest that hashes both ends, and a real archive."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from uranium_explorer import export_tiles as X


def fc(n: int = 3) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"k": i}, "geometry": {"type": "Point", "coordinates": [-105.0 + i * 0.01, 57.5]}}
        for i in range(n)]}


def test_arguments_keep_every_feature_and_name_the_layer_after_the_source() -> None:
    ex = X.TileExport("conductors", "context/em_conductors.geojson")
    args = X.tippecanoe_args(ex, Path("in.geojson"), Path("out.pmtiles"))
    assert "-pf" in args and "-pk" in args and "-r1" in args and "--generate-ids" in args, "no feature and no point is dropped at any zoom"
    assert args[args.index("-l") + 1] == "conductors" and "-Z4" in args and "-z12" in args
    assert not any(a.startswith("--drop") for a in args), "nothing is dropped to fit a tile"


def test_the_export_list_matches_the_map_source_ids() -> None:
    ids = [e.source_id for e in X.EXPORTS]
    assert ids == ["conductors", "faults", "host", "lakesed", "lakewater", "boulders", "surveyair", "surveyground", "cells"]
    assert next(e for e in X.EXPORTS if e.source_id == "cells").minzoom == 5


def test_build_writes_a_manifest_that_hashes_both_ends_and_names_missing_inputs(tmp_path: Path) -> None:
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "faults.geojson").write_text(json.dumps(fc(3)))
    calls = []

    def fake_run(args):
        calls.append(args)
        Path(args[args.index("-o") + 1]).write_bytes(b"PMTiles-fake")

    m = X.build(exports=(X.TileExport("faults", "context/faults.geojson"), X.TileExport("host", "context/graphitic_host.geojson")),
                data_dir=tmp_path, log=lambda *a: None, run=fake_run)
    assert len(calls) == 1 and set(m["tiles"]) == {"faults"}
    t = m["tiles"]["faults"]
    assert t["features"] == 3 and t["layer"] == "faults" and len(t["sha256"]) == 64 and len(t["source_sha256"]) == 64
    assert m["skipped"] == ["host: context/graphitic_host.geojson not exported yet"]
    assert json.loads((tmp_path / "tiles" / "manifest.json").read_text())["tiles"]["faults"]["file"] == "faults.pmtiles"


@pytest.mark.skipif(shutil.which("tippecanoe") is None, reason="tippecanoe not installed")
def test_a_real_archive_is_pmtiles_and_keeps_every_feature(tmp_path: Path) -> None:
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "faults.geojson").write_text(json.dumps(fc(40)))
    m = X.build(exports=(X.TileExport("faults", "context/faults.geojson", 4, 8),), data_dir=tmp_path, log=lambda *a: None)
    out = tmp_path / "tiles" / "faults.pmtiles"
    assert out.read_bytes()[:7] == b"PMTiles", "the PMTiles v3 magic"
    assert m["tiles"]["faults"]["features"] == 40 and m["tiles"]["faults"]["bytes"] == out.stat().st_size

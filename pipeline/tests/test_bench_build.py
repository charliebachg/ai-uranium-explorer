"""Build and audit, end to end on a synthetic store: the layout, the hashes, the leaks the audit catches,
and that a second build writes nothing new."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, box, mapping

from legacy_reader import store as ST
from legacy_reader.bench import build as B
from legacy_reader.bench import spec as S
from legacy_reader.prospect import retrieve as R
from bench_store import bench_frame, fake_fit, make_bench_store

SPEC = """
version = "t1"
seed = 11
fold_km = 30
n_folds = 5
held_out_share = 0.25

[strata]
deposit = 6
occurrence = 6
negative = 12
probe = 4

[card]
window_km = 20
size_px = 300
layers = ["em_conductors", "graphitic_host", "lake_sediment_sgs"]
drillholes = false

[pack]

[blind]
radius_km = 10

[retrieval]
k = 6
radius_km = 5000
query = "graphitic conductor unconformity uranium mineralization alteration"
"""


@pytest.fixture
def world(prospect_sandbox, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A synthetic store, a small spec, synthetic card layers, and a bench root in the temp dir."""
    db = prospect_sandbox.db
    df = make_bench_store(db, bench_frame(n_dep=12, n_occ=20, n_neg=30, n_probe=10, seed=4))
    monkeypatch.setattr(ST, "db_path", lambda: db)
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "t1.toml").write_text(SPEC)
    monkeypatch.setattr(S, "CONFIG_DIR", configs)

    def feature(geom, **props):
        return {"type": "Feature", "geometry": mapping(geom), "bbox": list(geom.bounds), "properties": props}

    layers = {
        "em_conductors": [feature(LineString([(x, y - 3000), (x + 4000, y + 3000)])) for x, y in zip(df["cx"][::5], df["cy"][::5], strict=True)],
        "graphitic_host": [feature(box(x - 3000, y - 2000, x + 1000, y + 2500)) for x, y in zip(df["cx"][::7], df["cy"][::7], strict=True)],
        "lake_sediment_sgs": [feature(Point(x + 2500, y - 1500), value=float(v)) for x, y, v in zip(df["cx"][::3], df["cy"][::3], df["sed_samples_n"][::3], strict=True)],
    }
    monkeypatch.setattr(B, "card_layers", lambda spec: layers)
    R.load_corpus.cache_clear()
    root = tmp_path / "bench"
    yield {"db": db, "df": df, "root": root, "layers": layers}
    R.load_corpus.cache_clear()


def test_build_writes_the_layout_and_audit_passes(world) -> None:
    lines: list[str] = []
    m = B.build("t1", log=lines.append, root=world["root"], fit=fake_fit)
    out = world["root"] / "t1"
    assert m["counts"]["stratum"] == {"deposit": m["counts"]["stratum"]["deposit"], "occurrence": 6, "negative": 12, "probe": 4}
    assert 1 <= m["counts"]["stratum"]["deposit"] <= 6
    n = m["counts"]["cells"]
    assert m["counts"]["split"]["open"] + m["counts"]["split"]["heldout"] == n
    assert m["seed"] == 11 and m["spec"]["retrieval"]["k"] == 6 and m["git_commit"] == "deadbeef"
    assert m["store_sha256"] and len(m["store_sha256"]) == 64
    for name in ("cells.jsonl", "key.json", "heldout.json", "manifest.json", "oof_scores.csv"):
        assert (out / name).is_file(), name
    assert len(list((out / "packs").glob("*.json"))) == n and len(list((out / "cards").glob("*.png"))) == n
    assert len(list((out / "blind").glob("*.json"))) == n
    assert "packs/b-0001.json" in m["files"] and "cards/b-0001.png" in m["files"] and "key.json" not in m["files"]
    assert m["key_sha256"] == B.sha256_file(out / "key.json")
    rows = [json.loads(line) for line in (out / "cells.jsonl").read_text().splitlines()]
    assert {r["bench_id"] for r in rows} == {f"b-{i:04d}" for i in range(1, n + 1)}
    key = json.loads((out / "key.json").read_text())
    assert set(key) == {r["bench_id"] for r in rows}
    labels = dict(zip(world["df"]["cell_id"], world["df"]["label_tier"], strict=True))
    for r in rows:
        assert key[r["bench_id"]] == {"label": labels[r["cell_id"]], "stratum": r["stratum"], "fold": r["fold"], "split": r["split"]}
    heldout = json.loads((out / "heldout.json").read_text())
    assert heldout == sorted(r["bench_id"] for r in rows if r["split"] == "heldout")
    pack = json.loads((out / "packs" / "b-0001.json").read_text())
    assert pack["bench_id"] == "b-0001" and set(pack["tools"]) == {"cell_features", "criteria_breakdown", "coverage"}
    assert "stratum" not in json.dumps(pack) and "label" not in pack
    oof = (out / "oof_scores.csv").read_text().splitlines()
    assert oof[0] == "bench_id,model,fold_kind,fold,score" and len(oof) == 1 + 3 * n
    passages = list((out / "passages").glob("*.json"))
    assert passages, "cells near the synthetic files have scrubbed passages"
    text = " ".join(p.read_text() for p in passages)
    assert "SMDC" not in text and "Cameco" not in text and "74H09" not in text and "[redacted]" in text
    assert B.audit("t1", root=world["root"]) == []
    assert any("sampled:" in line for line in lines)


def test_a_second_build_is_a_no_op_and_the_bytes_are_stable(world) -> None:
    root = world["root"]
    m1 = B.build("t1", log=lambda *a: None, root=root, fit=fake_fit)
    lines: list[str] = []
    m2 = B.build("t1", log=lines.append, root=root, fit=fake_fit)
    assert m1["files"] == m2["files"] and m1["key_sha256"] == m2["key_sha256"]
    assert any("resuming" in line for line in lines)
    assert any("wrote: packs 0, cards 0" in line and "blind 0, passages 0" in line for line in lines)
    # a card removed by hand is redrawn to the same bytes
    (root / "t1" / "cards" / "b-0001.png").unlink()
    m3 = B.build("t1", log=lambda *a: None, root=root, fit=fake_fit)
    assert m3["files"]["cards/b-0001.png"] == m1["files"]["cards/b-0001.png"]


def test_audit_fails_on_a_coordinate_a_name_a_cell_id_and_a_bad_hash(world) -> None:
    root = world["root"]
    B.build("t1", log=lambda *a: None, root=root, fit=fake_fit)
    out = root / "t1"
    pack_path = out / "packs" / "b-0002.json"
    pack = json.loads(pack_path.read_text())
    pack["tools"]["cell_features"]["note"] += " measured at 58.3510 N"
    pack_path.write_text(json.dumps(pack))
    problems = B.audit("t1", root=root)
    assert any("packs/b-0002.json: hash differs" in p for p in problems)
    assert any("packs/b-0002.json: coordinate pattern '58.3510'" in p for p in problems)
    pack["tools"]["cell_features"]["note"] = "drilled by SMDC near Cigar Lake in 0000_0003 under MAW00509 on 74H09"
    pack["lon"] = -105.0
    pack["extra"] = 6_409_000.0
    pack_path.write_text(json.dumps(pack))
    problems = B.audit("t1", root=root)
    reasons = " | ".join(problems)
    for expected in ("name 'SMDC'", "cell id pattern '0000_0003'", "file pattern 'MAW00509'", "nts pattern '74H09'",
                     "forbidden key 'lon'", "northing range"):
        assert expected in reasons, expected
    # the held-out list must agree with the cell list
    heldout = json.loads((out / "heldout.json").read_text())
    (out / "heldout.json").write_text(json.dumps(heldout + ["b-0001"] if "b-0001" not in heldout else heldout[1:]))
    assert any("heldout" in p for p in B.audit("t1", root=root))


def test_audit_reports_a_missing_manifest_and_show_prints_counts(world, capsys) -> None:
    assert B.audit("t1", root=world["root"]) == [f"no manifest at {world['root'] / 't1' / 'manifest.json'}"]
    assert B.show("t1", root=world["root"]) == {}
    B.build("t1", log=lambda *a: None, root=world["root"], fit=fake_fit)
    lines: list[str] = []
    m = B.show("t1", root=world["root"], log=lines.append)
    assert m["version"] == "t1"
    joined = "\n".join(lines)
    assert "UraniumBench t1" in joined and "occurrence 6" in joined and "heldout" in joined and "packs" in joined


def test_leaks_is_pure_and_precise() -> None:
    clean = {"tools": {"cell_features": {"rows": [{"value": 57.5, "feature": "relief_m", "note": "0.5723 share; 1979.0 first year"}]}},
             "values": {"b:b-0001:cell:relief_m": {"id": "b:b-0001:cell:relief_m", "value": 512.3}}}
    assert B.leaks(clean, {"0000_0001"}, None, True) == []
    assert B.leaks({"a": "cell 0000_0001"}, {"0000_0001"}, None, True) == ["cell id 0000_0001 at /a", "cell id pattern '0000_0001' at /a"]
    assert B.leaks({"a": "cell 0200_0001"}, {"0000_0001"}, None, True) == ["cell id pattern '0200_0001' at /a"]
    assert B.leaks({"a": "at -104.2825 west"}, set(), None, True) == ["coordinate pattern '-104.2825' at /a"]
    assert B.leaks({"stratum": "deposit"}, set(), None, True) == ["answer-key field 'stratum' at /"]
    assert B.leaks({"stratum": "deposit"}, set(), None, False) == [], "a passage file may say what it likes about strata"


def test_a_changed_spec_rebuilds_every_file(world, monkeypatch) -> None:
    import legacy_reader.bench.build as BB

    root = world["root"]
    BB.build("t1", log=lambda *a: None, root=root, fit=fake_fit)
    spec = BB.load_spec("t1")
    from dataclasses import replace
    changed = replace(spec, pack=replace(spec.pack, label_context=not spec.pack.label_context))
    monkeypatch.setattr(BB, "load_spec", lambda version: changed)
    lines: list[str] = []
    BB.build("t1", log=lines.append, root=root, fit=fake_fit)
    assert any("spec changed" in line for line in lines)
    assert not any("resuming" in line for line in lines)

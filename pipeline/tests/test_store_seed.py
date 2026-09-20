"""The seed pack: a store becomes Parquet and comes back the same; the public pack holds only what the inventory
says may be redistributed; a tampered file or manifest is refused; the same store always packs to the same
manifest. The real-data regression at the end runs the quick model search against the live store and is
gated on LR_REAL_DATA=1, because it takes a minute and the suite must stay fast."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import duckdb
import pytest

from legacy_reader.store import connect, db_path, tier_audit, write_meta
from legacy_reader.store import seed as SEED

LAYER = ("insert into native.layer (layer_key, title, service_url, layer_id, where_clause, out_sr, record_count, "
         "payload_sha256, licence, licence_url, redistributable, retrieved_at, bears_on, role, notes) values ")


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A small store built from schema.sql: every tier, a JSON and a BLOB column, a table schema.sql does not
    declare, one layer the inventory does not redistribute, and page text that must never leave the machine."""
    db = tmp_path / "fixture.duckdb"
    con = connect(db)
    con.execute(LAYER + "('em_conductors', 'EM conductors', 'https://gis.saskatchewan.ca/egis/rest/services/Economy/"
                "Regional_Datasets_and_Compilations/FeatureServer/7', 7, '1=1', 4326, 2, 'aaa', 'sk', 'u', true, 't', "
                "'pathway', 'feature', '')")
    con.execute(LAYER + "('geods_holes', 'GeoDS holes', 'https://geoscience-data-system.saskatchewan.ca/arcgis/rest/"
                "services/P_GeoDS_DrillingPage_EM/FeatureServer/2', 2, '1=1', 4326, 1, 'bbb', 'No licence stated on the "
                "service', null, false, 't', 'effort', 'feature', '')")
    con.execute(LAYER + "('uranium_deposit_footprints', 'Deposits', 'https://gis.saskatchewan.ca/egis/rest/services/"
                "Economy/Regional_Datasets_and_Compilations/FeatureServer/5', 5, '1=1', 4326, 1, 'ccc', 'sk', 'u', true, "
                "'t', 'label', 'label', '')")
    con.execute(LAYER + "('basin_geology', 'Athabasca outline', 'https://gis.saskatchewan.ca/egis/rest/services/Economy/"
                "Million_Scale_Geology/FeatureServer/6', 6, '1=1', 4326, 1, 'ddd', 'sk', 'u', true, 't', 'study_area', 'context', '')")
    # the file index: context, no licence; it is not an input to the derived tier and must not taint it
    con.execute(LAYER + "('file_index_uranium', 'Assessment file index', 'https://geoscience-data-system.saskatchewan.ca/x', "
                "0, '1=1', 4326, 1, 'eee', 'No licence stated on the service', null, false, 't', null, 'context', '')")
    con.execute("insert into native.scene (collection, stac_id, asset_key, href, datetime, cloud_cover, epsg, gsd, licence, "
                "retrieved_at) values ('sentinel-2-l2a', 'S2A_1', 'swir16', 'https://x/y.tif', '2025-07-01', 3.0, 32613, 20, "
                "'copernicus', 't')")
    con.execute("insert into native.corpus_file (file_num, company, property, work_period, nts, work_description, lon, lat, "
                "n_holes, hole_names, retrieved_at) values ('74H0001', 'Co', 'Prop', '1979', '74H', 'drilling', -105.5, 58.1, "
                "2, 'A-1,A-2', 't')")
    con.execute("insert into read.corpus_page (file_num, doc_name, doc_sha256, page, chars, text, extracted_at, source) "
                "values ('74H0001', 'report.pdf', 'd' || repeat('0', 63), 3, 21, 'verbatim page text 121.7', 't', 'ocr')")
    con.execute("insert into derived.grid values ('g2km', 2000, 2957, 30000, 'POLYGON((0 0,1 0,1 1,0 1,0 0))', 2, 't', 'derived')")
    con.execute("insert into derived.cell (cell_id, grid_id, col, row, cx, cy, lon, lat, geom_wkb, in_basin) values "
                "('0001_0001', 'g2km', 1, 1, 1000.0, 1000.0, -105.0, 58.0, '\\x01\\x02'::blob, true), "
                "('0001_0002', 'g2km', 1, 2, 1000.0, 3000.0, -105.0, 58.02, '\\x03'::blob, false)")
    con.execute("insert into derived.feature_spec (feature_key, title, unit, from_tier, source_keys, bears_on, is_effort, "
                "is_label, is_count, notes) values ('d_conductor_m', 'Distance to conductor', 'm', 'native', "
                "'[\"em_conductors\"]', 'pathway', false, false, false, '')")
    con.execute("insert into derived.cell_feature (cell_id, feature_key, value, value_text, unit, n_obs, nearest_m, from_tier, "
                "op, tool, params, inputs, computed_at) values "
                "('0001_0001', 'd_conductor_m', 512.0, null, 'm', 2, 512.0, 'native', 'nearest', 'features', '{\"k\": 1}', "
                "'[\"em_conductors\"]', 't'), "
                "('0001_0002', 'd_conductor_m', null, null, 'm', 0, null, 'native', 'nearest', 'features', null, "
                "'[\"em_conductors\"]', 't')")
    con.execute("insert into derived.cell_label (cell_id, label_tier, label_name, camp_id, block_id, computed_at) values "
                "('0001_0001', 'deposit', 'X', 1, 1, 't'), ('0001_0002', 'unlabelled', null, -1, 1, 't')")
    con.execute("insert into agent.memo (memo_id, cell_id, role, verdict, model, prompt_version, run_id, cache_key, cost_usd, "
                "duration_s, created_at, published) values ('m1', '0001_0001', 'skeptic', 'insufficient', 'test', 'v1', 'r1', "
                "null, 0.0, 0.0, 't', true)")
    con.execute("insert into agent.memo_claim values ('m1', 1, 'claim', '[\"v1\"]', 'agent')")
    # what `lr store rebuild` does for the stage tables schema.sql does not declare: a plain table with a tier column
    con.execute("create table read.page as select '74H0001' as file_num, 'p1' as page_id, 'd' as pdf_sha256, 3 as page, "
                "'table' as page_kind, 'read' as tier")
    # a v1 leftover outside the four tiers: not part of the store contract, never packed
    con.execute("create table main.pages as select 'p1' as page_id, 'old' as note")
    write_meta(con, "test")
    con.close()
    return db


def _counts(db: Path) -> dict[str, int]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("select table_schema, table_name from information_schema.tables where table_type = 'BASE TABLE' "
                           "and table_schema in ('native', 'read', 'derived', 'agent', 'expert', 'main') order by 1, 2").fetchall()
        return {f"{s}.{t}": con.execute(f"select count(*) from {s}.{t}").fetchone()[0] for s, t in rows}
    finally:
        con.close()


def _decisions(db: Path) -> dict[str, dict]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {d["table"]: d for d in SEED.decisions(con)}
    finally:
        con.close()


# ---------------------------------------------------------------- the licence rule


def test_the_licence_decision_per_table_follows_the_inventory_flags(store: Path) -> None:
    d = _decisions(store)
    assert d["native.layer"]["public"], "the registry carries flags, not payloads"
    assert d["native.scene"]["public"] and "copernicus" in d["native.scene"]["reason"]
    assert d["native.feature"]["public"] and "no rows" in d["native.feature"]["reason"]
    for name in ("derived.cell", "derived.cell_feature", "derived.feature_spec", "derived.cell_label", "derived.grid"):
        assert d[name]["public"], name
    assert "em_conductors" in d["derived.cell_feature"]["sources"]
    assert "uranium_deposit_footprints" in d["derived.cell_feature"]["sources"], "the label layer is upstream of the derived tier"
    assert "basin_geology" in d["derived.cell_feature"]["sources"], "so is the study area"
    assert "file_index_uranium" not in d["derived.cell_feature"]["sources"], "an unlicensed context layer is not"
    assert "geods_holes" not in d["derived.cell_feature"]["sources"], "nor a feature layer no feature cites"
    assert d["main.meta"]["public"]
    for name in ("read.corpus_page", "read.page", "native.corpus_file"):
        assert not d[name]["public"] and d[name]["private"], name
        assert "licence" in d[name]["reason"].lower() or "read tier" in d[name]["reason"], name
    assert "read_assay_intervals" in d["read.corpus_page"]["sources"], "the inventory's read-tier source is the basis"
    assert not d["agent.memo"]["public"] and d["agent.memo"]["private"] and "agent tier" in d["agent.memo"]["reason"]
    assert not d["agent.memo_claim"]["public"]


def test_a_native_table_carrying_an_unlicensed_layer_leaves_the_public_pack(store: Path) -> None:
    con = connect(store)
    con.execute("insert into native.feature (layer_key, record_id, geom_wkb, geom_type, epsg, minx, miny, maxx, maxy, attrs) "
                "values ('em_conductors', 1, null, 'LineString', 4326, 0, 0, 1, 1, '{}'), "
                "('geods_holes', 1, null, 'Point', 4326, 0, 0, 0, 0, '{\"hole\": \"A-1\"}')")
    con.close()
    d = _decisions(store)["native.feature"]
    assert not d["public"] and d["private"]
    assert "geods_holes" in d["reason"] and "not_stated" in d["reason"], d["reason"]
    assert d["sources"] == ["em_conductors", "geods_holes"]


def test_a_derived_feature_read_off_pages_taints_the_derived_tier(store: Path) -> None:
    con = connect(store)
    con.execute("insert into derived.feature_spec (feature_key, title, unit, from_tier, source_keys, bears_on, is_effort, "
                "is_label, is_count, notes) values ('u3o8_max', 'Max grade read', '%', 'read', '[\"read_assay_intervals\"]', "
                "'label', false, true, false, '')")
    con.execute("insert into derived.cell_feature (cell_id, feature_key, value, value_text, unit, n_obs, nearest_m, from_tier, "
                "op, tool, params, inputs, computed_at) values ('0001_0001', 'u3o8_max', 1.2, null, '%', 1, null, 'read', "
                "'max', 'features', null, '[\"read_assay_intervals\"]', 't')")
    con.close()
    d = _decisions(store)
    assert not d["derived.cell_feature"]["public"] and "read_assay_intervals" in d["derived.cell_feature"]["reason"]
    assert not d["derived.cell_label"]["public"], "every derived table shares the tier's upstream"


# ---------------------------------------------------------------- pack, verify, unpack


def test_the_public_pack_leaves_out_what_the_inventory_does_not_redistribute(store: Path, tmp_path: Path) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    d = Path(m["dir"])
    assert d.parent == tmp_path / "seed" and d.name == m["pack_sha256"][:16]
    assert (tmp_path / "seed" / "latest").resolve() == d.resolve()
    assert {"native.layer", "native.scene", "native.feature", "main.meta", "derived.grid", "derived.cell", "derived.cell_feature",
            "derived.feature_spec", "derived.cell_label", "derived.cell_score"} <= set(m["tables"])
    assert all(t["public"] for t in m["tables"].values())
    assert {"read.corpus_page", "read.page", "native.corpus_file", "native.assay_sheet_row", "agent.memo", "agent.memo_claim"} <= set(m["excluded"])
    assert not any(t["public"] for t in m["excluded"].values())
    assert not any(name.startswith("derived.") for name in m["excluded"]), "nothing derived from public sources is held back"
    assert set(m["excluded"]) & {"main.meta", "main.pages"} == {"main.pages"}, "the version stamp ships; the v1 leftover never does"
    assert set(m["tables"]).isdisjoint(m["excluded"])
    assert m["excluded"]["read.corpus_page"]["reason"] and m["excluded"]["read.corpus_page"]["rows"] == 1
    assert not (d / "read.corpus_page.parquet").exists()
    assert "verbatim page text" not in b"".join(p.read_bytes() for p in d.rglob("*") if p.is_file()).decode("latin-1")
    assert m["excluded"]["read.corpus_page"]["shape"] == "shapes/read.corpus_page.parquet", "the shape ships, the rows do not"
    assert (d / "shapes" / "read.page.parquet").is_file() and "shape" not in m["excluded"]["main.pages"]
    on_disk = json.loads((d / "manifest.json").read_text())
    assert on_disk["licence"]["basis"] == "knowledge/data_inventory.toml" and on_disk["licence"]["licences"]["not_stated"]["redistributable"] is False
    assert on_disk["store_version"] == "store/v2" and on_disk["pipeline_version"] == "test" and len(on_disk["store_sha256"]) == 64
    assert on_disk["tables"]["derived.cell"]["declared"] is True and on_disk["tables"]["derived.cell"]["order_by"] == ["cell_id"]


def test_pack_then_unpack_round_trip_keeps_every_row_and_the_tiers(store: Path, tmp_path: Path) -> None:
    before = _counts(store)
    m = SEED.pack("private", out=tmp_path / "seed", path=store, log=lambda *a: None)
    assert set(m["excluded"]) == {"main.pages"} and "shape" not in m["excluded"]["main.pages"]
    before.pop("main.pages")
    assert set(m["tables"]) == set(before), "the private pack carries every table the store contract covers"
    rebuilt = tmp_path / "rebuilt" / "lr.duckdb"
    side = SEED.unpack(Path(m["dir"]), into=rebuilt, log=lambda *a: None)
    assert _counts(rebuilt) == before
    assert side["pack_sha256"] == m["pack_sha256"] and side["source_store_sha256"] == m["store_sha256"]
    assert json.loads(rebuilt.with_name("lr.duckdb.seed.json").read_text())["tables"] == before
    con = duckdb.connect(str(rebuilt), read_only=True)
    try:
        assert tier_audit(con) == []
        assert con.execute("select text from read.corpus_page").fetchone()[0] == "verbatim page text 121.7"
        assert con.execute("select geom_wkb from derived.cell where cell_id = '0001_0001'").fetchone()[0] == b"\x01\x02"
        assert con.execute("select json_extract(params, '$.k')::int from derived.cell_feature where cell_id = '0001_0001'").fetchone()[0] == 1
        assert con.execute("select value from derived.cell_feature where cell_id = '0001_0002'").fetchone()[0] is None
        assert con.execute("select store_version, pipeline_version from main.meta").fetchone() == ("store/v2", "test")
        assert con.execute("select page_kind, tier from read.page").fetchone() == ("table", "read")
        pk = con.execute("select constraint_column_names from duckdb_constraints() where table_name = 'cell_feature' "
                         "and constraint_type = 'PRIMARY KEY'").fetchone()
        assert pk and list(pk[0]) == ["cell_id", "feature_key"], "declared tables keep their constraints"
        views = {r[0] for r in con.execute("select table_name from information_schema.tables where table_type = 'VIEW'").fetchall()}
        assert {"v_feature_matrix", "v_layer_summary"} <= views
    finally:
        con.close()


def test_the_public_pack_unpacks_to_a_store_without_the_private_tables(store: Path, tmp_path: Path) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    rebuilt = tmp_path / "public.duckdb"
    SEED.unpack(tmp_path / "seed" / "latest", into=rebuilt, log=lambda *a: None)
    counts = _counts(rebuilt)
    assert counts["derived.cell_feature"] == 2 and counts["native.layer"] == 5
    assert counts["read.corpus_page"] == 0 and counts["agent.memo"] == 0, "declared, empty"
    assert counts["read.page"] == 0 and "main.pages" not in counts, "undeclared: present as its shape; the leftover not at all"
    con = duckdb.connect(str(rebuilt), read_only=True)
    try:
        assert [c[0] for c in con.execute("describe read.page").fetchall()] == ["file_num", "page_id", "pdf_sha256", "page", "page_kind", "tier"]
        assert tier_audit(con) == []
    finally:
        con.close()


def test_a_tampered_parquet_is_refused_and_leaves_no_store(store: Path, tmp_path: Path) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    f = Path(m["dir"]) / "derived.cell.parquet"
    raw = bytearray(f.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    f.write_bytes(bytes(raw))
    with pytest.raises(SEED.SeedError, match="derived.cell: sha256"):
        SEED.verify(Path(m["dir"]), log=lambda *a: None)
    target = tmp_path / "x" / "lr.duckdb"
    with pytest.raises(SEED.SeedError):
        SEED.unpack(Path(m["dir"]), into=target, log=lambda *a: None)
    assert not target.exists() and not list(target.parent.glob("*")) if target.parent.exists() else True


def test_an_edited_manifest_is_refused_by_its_own_address(store: Path, tmp_path: Path) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    p = Path(m["dir"]) / "manifest.json"
    edited = json.loads(p.read_text())
    edited["tables"]["derived.cell"]["rows"] = 1
    p.write_text(json.dumps(edited))
    problems: list[str] = []
    with pytest.raises(SEED.SeedError):
        SEED.verify(Path(m["dir"]), log=problems.append)
    assert any("own pack_sha256" in line for line in problems) and any("rows in the file" in line for line in problems)


def test_the_manifest_is_deterministic_for_the_same_store(store: Path, tmp_path: Path) -> None:
    a = SEED.pack("private", out=tmp_path / "a", path=store, log=lambda *a: None)
    b = SEED.pack("private", out=tmp_path / "b", path=store, log=lambda *a: None)
    assert a["pack_sha256"] == b["pack_sha256"]
    assert (Path(a["dir"]) / "manifest.json").read_bytes() == (Path(b["dir"]) / "manifest.json").read_bytes()
    for name, t in a["tables"].items():
        assert (Path(a["dir"]) / t["file"]).read_bytes() == (Path(b["dir"]) / t["file"]).read_bytes(), name
    public = SEED.pack("public", out=tmp_path / "c", path=store, log=lambda *a: None)
    assert public["pack_sha256"] != a["pack_sha256"], "a different scope is a different pack"
    again = SEED.pack("private", out=tmp_path / "a", path=store, log=lambda *a: None)
    assert again["dir"] == a["dir"], "packing the same content twice into one root keeps the one directory"


def test_unpack_refuses_to_overwrite_a_store(store: Path, tmp_path: Path) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    with pytest.raises(SEED.SeedError, match="refuses to overwrite"):
        SEED.unpack(Path(m["dir"]), into=store, log=lambda *a: None)


def test_ensure_unpacks_only_when_the_store_is_missing(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    target = tmp_path / "fresh" / "lr.duckdb"
    monkeypatch.setenv("LR_SEED_DIR", str(tmp_path / "seed" / "latest"))
    assert SEED.seed_dir() == tmp_path / "seed" / "latest"
    assert SEED.ensure(into=target, log=lambda *a: None) == "unpacked" and target.is_file()
    assert SEED.ensure(into=target, log=lambda *a: None) == "present"
    monkeypatch.setenv("LR_SEED_DIR", str(tmp_path / "nowhere"))
    assert SEED.ensure(into=tmp_path / "other.duckdb", log=lambda *a: None) == "no-seed"
    assert not (tmp_path / "other.duckdb").exists()
    monkeypatch.delenv("LR_SEED_DIR")
    assert SEED.seed_dir() == SEED.seed_root() / "latest"


# ---------------------------------------------------------------- hosting: push and pull


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch):
    """A fake S3 in this process (moto): no network, no endpoint, the standard AWS names set to test values."""
    from moto import mock_aws

    for name in ("LR_S3_ENDPOINT", "LR_S3_KEY", "LR_S3_SECRET", "AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        import boto3

        yield boto3.client("s3", region_name="us-east-1")


def _keys(client, bucket: str) -> list[str]:
    out = client.list_objects_v2(Bucket=bucket)
    return sorted(o["Key"] for o in out.get("Contents", []))


def _same_pack(a: Path, b: Path) -> bool:
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    return files == sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file()) and all(
        (a / f).read_bytes() == (b / f).read_bytes() for f in files)


def test_push_writes_the_pack_under_its_hash_with_the_manifest_last_and_pull_brings_it_back(store: Path, tmp_path: Path, s3, monkeypatch) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    h = m["pack_sha256"][:16]
    order: list[str] = []
    real_put = SEED._S3.put
    monkeypatch.setattr(SEED._S3, "put", lambda self, name, src: (order.append(name), real_put(self, name, src)))
    out = SEED.push(Path(m["dir"]), "s3://seeds/latest", log=lambda *a: None)
    assert out["pack_sha256"] == m["pack_sha256"] and out["files"] == len(order)
    assert order[-2:] == [f"{h}/manifest.json", "manifest.json"], "the pack's manifest closes its directory; the pointer closes the push"
    assert all(name.startswith(f"{h}/") for name in order[:-1])
    keys = _keys(s3, "seeds")
    assert f"latest/{h}/manifest.json" in keys and "latest/manifest.json" in keys and f"latest/{h}/derived.cell.parquet" in keys
    assert f"latest/{h}/shapes/read.corpus_page.parquet" in keys, "the shapes travel too"
    assert not any(k.startswith("latest/") and k.count("/") == 1 and k != "latest/manifest.json" for k in keys), "nothing but the pointer sits at the prefix"

    pulled = SEED.pull("s3://seeds/latest", into=tmp_path / "pulled", log=lambda *a: None)
    d = Path(pulled["dir"])
    assert d == tmp_path / "pulled" / h and (tmp_path / "pulled" / "latest").resolve() == d.resolve()
    assert pulled["pack_sha256"] == m["pack_sha256"] and _same_pack(Path(m["dir"]), d), "every byte the same"
    assert SEED.verify(d, log=lambda *a: None)["pack_sha256"] == m["pack_sha256"]
    assert not list((tmp_path / "pulled").glob(".pulling-*"))

    pinned = SEED.pull(f"s3://seeds/latest/{h}", into=tmp_path / "pinned", log=lambda *a: None)
    assert _same_pack(Path(pinned["dir"]), d), "one pack's own directory pulls the same"
    logged: list[str] = []
    again = SEED.pull("s3://seeds/latest", into=tmp_path / "pulled", log=logged.append)
    assert again["dir"] == str(d) and any("kept" in line for line in logged), "a pack already on disk that verifies is kept"

    rebuilt = tmp_path / "from-remote.duckdb"
    SEED.unpack(d, into=rebuilt, log=lambda *a: None)
    assert _counts(rebuilt)["derived.cell_feature"] == 2


def test_pull_refuses_a_tampered_object_or_manifest_and_leaves_nothing_behind(store: Path, tmp_path: Path, s3) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    h = m["pack_sha256"][:16]
    SEED.push(Path(m["dir"]), "s3://seeds/latest", log=lambda *a: None)
    raw = bytearray((Path(m["dir"]) / "derived.cell.parquet").read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    s3.put_object(Bucket="seeds", Key=f"latest/{h}/derived.cell.parquet", Body=bytes(raw))
    into = tmp_path / "pulled"
    with pytest.raises(SEED.SeedError, match="derived.cell.parquet: sha256 .* pull refused"):
        SEED.pull("s3://seeds/latest", into=into, log=lambda *a: None)
    assert not (into / h).exists() and not (into / "latest").exists() and not list(into.glob(".pulling-*"))

    edited = json.loads((Path(m["dir"]) / "manifest.json").read_text())
    edited["tables"]["derived.cell"]["rows"] = 1
    s3.put_object(Bucket="seeds", Key="latest/manifest.json", Body=json.dumps(edited).encode())
    with pytest.raises(SEED.SeedError, match="own pack_sha256"):
        SEED.pull("s3://seeds/latest", into=into, log=lambda *a: None)
    assert not list(into.iterdir()), "nothing behind"

    with pytest.raises(SEED.SeedError, match="not found"):
        SEED.pull("s3://seeds/nowhere", into=into, log=lambda *a: None)
    with pytest.raises(SEED.SeedError, match="unsupported seed URL"):
        SEED.pull("ftp://seeds/latest", into=into, log=lambda *a: None)


def test_a_push_that_stops_part_way_leaves_no_pointer_to_pull(store: Path, tmp_path: Path, s3, monkeypatch) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    h = m["pack_sha256"][:16]
    real_put = SEED._S3.put

    def failing_put(self, name: str, src: Path) -> None:
        if name.endswith("manifest.json"):
            raise RuntimeError("connection reset")
        real_put(self, name, src)

    monkeypatch.setattr(SEED._S3, "put", failing_put)
    with pytest.raises(RuntimeError, match="connection reset"):
        SEED.push(Path(m["dir"]), "s3://seeds/latest", log=lambda *a: None)
    keys = _keys(s3, "seeds")
    assert f"latest/{h}/derived.cell.parquet" in keys and "latest/manifest.json" not in keys
    monkeypatch.setattr(SEED._S3, "put", real_put)
    with pytest.raises(SEED.SeedError, match="manifest.json not found"):
        SEED.pull("s3://seeds/latest", into=tmp_path / "pulled", log=lambda *a: None)
    with pytest.raises(SEED.SeedError, match="pull only"):
        SEED.push(Path(m["dir"]), "https://example.invalid/seeds", log=lambda *a: None)
    with pytest.raises(SEED.SeedError, match="refused"):
        (Path(m["dir"]) / "derived.cell.parquet").write_bytes(b"not parquet")
        SEED.push(Path(m["dir"]), "s3://seeds/latest", log=lambda *a: None)


@pytest.fixture
def static_host(tmp_path: Path):
    """A pushed layout served by the stdlib HTTP server in a thread: <site>/latest/<hash>/... and <site>/latest/manifest.json."""
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    site = tmp_path / "site"
    site.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *a, **k: None   # type: ignore[attr-defined]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield site, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_pull_over_https_reads_the_manifest_then_every_file_and_verifies_each(store: Path, tmp_path: Path, static_host) -> None:
    import shutil

    site, base = static_host
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    h = m["pack_sha256"][:16]
    shutil.copytree(m["dir"], site / "latest" / h)
    shutil.copy(Path(m["dir"]) / "manifest.json", site / "latest" / "manifest.json")

    pulled = SEED.pull(f"{base}/latest", into=tmp_path / "pulled", log=lambda *a: None)
    assert _same_pack(Path(pulled["dir"]), Path(m["dir"])) and Path(pulled["dir"]).name == h
    pinned = SEED.pull(f"{base}/latest/{h}/", into=tmp_path / "pinned", log=lambda *a: None)
    assert _same_pack(Path(pinned["dir"]), Path(m["dir"]))

    (site / "latest" / h / "shapes" / "read.corpus_page.parquet").write_bytes(b"\x00")
    with pytest.raises(SEED.SeedError, match="shapes/read.corpus_page.parquet: sha256"):
        SEED.pull(f"{base}/latest", into=tmp_path / "bad", log=lambda *a: None)
    assert not list((tmp_path / "bad").iterdir())
    with pytest.raises(SEED.SeedError, match="not found"):
        SEED.pull(f"{base}/elsewhere", into=tmp_path / "bad", log=lambda *a: None)


def test_ensure_pulls_from_the_seed_url_when_nothing_is_on_disk(store: Path, tmp_path: Path, s3, monkeypatch) -> None:
    m = SEED.pack("public", out=tmp_path / "seed", path=store, log=lambda *a: None)
    SEED.push(Path(m["dir"]), "s3://seeds/latest", log=lambda *a: None)
    root = tmp_path / "clone-seed"
    monkeypatch.setattr(SEED, "seed_root", lambda: root)
    monkeypatch.setenv("LR_SEED_DIR", str(root / "latest"))
    target = tmp_path / "clone" / "lr.duckdb"
    monkeypatch.delenv("LR_SEED_URL", raising=False)
    assert SEED.ensure(into=target, log=lambda *a: None) == "no-seed" and not target.exists()
    monkeypatch.setenv("LR_SEED_URL", "s3://seeds/nothing-published-yet")
    assert SEED.ensure(into=target, log=lambda *a: None) == "no-seed" and not target.exists(), "an empty prefix is no seed, not a fault"
    assert not (root / "latest").exists()
    monkeypatch.setenv("LR_SEED_URL", "s3://seeds/latest")
    logged: list[str] = []
    assert SEED.ensure(into=target, log=logged.append) == "pulled" and target.is_file()
    assert (root / "latest").resolve() == (root / m["pack_sha256"][:16]).resolve(), "the pulled pack is the local pack from now on"
    assert _counts(target)["derived.cell"] == 2
    assert SEED.ensure(into=target, log=lambda *a: None) == "present"
    assert SEED.ensure(into=tmp_path / "second" / "lr.duckdb", log=lambda *a: None) == "unpacked", "the pack on disk serves the next store"
    assert not any("testing" in line for line in logged), "no credential in a log line"


# ---------------------------------------------------------------- the real-data regression (PRD C.2.4)


@pytest.mark.real_data
def test_the_quick_model_search_has_not_regressed_beyond_the_interval() -> None:
    """`lr prospect modelsearch --quick` against the store, read-only, compared with the last full search's stored
    metrics: an arm has regressed when the stored interval's low end sits above the quick run's whole interval
    (the promotion rule's "intervals apart", pointed the other way). About a minute; LR_REAL_DATA=1 enables it,
    and CI unpacks the seed pack first (`lr store seed unpack`)."""
    if os.environ.get("LR_REAL_DATA") != "1":
        pytest.skip("LR_REAL_DATA=1 to run the quick model search against the live store")
    db = db_path()
    if not db.is_file():
        pytest.skip(f"no store at {db}; `lr store seed unpack <pack>` first")
    con = connect(db, read_only=True)
    try:
        stored = {k: (v, note) for k, v, note in con.execute(
            "select metric_key, value, note from derived.metric where metric_key like 'search.%.pr_auc'").fetchall()}
    finally:
        con.close()
    if not stored:
        pytest.skip("no search.* metrics in the store to compare against; run the full model search first")
    from legacy_reader.prospect import modelsearch as MS

    out = MS.run(quick=True, log=lambda *a: None, write=False, track=False)
    compared, regressed = [], []
    for r in out["rows"]:
        key = f"search.{r['arm']}.pr_auc"
        if "pr_auc" not in r or key not in stored:
            continue
        ref_value, note = stored[key]
        m = re.search(r"95% CI ([0-9.]+)-([0-9.]+)", note or "")
        ref_lo = float(m.group(1)) if m else float(ref_value)
        new_lo, new_hi = r["pr_auc_ci"]
        compared.append(r["arm"])
        # shown with `pytest -rA` or `-s`: what was compared, not just that it passed
        print(f"  {r['arm']:<30} stored {ref_value:.3f} (low {ref_lo:.3f})   now {r['pr_auc']:.3f} [{new_lo:.3f},{new_hi:.3f}]")
        if ref_lo > new_hi:
            regressed.append(f"{r['arm']}: stored PR-AUC {ref_value:.3f} (low {ref_lo:.3f}) against {r['pr_auc']:.3f} "
                             f"[{new_lo:.3f},{new_hi:.3f}] now")
    assert compared, "the quick arms must have stored counterparts"
    assert not regressed, "regression beyond the interval:\n  " + "\n  ".join(regressed)

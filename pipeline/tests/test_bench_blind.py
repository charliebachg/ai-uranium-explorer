"""Blind-lists and passages: the radius is exact, exclusion reaches every tier, and names do not survive."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacy_reader import store as ST
from legacy_reader.bench import blind as B
from legacy_reader.bench import spec as S
from legacy_reader.prospect import retrieve as R
from legacy_reader.prospect import tools as T
from bench_store import FILES, bench_frame, make_bench_store


@pytest.fixture
def store(prospect_sandbox, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = prospect_sandbox.db
    make_bench_store(db, bench_frame(n_dep=4, n_occ=4, n_neg=4, n_probe=4))
    monkeypatch.setattr(ST, "db_path", lambda: db)
    R.load_corpus.cache_clear()
    yield db
    R.load_corpus.cache_clear()


def test_distance_is_geodesic() -> None:
    # one degree of latitude at 58 N is about 111.4 km on the ellipsoid, not the sphere's 111.2
    assert 111.3 < B.distance_km(-105.0, 58.0, -105.0, 59.0) < 111.5


def test_blind_list_keeps_files_inside_the_radius_only(store: Path) -> None:
    con = ST.connect(store, read_only=True)
    try:
        here = con.execute("select lon, lat from derived.cell where cell_id = '0000_0000'").fetchone()
        rows = con.execute("select file_num, lon, lat from native.corpus_file").fetchall()
        dist = {f: B.distance_km(here[0], here[1], lon, lat) for f, lon, lat in rows}
        inside = sorted(f for f, d in dist.items() if d <= 10.0)
        assert B.blind_list("0000_0000", 10.0, con) == inside
        assert "64L05-0060" in inside, "the file placed on the cell itself"
        # the radius is a rule: just inside is in, just outside is out
        nearest_out = min((d for d in dist.values() if d > 10.0), default=None)
        if nearest_out is not None:
            assert nearest_out not in {d for f, d in dist.items() if f in B.blind_list("0000_0000", 10.0, con)}
        assert B.blind_list("0000_0000", 100_000.0, con) == sorted(f for f, *_ in FILES)
        with pytest.raises(KeyError):
            B.blind_list("nope", 10.0, con)
    finally:
        con.close()


def test_a_read_report_with_a_collar_inside_joins_the_list(store: Path) -> None:
    con = ST.connect(store)
    try:
        here = con.execute("select lon, lat from derived.cell where cell_id = '0000_0001'").fetchone()
        con.execute("create table read.report (file_num text, split text, tier text)")
        con.execute("insert into read.report values ('74F08-0021', 'dev', 'read')")
        con.execute("create table derived.collar_position (file_num text, hole_id text, lon double, lat double, tier text)")
        con.execute("insert into derived.collar_position values ('74F08-0021', 'H1', ?, ?, 'derived')", [here[0] + 0.01, here[1]])
        assert "74F08-0021" in B.blind_list("0000_0001", 5.0, con)
        assert "74F08-0021" not in B.blind_list("0000_0001", 0.1, con)
    finally:
        con.close()


def test_retrieve_excludes_named_files_from_every_tier_and_is_unchanged_when_empty(store: Path) -> None:
    con = ST.connect(store)
    try:
        con.execute("insert into read.field_value values ('64L05-0060', 'x:64L05-0060:abc', 12, '2.4', 'm', "
                    "'2.4 metres of pitchblende', 'interval_length', 'pass', 'read')")
        here = con.execute("select lon, lat from derived.cell where cell_id = '0000_0000'").fetchone()
    finally:
        con.close()
    query = "graphitic conductor unconformity pitchblende"
    before = R.retrieve(query, lon=here[0], lat=here[1], radius_km=5000, k=10)
    assert {p.file_num for p in before} >= {"64L05-0060"} and {p.tier for p in before} == {"extracted", "page", "metadata"}
    after = R.retrieve(query, lon=here[0], lat=here[1], radius_km=5000, k=10, exclude_files=["64L05-0060"])
    assert after and all(p.file_num != "64L05-0060" for p in after)
    assert [p.cite() for p in R.retrieve(query, lon=here[0], lat=here[1], radius_km=5000, k=10, exclude_files=[])] == \
        [p.cite() for p in before]
    # with no position the exclusion still applies to the ranked corpus
    whole = R.retrieve(query, k=10, exclude_files=["64L05-0060"])
    assert whole and all(p.file_num != "64L05-0060" for p in whole)
    tool = T.retrieve(query, cell_id="0000_0000", k=10, radius_km=5000, exclude_files=["64L05-0060"])
    assert tool.args["exclude_files"] == ["64L05-0060"] and all(r["file"] != "64L05-0060" for r in tool.rows)
    assert "exclude_files" not in T.retrieve(query, cell_id="0000_0000", k=3).args


def test_passages_exclude_the_blind_list_and_scrub_names_files_holes_and_coordinates(store: Path) -> None:
    spec = S.BenchSpec(version="t", seed=1, strata=S.Strata(1, 1, 1, 1), fold_km=30, n_folds=5, held_out_share=0.2,
                       card=S.CardSpec(20, 200, ("em_conductors",)), retrieval=S.RetrievalSpec(6, 5000.0,
                       "graphitic conductor unconformity uranium mineralization alteration"))
    con = ST.connect(store, read_only=True)
    try:
        rows = B.passages("0000_0000", spec, ["64L05-0060"], con)
    finally:
        con.close()
    assert rows, "the other files are still in range"
    text = " ".join(r["text"] for r in rows)
    for leak in ("SMDC", "Cameco", "CAMECO", "McArthur", "74H09", "MAW00509", "KL-101", "MC-361", "Asamera", "Cluff", "64L05"):
        assert leak.lower() not in text.lower(), leak
    assert "[redacted]" in text
    for r in rows:
        assert set(r) == {"passage_id", "tier", "page", "distance_km", "quotable", "numbers_allowed", "text"}
        assert r["tier"] in ("page", "metadata", "extracted")
    assert not any("Cluff" in r["text"] or "Q6-1" in r["text"] for r in rows), "the excluded file is not there to scrub"


def test_passages_are_empty_where_nothing_is_written(store: Path) -> None:
    spec = S.BenchSpec(version="t", seed=1, strata=S.Strata(1, 1, 1, 1), fold_km=30, n_folds=5, held_out_share=0.2,
                       card=S.CardSpec(20, 200, ("em_conductors",)), retrieval=S.RetrievalSpec(6, 0.001, "pitchblende"))
    con = ST.connect(store, read_only=True)
    try:
        assert B.passages("0000_0015", spec, [], con) == []
    finally:
        con.close()

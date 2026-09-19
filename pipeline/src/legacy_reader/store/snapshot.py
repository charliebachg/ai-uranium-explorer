"""Snapshots and lineage: the two things "every on-screen value walks to a hashed source pull" needs.

A snapshot is a manifest of the analytics store at a moment: the file's hash, every tiered table's row count,
the pipeline version and git commit, and every native layer's pull hash and retrieval time. It is not a copy
of the file — the store is a gigabyte the machine that computed it owns — it is the record a run cites so
`--snapshot <hash>` can refuse to score a store that is not the one it names.

Lineage checks the chain itself: every native layer row carries a payload hash that matches the pull log,
every feature spec names source keys that resolve to a layer or a scene, and every inventory source marked
verified either has a store row or says it is unpulled.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..paths import PATHS
from . import TIER_BY_SCHEMA, connect, db_path

SNAPSHOT_VERSION = "snapshot/v1"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=PATHS.root, check=False)
        return out.stdout.strip() or None
    except OSError:
        return None


def snapshots_dir() -> Path:
    p = PATHS.data / "snapshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def take(log: Callable[[str], None] = print, path: Path | None = None) -> dict[str, Any]:
    """Write a snapshot manifest for the store as it is now. Returns it."""
    db = path or db_path()
    sha = _sha256(db)
    con = connect(db, read_only=True)
    try:
        tables = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema in ('native','read','derived','agent') and table_type = 'BASE TABLE' order by 1, 2").fetchall()
        counts = {f"{s}.{t}": con.execute(f"select count(*) from {s}.{t}").fetchone()[0] for s, t in tables}
        layers = con.execute("select layer_key, payload_sha256, retrieved_at, record_count from native.layer order by 1").fetchall()
        meta = con.execute("select store_version, pipeline_version, built_at from main.meta").fetchone()
    finally:
        con.close()
    manifest = {
        "version": SNAPSHOT_VERSION, "taken_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "store_sha256": sha, "store_bytes": db.stat().st_size, "store_version": meta[0] if meta else None,
        "pipeline_version": meta[1] if meta else None, "built_at": meta[2] if meta else None, "git_commit": _git_commit(),
        "tables": counts,
        "layers": {k: {"payload_sha256": p, "retrieved_at": r, "record_count": n} for k, p, r, n in layers},
    }
    out = snapshots_dir() / f"{manifest['taken_at'].replace(':', '')}-{sha[:12]}.json"
    out.write_text(json.dumps(manifest, indent=1) + "\n")
    log(f"  snapshot {sha[:12]}: {sum(counts.values()):,} rows in {len(counts)} tables, {len(layers)} layers -> {out.name}")
    return manifest


def list_snapshots() -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(snapshots_dir().glob("*.json"))]


def verify(snapshot_hash: str, path: Path | None = None) -> dict[str, Any]:
    """The named snapshot, if the store on disk is still the store it describes; raises otherwise."""
    matches = [s for s in list_snapshots() if s["store_sha256"].startswith(snapshot_hash)]
    if not matches:
        raise FileNotFoundError(f"no snapshot starting with {snapshot_hash!r} under {snapshots_dir()}")
    snap = matches[-1]
    now = _sha256(path or db_path())
    if now != snap["store_sha256"]:
        raise RuntimeError(f"the store on disk ({now[:12]}) is not snapshot {snap['store_sha256'][:12]}; "
                           "a run against it would not be the run the snapshot names")
    return snap


def register_layers(log: Callable[[str], None] = print) -> dict[str, int]:
    """Every pulled layer as a native.layer row with the hash of what was pulled.

    The first rebuild registered the eleven Phase 1 layers without payload hashes, and the ten layers pulled
    for the prospect features were never registered at all although features cite them. This walks the pull
    log, hashes each payload file, inserts the missing rows and fills the missing hashes."""
    from ..index import load_pull_log
    from ..prospect.inventory import load as load_inventory

    pulls = load_pull_log()
    inv = {s.key: s for s in load_inventory().sources}
    bears_default = {"survey_footprints_ground": "effort", "survey_footprints_airborne": "effort"}
    con = connect()
    try:
        # the first rebuild created this table from a frame whose hash column was all null, so DuckDB typed it
        # INT32; the schema says text. Put the declared type back before writing a hash into it.
        for col, want in (("payload_sha256", "VARCHAR"), ("where_clause", "VARCHAR"), ("licence_url", "VARCHAR"),
                          ("bears_on", "VARCHAR"), ("notes", "VARCHAR")):
            typ = con.execute("select data_type from information_schema.columns where table_schema='native' "
                              "and table_name='layer' and column_name=?", [col]).fetchone()
            if typ and typ[0] != want:
                con.execute(f"alter table native.layer alter {col} type {want}")
                log(f"  native.layer.{col}: type {typ[0]} -> {want} (the schema's declaration)")
        # a table the rebuild created from a frame has no column defaults, so the tier is written explicitly
        con.execute("update native.layer set tier = 'native' where tier is null")
        have = {r[0]: r[1] for r in con.execute("select layer_key, payload_sha256 from native.layer").fetchall()}
        inserted = hashed = 0
        for key, pull in pulls.items():
            path = PATHS.pipeline / pull["path"]   # pull-log paths are relative to pipeline/
            if not path.is_file():
                log(f"  {key}: payload {pull['path']} not on disk; skipped")
                continue
            sha = _sha256(path)
            if key in have:
                if not have[key]:
                    con.execute("update native.layer set payload_sha256 = ? where layer_key = ?", [sha, key])
                    hashed += 1
                continue
            src = inv.get(key)
            con.execute(
                "insert into native.layer (layer_key, title, service_url, layer_id, where_clause, out_sr, record_count, "
                "payload_sha256, licence, licence_url, redistributable, retrieved_at, bears_on, role, notes, tier) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'native')",
                [key, pull.get("title") or key, pull["url"], None, pull.get("where"), None, int(pull.get("count") or 0), sha,
                 pull.get("licence") or (src.licence.name if src else "not stated"), pull.get("licence_url"),
                 bool(pull.get("redistributable")), pull["retrieved_at"],
                 (src.bears_on if src else bears_default.get(key)), (src.role if src else "feature"),
                 f"registered from the pull log on {dt.datetime.now(dt.UTC).date().isoformat()}"])
            inserted += 1
    finally:
        con.close()
    log(f"  native.layer: {inserted} layer(s) registered, {hashed} payload hash(es) filled")
    return {"inserted": inserted, "hashed": hashed}


def lineage(log: Callable[[str], None] = print) -> list[str]:
    """Problems in the provenance chain; empty means every value can walk back to a hashed pull."""
    from ..index import load_pull_log
    from ..prospect.inventory import load as load_inventory

    problems: list[str] = []
    pulls = load_pull_log()
    con = connect(read_only=True)
    try:
        layers = {r[0]: {"sha": r[1], "at": r[2], "n": r[3]} for r in
                  con.execute("select layer_key, payload_sha256, retrieved_at, record_count from native.layer").fetchall()}
        norm = lambda k: re.sub(r"[^a-z0-9]", "", str(k).lower())  # 'sentinel-2-l2a' and 'sentinel2_l2a' are one thing
        scenes = {norm(r[0]) for r in con.execute("select distinct collection from native.scene").fetchall()}
        specs = con.execute("select feature_key, from_tier, source_keys from derived.feature_spec").fetchall()
    finally:
        con.close()
    for key, row in layers.items():
        if not row["sha"]:
            problems.append(f"native.layer {key}: no payload hash")
        pull = pulls.get(key)
        if pull is None:
            problems.append(f"native.layer {key}: no entry in the pull log")
        elif pull.get("count") not in (None, row["n"]):
            problems.append(f"native.layer {key}: pull log count {pull.get('count')} differs from store {row['n']}")
    known = {norm(k) for k in layers} | scenes
    for feature_key, from_tier, source_keys in specs:
        keys = json.loads(source_keys or "[]")
        if not keys:
            problems.append(f"feature_spec {feature_key}: no source keys")
        for k in keys:
            if norm(k) not in known:
                problems.append(f"feature_spec {feature_key}: source {k!r} is not a layer or scene in the store")
    inv = load_inventory()
    urls = {p.get("url", "") for p in pulls.values()}
    for src in inv.sources:
        pulled = src.key in layers or any(u.startswith(src.url) for u in urls if src.url)
        if src.role == "feature" and src.verified and src.record_count and not pulled:
            problems.append(f"inventory {src.key}: verified feature source with {src.record_count} records but no store layer")
    for p in problems:
        log("  " + p)
    log("  lineage clean" if not problems else f"  {len(problems)} lineage problem(s)")
    return problems

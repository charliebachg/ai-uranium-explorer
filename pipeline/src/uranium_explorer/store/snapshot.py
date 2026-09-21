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
from . import connect, db_path

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
            "where table_schema in ('native','read','derived','agent','expert') and table_type = 'BASE TABLE' order by 1, 2").fetchall()
        counts = {f"{s}.{t}": con.execute(f"select count(*) from {s}.{t}").fetchone()[0] for s, t in tables}
        layers = con.execute("select layer_key, payload_sha256, retrieved_at, record_count from native.layer order by 1").fetchall()
        meta = con.execute("select store_version, pipeline_version, built_at from main.meta").fetchone()
        features = feature_summary(con)
    finally:
        con.close()
    manifest = {
        "version": SNAPSHOT_VERSION, "taken_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "store_sha256": sha, "store_bytes": db.stat().st_size, "store_version": meta[0] if meta else None,
        "pipeline_version": meta[1] if meta else None, "built_at": meta[2] if meta else None, "git_commit": _git_commit(),
        "tables": counts,
        "layers": {k: {"payload_sha256": p, "retrieved_at": r, "record_count": n} for k, p, r, n in layers},
        "features": features,
    }
    out = snapshots_dir() / f"{manifest['taken_at'].replace(':', '')}-{sha[:12]}.json"
    out.write_text(json.dumps(manifest, indent=1) + "\n")
    log(f"  snapshot {sha[:12]}: {sum(counts.values()):,} rows in {len(counts)} tables, {len(layers)} layers -> {out.name}")
    return manifest


QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def feature_summary(con: Any) -> dict[str, dict[str, Any]]:
    """Per feature: how many cells carry a value, how many an observation, and five quantiles of the values.

    This is what a later drift check compares against: a source refreshed under the same key should move the
    distribution little, and a large move is a question for a person before any model is refit."""
    try:
        rows = con.execute(
            "select feature_key, count(*) filter (where value is not null) as n, "
            "count(*) filter (where n_obs > 0) as with_obs, "
            + ", ".join(f"quantile_cont(value, {q}) as q{int(q * 100)}" for q in QUANTILES)
            + " from derived.cell_feature group by 1 order by 1").fetchall()
    except Exception:  # noqa: BLE001 - no feature table in this store
        return {}
    out = {}
    for key, n, with_obs, *qs in rows:
        out[key] = {"n": int(n), "with_obs": int(with_obs),
                    "q": [None if v is None else round(float(v), 6) for v in qs]}
    return out


def drift(snapshot_hash: str | None = None, path: Path | None = None,
          log: Callable[[str], None] = print) -> dict[str, Any]:
    """Feature distributions now against the ones a snapshot recorded (the drift check).

    For every feature both sides know, the shift of each quantile is measured in units of the snapshot's
    interquartile range; a feature drifts when its median moves more than a quarter of that range, when a
    quarter of its cells appear or disappear, or when it exists on one side only. Constant features (an IQR of
    zero) drift on any change. Nothing here refits anything: the verdict is a list for a person."""
    snaps = list_snapshots()
    if snapshot_hash:
        matches = [s for s in snaps if s["store_sha256"].startswith(snapshot_hash)]
        if not matches:
            raise FileNotFoundError(f"no snapshot starting with {snapshot_hash!r}")
        snap = matches[-1]
    else:
        snap = latest()
        if snap is None:
            raise FileNotFoundError("no snapshot to compare against; run `ue store snapshot` first")
    before = snap.get("features") or {}
    con = connect(path or db_path(), read_only=True)
    try:
        now = feature_summary(con)
    finally:
        con.close()
    rows = compare_features(before, now)
    for r in rows:
        if r["drifted"]:
            log(f"  {r['feature_key']:<24} {r['why']}")
    drifted = [r for r in rows if r["drifted"]]
    log(f"  drift against snapshot {snap['store_sha256'][:12]} ({snap['taken_at']}): "
        f"{len(drifted)} of {len(rows)} feature(s) drifted"
        + ("" if before else "; that snapshot recorded no feature summary, so every feature is new to it"))
    return {"snapshot": snap["store_sha256"][:12], "taken_at": snap["taken_at"], "rows": rows,
            "drifted": [r["feature_key"] for r in drifted]}


def compare_features(before: dict[str, dict[str, Any]], now: dict[str, dict[str, Any]],
                     median_shift: float = 0.25, count_shift: float = 0.25) -> list[dict[str, Any]]:
    """Pure: one row per feature on either side, with the shift measured and the drift decision made."""
    rows = []
    for key in sorted(set(before) | set(now)):
        b, n = before.get(key), now.get(key)
        if b is None or n is None:
            rows.append({"feature_key": key, "drifted": True, "why": "only in the snapshot" if n is None else "new since the snapshot",
                         "median_shift_iqr": None, "count_change": None})
            continue
        bq, nq = b.get("q") or [], n.get("q") or []
        count_change = (n["n"] - b["n"]) / b["n"] if b.get("n") else (1.0 if n.get("n") else 0.0)
        shift = None
        why = []
        if len(bq) >= 5 and len(nq) >= 5 and bq[2] is not None and nq[2] is not None:
            iqr = (bq[3] or 0.0) - (bq[1] or 0.0)
            delta = (nq[2] or 0.0) - (bq[2] or 0.0)
            if iqr > 0:
                shift = delta / iqr
                if abs(shift) > median_shift:
                    why.append(f"median moved {shift:+.2f} IQR")
            elif delta != 0:
                shift = float("inf") if delta > 0 else float("-inf")
                why.append("a constant feature changed value")
        if abs(count_change) > count_shift:
            why.append(f"cells with a value changed {count_change:+.0%}")
        rows.append({"feature_key": key, "drifted": bool(why), "why": "; ".join(why) or "stable",
                     "median_shift_iqr": None if shift is None or shift != shift or abs(shift) == float("inf") else round(shift, 3),
                     "count_change": round(count_change, 4)})
    return rows


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


def store_sha(path: Path | None = None) -> str | None:
    """The sha256 of the store file as it is now; None when there is no store file to hash."""
    db = path or db_path()
    return _sha256(db) if db.is_file() else None


def latest() -> dict[str, Any] | None:
    """The most recently taken snapshot manifest, or None when none has been taken."""
    snaps = list_snapshots()
    return max(snaps, key=lambda s: s.get("taken_at") or "") if snaps else None


def pin(snapshot: str | None = None, path: Path | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """What an evaluation run names: the store's hash now, and the snapshot that hash is.

    With `snapshot` given, the store on disk must be the store that snapshot describes, or the run does not
    start: `verify` raises FileNotFoundError when no snapshot matches and RuntimeError when the store differs.
    Without it the store is hashed and, if a snapshot manifest carries that exact hash, the run cites it; the
    run is then reproducible but was not pinned, and `pinned` says so. No store file at all hashes to None,
    which is what a run on an injected frame or an empty checkout records."""
    if snapshot:
        snap = verify(snapshot, path=path)
        sha = snap["store_sha256"]
        log(f"  store {sha[:12]} (snapshot {sha[:12]}, pinned)")
        return {"store_sha256": sha, "snapshot": sha[:12], "pinned": True}
    sha = store_sha(path)
    if sha is None:
        log("  store: no store file to hash; the run names no snapshot")
        return {"store_sha256": None, "snapshot": None, "pinned": False}
    named = any(s.get("store_sha256") == sha for s in list_snapshots())
    log(f"  store {sha[:12]} (snapshot {sha[:12]})" if named else f"  store {sha[:12]} (no snapshot names it)")
    return {"store_sha256": sha, "snapshot": sha[:12] if named else None, "pinned": False}


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
    for feature_key, _from_tier, source_keys in specs:
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

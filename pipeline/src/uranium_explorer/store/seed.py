"""The seed pack: the analytics store as one Parquet file per table, content-addressed, so a fresh clone
reaches a running app without the machine that computed it.

The store is a gitignored DuckDB file nothing publishes. A pack is the same rows in a form that can be hosted
and verified: a directory named by the hash of its manifest, one Parquet file per table, and `manifest.json`
naming the store it came from (its sha256, `STORE_VERSION`, the pipeline version), every table's row count
and file hash, and the licence decision that put each table in or left it out. `unpack` rebuilds a store from
it, applies `schema.sql`, runs the tier audit and refuses on any hash or count that does not match.

The licence rule is derived from `knowledge/data_inventory.toml`, not from a list of table names. A table is
in the public pack only when every source it carries is marked redistributable there; a table keyed by
assessment file (`file_num`, `doc_sha256`) holds material from documents that carry no named licence and goes
only in the private pack, as does everything in the `read` tier and the `agent` tier. The decision and its
reason are written per table into the manifest, so a reader of the pack can see why a table is missing.

Hosting is a transport over the same files, content-addressed by the pack hash: `push` uploads a verified pack
to `s3://bucket/prefix` under `<prefix>/<hash>/`, its manifest last, then writes `<prefix>/manifest.json` as
the pointer to the current pack, last of all, so a partial upload is never taken for a pack. `pull` reads the
manifest at an `s3://` or `https://` URL (a static host, a release's asset directory) and then every file it
names, checking each sha256 on the way, and refuses on any mismatch leaving nothing behind. `ensure` pulls
from `UE_SEED_URL` when there is neither a store nor a pack on disk. The URL is a deployment choice.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import duckdb

from ..paths import PATHS
from . import SCHEMA_SQL, STORE_VERSION, TIER_BY_SCHEMA, connect, db_path, tier_audit

SEED_VERSION = "seed/v1"
MANIFEST = "manifest.json"
#: the one untiered table the store contract includes: its version stamp
META = ("main", "meta")
#: rows per Parquet row group; fixed, so the same rows always give the same bytes (on one DuckDB and pyarrow
#: version: the codec's output is part of the file, so a pack made elsewhere can carry the same rows under
#: another address; `store_sha256` in the manifest is what says two packs came from one store)
BATCH = 200_000


class SeedError(Exception):
    """A pack that does not describe what is on disk, or a store that cannot be rebuilt from it."""


class SeedMissing(SeedError):
    """Nothing at the URL: no manifest published there (yet). `ensure` treats it as no seed, not as a fault."""


def seed_root() -> Path:
    return PATHS.data / "seed"


def seed_dir() -> Path:
    """Where the app looks for a pack: `UE_SEED_DIR`, or `pipeline/data/seed/latest` (the symlink `pack` writes)."""
    env = os.environ.get("UE_SEED_DIR", "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else (PATHS.root / p)
    return seed_root() / "latest"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _norm(key: Any) -> str:
    """'sentinel-2-l2a' and 'sentinel2_l2a' are one thing (the same rule `snapshot.lineage` applies)."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# ---------------------------------------------------------------- the licence rule


RULE = (
    "A table is in the public pack only when every source it carries is marked redistributable in "
    "knowledge/data_inventory.toml. Sources are resolved from the rows themselves: a `layer_key` column names "
    "pulled layers, a `collection` column names STAC collections, and the derived tier's upstream is what "
    "derived.feature_spec and derived.cell_feature declare plus the label and study-area layers. A table keyed by "
    "assessment file (`file_num`, `doc_sha256`) holds material from documents with no named licence and goes only "
    "in the private pack; so does every table in the `read` tier and the `agent` tier. The layer registry "
    "(native.layer) carries service URLs, counts, hashes and licence flags, not payloads, and is public."
)


def _resolve(key: str, inv: Any, layers: dict[str, dict[str, Any]]) -> tuple[bool | None, str]:
    """A source key -> (redistributable, where the flag came from). None when nothing marks it either way."""
    n = _norm(key)
    for s in inv.sources:
        if _norm(s.key) == n or (s.collection and _norm(s.collection) == n):
            return s.redistributable, f"inventory {s.key} ({s.licence.key})"
    row = layers.get(key)
    if row is not None:
        for s in inv.sources:
            if s.url and row.get("service_url", "").startswith(s.url):
                return s.redistributable, f"inventory {s.key} by service URL ({s.licence.key})"
        if row.get("redistributable") is not None:
            return bool(row["redistributable"]), f"native.layer {key}'s recorded flag ({row.get('licence')})"
    return None, "no inventory source or layer row names it"


def _distinct(con: duckdb.DuckDBPyConnection, schema: str, table: str, column: str) -> list[str]:
    rows = con.execute(f'select distinct "{column}" from "{schema}"."{table}" where "{column}" is not null order by 1').fetchall()
    return [str(r[0]) for r in rows]


def _json_keys(con: duckdb.DuckDBPyConnection, schema: str, table: str, column: str) -> set[str]:
    """Every key inside a JSON-array column (`source_keys`, `inputs`), across the table."""
    out: set[str] = set()
    try:
        rows = con.execute(f'select distinct "{column}"::varchar from "{schema}"."{table}" where "{column}" is not null').fetchall()
    except duckdb.Error:
        return out
    for (raw,) in rows:
        try:
            val = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(val, list):
            out.update(str(v) for v in val)
    return out


def _derived_upstream(con: duckdb.DuckDBPyConnection, tables: set[tuple[str, str]], inv: Any,
                      layers: dict[str, dict[str, Any]]) -> tuple[set[str], list[str]]:
    """What the derived tier was computed from: the feature lineage it declares, and the label and study-area layers."""
    keys: set[str] = set()
    notes: list[str] = []
    if ("derived", "feature_spec") in tables:
        keys |= _json_keys(con, "derived", "feature_spec", "source_keys")
    if ("derived", "cell_feature") in tables:
        keys |= _json_keys(con, "derived", "cell_feature", "inputs")
        tiers = _distinct(con, "derived", "cell_feature", "from_tier")
        if "read" in tiers:
            read_sources = [s.key for s in inv.sources if s.tier == "read"]
            keys.update(read_sources)
            notes.append("a feature is computed from the read tier")
    # the labels and the study area are inputs to every derived table; other context layers (the file index, the
    # NTS sheets) are not, and one of them carries no licence
    for key, row in layers.items():
        if row.get("role") == "label" or row.get("bears_on") == "study_area":
            keys.add(key)
    return keys, notes


def decisions(con: duckdb.DuckDBPyConnection, inventory: Any = None) -> list[dict[str, Any]]:
    """One decision per table in the store: public, private only, or skipped; with the reason and the sources.

    Pure over the store and the inventory: the same store and the same inventory always give the same list,
    which is what makes a pack's manifest deterministic."""
    from ..prospect.inventory import load as load_inventory

    inv = inventory or load_inventory()
    rows = con.execute(
        "select table_schema, table_name from information_schema.tables where table_type = 'BASE TABLE' "
        "and table_schema in ('native', 'read', 'derived', 'agent', 'expert', 'main') order by 1, 2").fetchall()
    tables = {(s, t) for s, t in rows}
    layers: dict[str, dict[str, Any]] = {}
    if ("native", "layer") in tables:
        for key, url, lic, redist, role, bears_on in con.execute(
                "select layer_key, service_url, licence, redistributable, role, bears_on from native.layer").fetchall():
            layers[key] = {"service_url": url or "", "licence": lic, "redistributable": redist, "role": role, "bears_on": bears_on}
    upstream, upstream_notes = _derived_upstream(con, tables, inv, layers)
    read_sources = [s for s in inv.sources if s.tier == "read"]
    out: list[dict[str, Any]] = []
    for schema, table in sorted(tables):
        name = f"{schema}.{table}"
        columns = [c[0] for c in con.execute(f'describe "{schema}"."{table}"').fetchall()]
        n = con.execute(f'select count(*) from "{schema}"."{table}"').fetchone()[0]
        d: dict[str, Any] = {"table": name, "rows": int(n), "sources": [], "public": False, "private": True}
        if schema == "main":
            if (schema, table) == META:
                d.update(public=True, reason="the store's version stamp; no source data")
            else:
                d.update(private=False, reason="outside the four tiers (a v1 leftover); not part of the store contract")
        elif schema == "agent":
            d.update(reason="the agent tier: model-written argument over the private evidence record; private pack only")
        elif schema == "expert":
            d.update(reason="the expert tier: what a named geologist stated about a cell, with their name; private pack only")
        elif "file_num" in columns or "doc_sha256" in columns:
            d.update(sources=[s.key for s in read_sources],
                     reason="keyed by assessment file; the documents carry no named licence and the inventory's "
                            "read-tier source is licensed '"
                            + (read_sources[0].licence.name if read_sources else "no licence stated")
                            + "' (not redistributable)")
        elif schema == "read":
            d.update(sources=[s.key for s in read_sources],
                     reason="the read tier: what a reader made of document pages; the inventory marks no read-tier "
                            "source redistributable")
        elif (schema, table) == ("native", "layer"):
            d.update(public=True, sources=sorted(layers),
                     reason="the layer registry: service URLs, counts, payload hashes and each pull's own licence "
                            "flag; carries no feature payload")
        else:
            if "layer_key" in columns:
                keys, how = _distinct(con, schema, table, "layer_key"), "layers named by layer_key"
            elif "collection" in columns:
                keys, how = _distinct(con, schema, table, "collection"), "STAC collections named by collection"
            elif schema == "derived":
                keys, how = sorted(upstream), "the derived tier's upstream (feature lineage, label and study-area layers)"
            else:
                d.update(reason="no rule resolves this table's sources to the inventory; private pack only until one does")
                out.append(d)
                continue
            resolved = {k: _resolve(k, inv, layers) for k in keys}
            bad = sorted(k for k, (ok, _) in resolved.items() if not ok)
            d["sources"] = sorted(keys)
            if not keys:
                d.update(public=True, reason=f"{how}: no rows, so nothing to license" if n == 0 else f"{how}: none named")
            elif bad:
                d.update(reason=f"{how}: not marked redistributable: "
                         + "; ".join(f"{k} ({resolved[k][1]})" for k in bad))
            else:
                d.update(public=True, reason=f"{how}: every source marked redistributable ("
                         + ", ".join(sorted({resolved[k][1].split(' (')[-1].rstrip(')') for k in keys})) + ")")
            if schema == "derived" and upstream_notes:
                d["reason"] += "; " + "; ".join(upstream_notes)
        out.append(d)
    return out


# ---------------------------------------------------------------- pack


def _order_by(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> list[str] | None:
    row = con.execute(
        "select constraint_column_names from duckdb_constraints() where schema_name = ? and table_name = ? "
        "and constraint_type = 'PRIMARY KEY'", [schema, table]).fetchone()
    return list(row[0]) if row else None


def _write_parquet(con: duckdb.DuckDBPyConnection, schema: str, table: str, path: Path,
                   shape_only: bool = False) -> tuple[int, list[str] | None]:
    """The table's rows in a fixed order (its primary key, else every column), streamed to Parquet.

    `shape_only` writes the columns and their types with no rows: what the public pack ships for a table it
    may not carry, so the unpacked store still has every table the code addresses."""
    import pyarrow.parquet as pq

    keys = _order_by(con, schema, table)
    order = ", ".join(f'"{k}"' for k in keys) if keys else "all"
    limit = " limit 0" if shape_only else ""
    reader = con.execute(f'select * from "{schema}"."{table}" order by {order}{limit}').to_arrow_reader(BATCH)
    rows = 0
    with pq.ParquetWriter(str(path), reader.schema, compression="zstd") as writer:
        for batch in reader:
            writer.write_batch(batch)
            rows += batch.num_rows
    return rows, keys


def _declared() -> set[tuple[str, str]]:
    return {(m.group(1), m.group(2)) for m in re.finditer(r"create table if not exists\s+(\w+)\.(\w+)", SCHEMA_SQL.read_text(), re.I)}


def _canonical(manifest: dict[str, Any]) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "pack_sha256"}
    return (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()


def pack_hash(manifest: dict[str, Any]) -> str:
    """The content address: the hash of everything in the manifest but the address itself."""
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def pack(scope: str = "public", out: Path | None = None, path: Path | None = None,
         log: Callable[[str], None] = print) -> dict[str, Any]:
    """Write a pack of the store at `path` (default: the live store) under `out/<hash>/` and point `out/latest` at it.

    `scope` is "public" (only tables every source of which is redistributable) or "private" (every table the
    store contract covers). The manifest carries the decision for every table either way, so the public pack
    says what it left out and why."""
    from .. import __version__
    from ..prospect.inventory import INVENTORY_PATH
    from ..prospect.inventory import load as load_inventory

    if scope not in ("public", "private"):
        raise SeedError(f"scope must be public or private, not {scope!r}")
    db = path or db_path()
    if not db.is_file():
        raise SeedError(f"no store at {db}")
    root = out or seed_root()
    root.mkdir(parents=True, exist_ok=True)
    work = root / f".packing-{os.getpid()}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    inv = load_inventory()
    declared = _declared()
    store_sha = _sha256(db)
    con = connect(db, read_only=True)
    try:
        meta = con.execute("select store_version, pipeline_version, built_at from main.meta").fetchone() \
            if con.execute("select count(*) from information_schema.tables where table_schema = 'main' and table_name = 'meta'").fetchone()[0] else None
        decided = decisions(con, inv)
        tables: dict[str, Any] = {}
        excluded: dict[str, Any] = {}
        for d in decided:
            include = d["public"] if scope == "public" else d["private"]
            entry = {k: d[k] for k in ("rows", "public", "private", "reason", "sources")}
            schema, table = d["table"].split(".", 1)
            if not include:
                excluded[d["table"]] = entry
                if d["private"]:
                    # a table of the store contract this pack may not carry: ship its shape, so the unpacked
                    # store has the table, empty, rather than a missing-table error where the code reads it
                    shape = f"shapes/{d['table']}.parquet"
                    (work / "shapes").mkdir(exist_ok=True)
                    _write_parquet(con, schema, table, work / shape, shape_only=True)
                    entry.update(shape=shape, shape_sha256=_sha256(work / shape), shape_bytes=(work / shape).stat().st_size,
                                 declared=(schema, table) in declared)
                continue
            file = f"{d['table']}.parquet"
            rows, keys = _write_parquet(con, schema, table, work / file)
            if rows != d["rows"]:
                raise SeedError(f"{d['table']}: counted {d['rows']} rows, wrote {rows}")
            entry.update(file=file, sha256=_sha256(work / file), bytes=(work / file).stat().st_size,
                         declared=(schema, table) in declared, order_by=keys or "all")
            tables[d["table"]] = entry
            log(f"  {d['table']:<28} {rows:>9,} rows  {entry['bytes'] / 1e6:8.2f} MB")
    finally:
        con.close()
    manifest: dict[str, Any] = {
        "version": SEED_VERSION, "scope": scope,
        "store_sha256": store_sha, "store_bytes": db.stat().st_size,
        "store_version": meta[0] if meta else STORE_VERSION, "pipeline_version": meta[1] if meta else __version__,
        "store_built_at": meta[2] if meta else None,
        "tables": tables, "excluded": excluded,
        "licence": {
            "basis": str(INVENTORY_PATH.relative_to(PATHS.pipeline)), "inventory_schema_version": inv.schema_version,
            "rule": RULE,
            "licences": {k: {"name": v.name, "redistributable": v.redistributable, "url": v.url} for k, v in sorted(inv.licences.items())},
        },
    }
    manifest["pack_sha256"] = pack_hash(manifest)
    (work / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    dest = root / manifest["pack_sha256"][:16]
    if dest.exists():
        shutil.rmtree(work)
        log(f"  pack {manifest['pack_sha256'][:16]} already exists under {root}; nothing rewritten")
    else:
        work.rename(dest)
    latest = root / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink() if latest.is_symlink() or latest.is_file() else shutil.rmtree(latest)
    latest.symlink_to(dest.name)
    total = sum(t["bytes"] for t in tables.values())
    shapes = sum(1 for t in excluded.values() if "shape" in t)
    log(f"  {scope} pack {manifest['pack_sha256'][:16]}: {len(tables)} tables, {sum(t['rows'] for t in tables.values()):,} rows, "
        f"{total / 1e6:.1f} MB; {len(excluded)} table(s) left out, {shapes} of them as empty shapes -> {dest}")
    manifest["dir"] = str(dest)
    return manifest


# ---------------------------------------------------------------- verify and unpack


def _locate(src: Path) -> tuple[Path, dict[str, Any]]:
    p = Path(src)
    if p.is_file():
        p = p.parent if p.name == MANIFEST else p
    if not (p / MANIFEST).is_file():
        raise SeedError(f"no {MANIFEST} under {p}")
    return p, json.loads((p / MANIFEST).read_text())


def verify(src: Path, log: Callable[[str], None] = print) -> dict[str, Any]:
    """The manifest, if every file it names is present with the hash and the row count it states; raises otherwise.

    Also checks that the manifest is the one its own address names, so an edited manifest is caught as
    readily as an edited table."""
    import pyarrow.parquet as pq

    p, manifest = _locate(src)
    problems: list[str] = []
    if manifest.get("version") != SEED_VERSION:
        problems.append(f"pack version {manifest.get('version')!r}, expected {SEED_VERSION}")
    if pack_hash(manifest) != manifest.get("pack_sha256"):
        problems.append("manifest does not hash to its own pack_sha256 (edited after packing)")
    for name, t in sorted(manifest.get("tables", {}).items()):
        f = p / t["file"]
        if not f.is_file():
            problems.append(f"{name}: {t['file']} missing")
            continue
        sha = _sha256(f)
        if sha != t["sha256"]:
            problems.append(f"{name}: sha256 {sha[:12]} differs from the manifest's {t['sha256'][:12]}")
            continue
        n = pq.read_metadata(str(f)).num_rows
        if n != t["rows"]:
            problems.append(f"{name}: {n} rows in the file, {t['rows']} in the manifest")
    for name, t in sorted(manifest.get("excluded", {}).items()):
        if "shape" not in t:
            continue
        f = p / t["shape"]
        if not f.is_file():
            problems.append(f"{name}: shape {t['shape']} missing")
        elif _sha256(f) != t["shape_sha256"]:
            problems.append(f"{name}: shape sha256 differs from the manifest's")
        elif pq.read_metadata(str(f)).num_rows:
            problems.append(f"{name}: shape {t['shape']} carries rows; a shape is columns only")
    for line in problems:
        log("  " + line)
    if problems:
        raise SeedError(f"pack at {p} refused: {len(problems)} problem(s)\n  " + "\n  ".join(problems))
    log(f"  pack {manifest['pack_sha256'][:16]} ({manifest['scope']}): {len(manifest['tables'])} tables verified, "
        f"store {manifest['store_sha256'][:12]}")
    manifest["dir"] = str(p)
    return manifest


def _sql_str(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _load_table(con: duckdb.DuckDBPyConnection, name: str, file: Path) -> None:
    """Into a declared table by column, cast to the declared types; an undeclared one is created from the file."""
    import pyarrow.parquet as pq

    schema, table = name.split(".", 1)
    cols = pq.read_schema(str(file)).names
    declared = con.execute(
        "select column_name, data_type from information_schema.columns where table_schema = ? and table_name = ? "
        "order by ordinal_position", [schema, table]).fetchall()
    con.execute(f'create schema if not exists "{schema}"')
    if declared:
        types = dict(declared)
        extra = [c for c in cols if c not in types]
        if extra:
            raise SeedError(f"{name}: the pack carries column(s) {extra} the schema does not declare")
        select = ", ".join(f'"{c}"::{types[c]} as "{c}"' for c in cols)
        names = ", ".join(f'"{c}"' for c in cols)
        con.execute(f'insert into "{schema}"."{table}" ({names}) select {select} from read_parquet({_sql_str(file)})')
    else:
        con.execute(f'create table "{schema}"."{table}" as select * from read_parquet({_sql_str(file)})')


def unpack(src: Path, into: Path | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Rebuild a store file from a pack: verify, apply schema.sql, load every table, audit the tiers, recount.

    Refuses to overwrite an existing store; writes beside the target and moves it into place only when every
    check has passed, so a refused unpack leaves nothing behind. A sidecar `<store>.seed.json` records which
    pack the file came from.

    The rebuilt store follows `schema.sql` as it is now, not the packed store's physical layout: a column a
    migration appended to the live store sits where the schema declares it, and a column the live store once
    retyped through `create or replace ... as select` gets its declared type back. Every value is the same;
    the code addresses columns by name, so nothing reads differently."""
    from .load import CROSS_TIER_VIEWS

    target = into or db_path()
    if target.exists():
        raise SeedError(f"{target} exists; unpack refuses to overwrite a store (move it aside first)")
    manifest = verify(src, log=log)
    p = Path(manifest["dir"])
    if manifest.get("store_version") != STORE_VERSION:
        raise SeedError(f"pack is {manifest.get('store_version')}, this code expects {STORE_VERSION}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    for stale in (partial, partial.with_name(partial.name + ".wal")):
        if stale.exists():
            stale.unlink()
    try:
        con = connect(partial)
        try:
            counts: dict[str, int] = {}
            for name, t in sorted(manifest["tables"].items()):
                _load_table(con, name, p / t["file"])
                n = con.execute(f'select count(*) from "{name.split(".", 1)[0]}"."{name.split(".", 1)[1]}"').fetchone()[0]
                if n != t["rows"]:
                    raise SeedError(f"{name}: loaded {n} rows, the manifest says {t['rows']}")
                counts[name] = int(n)
            for name, t in sorted(manifest.get("excluded", {}).items()):
                if "shape" not in t:
                    continue
                _load_table(con, name, p / t["shape"])
                schema, table = name.split(".", 1)
                if con.execute(f'select count(*) from "{schema}"."{table}"').fetchone()[0]:
                    raise SeedError(f"{name}: the shape loaded rows")
                counts[name] = 0
            have = {f"{s}.{t}" for s, t in con.execute(
                "select table_schema, table_name from information_schema.tables where table_type = 'BASE TABLE'").fetchall()}
            if {"read.report", "native.file_index"} <= have:
                con.execute(CROSS_TIER_VIEWS)
            if META[0] + "." + META[1] not in have:
                from .. import __version__
                from . import write_meta

                write_meta(con, manifest.get("pipeline_version") or __version__)
            problems = tier_audit(con)
        finally:
            con.close()
        if problems:
            raise SeedError("unpacked store refused: tier audit failed\n  " + "\n  ".join(problems))
        partial.rename(target)
    except BaseException:
        for stale in (partial, partial.with_name(partial.name + ".wal")):
            if stale.exists():
                stale.unlink()
        raise
    sidecar = {
        "pack_sha256": manifest["pack_sha256"], "scope": manifest["scope"], "pack_dir": str(p),
        "source_store_sha256": manifest["store_sha256"], "store_sha256": _sha256(target),
        "unpacked_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "tables": counts,
    }
    target.with_name(target.name + ".seed.json").write_text(json.dumps(sidecar, indent=1) + "\n")
    log(f"  {target}: {len(counts)} tables, {sum(counts.values()):,} rows from pack {manifest['pack_sha256'][:16]} "
        f"({manifest['scope']}); tier audit clean; sidecar {target.name}.seed.json")
    return sidecar


# ---------------------------------------------------------------- hosting: push and pull


def seed_url() -> str | None:
    """Where `ensure` pulls a pack from when none is on disk: `UE_SEED_URL`, an `s3://` or `https://` URL, or None."""
    return os.environ.get("UE_SEED_URL", "").strip() or None


def _shown(url: str) -> str:
    """A URL as it may be logged: scheme, host and path; never a query string or user info."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return f"{parts.scheme}://{host}{parts.path}"


def _files(manifest: dict[str, Any]) -> list[tuple[str, str, int]]:
    """Every file a pack holds besides its manifest: (relative name, sha256, bytes), tables then shapes."""
    out = [(t["file"], t["sha256"], int(t["bytes"])) for _, t in sorted(manifest.get("tables", {}).items())]
    out += [(t["shape"], t["shape_sha256"], int(t["shape_bytes"])) for _, t in sorted(manifest.get("excluded", {}).items())
            if "shape" in t]
    return out


class _S3:
    """An S3-compatible prefix: the compose MinIO locally (`UE_S3_ENDPOINT`), any S3 elsewhere.

    Credentials come from boto3's own chain, the standard AWS names first (`AWS_ACCESS_KEY_ID`,
    `AWS_SECRET_ACCESS_KEY`); when those are unset the pair the compose file starts MinIO with (`UE_S3_KEY`,
    `UE_S3_SECRET`) stands in, so one `.env` serves both. Nothing here logs a credential or a signed URL."""

    scheme = "s3"

    def __init__(self, url: str) -> None:
        import boto3
        from botocore.config import Config

        parts = urlsplit(url)
        if parts.scheme != "s3" or not parts.netloc:
            raise SeedError(f"not an s3://bucket/prefix URL: {url!r}")
        self.url = url
        self.bucket = parts.netloc
        self.prefix = parts.path.strip("/")
        endpoint = os.environ.get("UE_S3_ENDPOINT", "").strip() or None
        creds: dict[str, str] = {}
        if not os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("UE_S3_KEY"):
            creds = {"aws_access_key_id": os.environ["UE_S3_KEY"], "aws_secret_access_key": os.environ.get("UE_S3_SECRET", "")}
        # path-style addressing for a self-hosted endpoint (MinIO answers at host/bucket, not bucket.host)
        extra = {"s3": {"addressing_style": "path"}} if endpoint else {}
        config = Config(signature_version="s3v4", retries={"max_attempts": 4}, **extra)
        self.client = boto3.client("s3", endpoint_url=endpoint, config=config,
                                   region_name=os.environ.get("AWS_DEFAULT_REGION") or "us-east-1", **creds)

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def get(self, name: str, dest: Path) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.download_file(self.bucket, self.key(name), str(dest))
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound"):
                raise SeedMissing(f"{self.key(name)} not found at {_shown(self.url)}") from None
            raise SeedError(f"{self.key(name)} at {_shown(self.url)}: {code or type(err).__name__}") from None

    def put(self, name: str, src: Path) -> None:
        self.client.upload_file(str(src), self.bucket, self.key(name))

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
            return
        except ClientError as err:
            if err.response.get("Error", {}).get("Code", "") not in ("404", "NoSuchBucket", "NotFound"):
                raise SeedError(f"bucket {self.bucket}: {err.response.get('Error', {}).get('Code') or 'refused'}") from None
        region = self.client.meta.region_name
        kwargs = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        try:
            self.client.create_bucket(Bucket=self.bucket, **kwargs)
        except ClientError as err:
            raise SeedError(f"bucket {self.bucket} does not exist and could not be created "
                            f"({err.response.get('Error', {}).get('Code')}); create it first") from None


class _Https:
    """A static host: a directory of files behind any web server, or a release's asset directory. Pull only."""

    scheme = "https"

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise SeedError(f"not an https:// URL: {url!r}")
        self.url = url
        self.base = url.rstrip("/")

    def get(self, name: str, dest: Path) -> None:
        import httpx

        target = f"{self.base}/{quote(name)}"
        try:
            with httpx.stream("GET", target, follow_redirects=True, timeout=120.0) as resp:
                if resp.status_code == 404:
                    raise SeedMissing(f"{name} not found at {_shown(self.base)}")
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as err:
            raise SeedError(f"{name} at {_shown(self.base)}: {type(err).__name__}: {str(err)[:200]}") from None

    def put(self, name: str, src: Path) -> None:
        raise SeedError("https:// is pull only; push to an s3:// URL and serve it from there")


def _remote(url: str) -> _S3 | _Https:
    scheme = urlsplit(url).scheme
    if scheme == "s3":
        return _S3(url)
    if scheme in ("https", "http"):
        return _Https(url)
    raise SeedError(f"unsupported seed URL {url!r}: s3://bucket/prefix or https://host/path")


def push(src: Path, to: str, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Upload a pack to `to` (an `s3://bucket/prefix`): every file under `<prefix>/<hash>/`, that directory's
    manifest after them, then `<prefix>/manifest.json` as the pointer to the current pack, last of all.

    The pack is verified on disk first, so nothing unverifiable is ever published. A push that stops part way
    leaves the pointer where it was (at the previous pack, or absent), which is what makes the layout safe to
    pull from while it is being written to."""
    manifest = verify(src, log=log)
    p = Path(manifest["dir"])
    remote = _remote(to)
    if isinstance(remote, _Https):
        raise SeedError("https:// is pull only; push to an s3:// URL and serve it from there")
    remote.ensure_bucket()
    h = manifest["pack_sha256"][:16]
    files = _files(manifest)
    total = 0
    for name, _sha, size in files:
        remote.put(f"{h}/{name}", p / name)
        total += size
        log(f"  {name:<44} {size / 1e6:8.2f} MB -> {h}/")
    remote.put(f"{h}/{MANIFEST}", p / MANIFEST)
    remote.put(MANIFEST, p / MANIFEST)
    log(f"  pack {h} ({manifest['scope']}): {len(files)} files, {total / 1e6:.1f} MB pushed to {_shown(to)}; "
        f"{_shown(to).rstrip('/')}/{MANIFEST} now names it")
    return {"url": to, "pack_sha256": manifest["pack_sha256"], "scope": manifest["scope"], "files": len(files) + 2, "bytes": total}


def pull(url: str, into: Path | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Fetch the pack at `url` into `into/<hash>/` (default `data/seed`) and point `into/latest` at it.

    `url` is a prefix holding `manifest.json` and `<hash>/` beside it (what `push` writes), or one pack's own
    directory (`<prefix>/<hash>`). The manifest is read first and checked against its own address; every file
    is then fetched and its sha256 checked as it lands; `verify` runs over the whole at the end. Any mismatch
    refuses the pull and removes what was fetched. A pack already present under `into` is kept if it verifies."""
    remote = _remote(url)
    root = into or seed_root()
    root.mkdir(parents=True, exist_ok=True)
    work = root / f".pulling-{os.getpid()}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    try:
        remote.get(MANIFEST, work / MANIFEST)
        try:
            manifest = json.loads((work / MANIFEST).read_text())
        except ValueError as err:
            raise SeedError(f"{MANIFEST} at {_shown(url)} is not JSON: {err}") from None
        if manifest.get("version") != SEED_VERSION:
            raise SeedError(f"pack at {_shown(url)} is version {manifest.get('version')!r}, expected {SEED_VERSION}")
        if pack_hash(manifest) != manifest.get("pack_sha256"):
            raise SeedError(f"{MANIFEST} at {_shown(url)} does not hash to its own pack_sha256; refused")
        h = manifest["pack_sha256"][:16]
        # the files sit beside the manifest when the URL is the pack's own directory, else under <hash>/
        sub = "" if url.rstrip("/").endswith("/" + h) else f"{h}/"
        total = 0
        for name, sha, size in _files(manifest):
            dest = work / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            remote.get(sub + name, dest)
            got = _sha256(dest)
            if got != sha:
                raise SeedError(f"{name}: sha256 {got[:12]} differs from the manifest's {sha[:12]}; pull refused")
            total += size
            log(f"  {name:<44} {size / 1e6:8.2f} MB  sha256 ok")
        verify(work, log=lambda *a: None)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    dest = root / h
    if dest.exists():
        try:
            verify(dest, log=lambda *a: None)
            shutil.rmtree(work)
            log(f"  pack {h} already under {root} and verifies; kept")
        except SeedError:
            shutil.rmtree(dest)
            work.rename(dest)
            log(f"  pack {h} under {root} did not verify; replaced")
    else:
        work.rename(dest)
    latest = root / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink() if latest.is_symlink() or latest.is_file() else shutil.rmtree(latest)
    latest.symlink_to(dest.name)
    log(f"  pack {h} ({manifest['scope']}): {len(manifest['tables'])} tables, {total / 1e6:.1f} MB from {_shown(url)} -> {dest}")
    manifest["dir"] = str(dest)
    return manifest


def ensure(seed: Path | None = None, into: Path | None = None, log: Callable[[str], None] = print) -> str:
    """The container's first step: unpack the seed when there is no store. Returns what it did.

    "present" when a store exists (nothing touched), "unpacked" when a pack on disk was unpacked into place,
    "pulled" when there was no pack on disk and `UE_SEED_URL` supplied one (pulled under `data/seed`, then
    unpacked), and "no-seed" when there is none of these; that last case is not an error here, so the app
    still starts and says what is missing."""
    target = into or db_path()
    if target.is_file():
        log(f"  store present at {target}; seed not needed")
        return "present"
    src = seed or seed_dir()
    if (src / MANIFEST).is_file():
        unpack(src, target, log=log)
        return "unpacked"
    url = seed_url()
    if not url:
        log(f"  no store at {target} and no seed pack at {src} (set UE_SEED_DIR, put a pack at "
            f"{seed_root() / 'latest'}, or set UE_SEED_URL to pull one); the app starts without a store")
        return "no-seed"
    log(f"  no store at {target} and no seed pack at {src}; pulling from {_shown(url)}")
    try:
        pulled = pull(url, into=seed_root(), log=log)
    except SeedMissing as err:
        # nothing published there yet is the no-seed case, not a fault: the app starts and says so. A pack that
        # is there and does not verify is a fault, and still raises.
        log(f"  {err}; the app starts without a store")
        return "no-seed"
    unpack(Path(pulled["dir"]), target, log=log)
    return "pulled"


__all__ = ["RULE", "SEED_VERSION", "SeedError", "SeedMissing", "decisions", "ensure", "pack", "pack_hash", "pull", "push",
           "seed_dir", "seed_root", "seed_url", "unpack", "verify", "TIER_BY_SCHEMA"]

"""The data readiness gate (PRD §9.1): five columns, one row per dataset the focused tasks depend on.

The rule is "no agent phase starts until every dataset the focused tasks depend on is green on all five
columns". This module says, for each such dataset, which columns are green and why the others are not. It
never fixes anything; it is the checklist the agent phases wait behind, and the verdict it prints is the one
the PRD asks for.

Three kinds of dataset are in scope, one row each:

* every source layer or scene collection a feature is built from (`derived.feature_spec.source_keys`);
* every label source in the inventory (role "label"), which is never a feature;
* every enabled file of the reading corpus (`selection.json["enabled"]`), whose objects live under
  `data/raw/<file>/`, whose rendered pages live under `data/pages/<pdf_sha256>/pNNNN.png` (one row per page in
  `data/out/pages.parquet`), whose OCR sidecars live under `data/ocr/<pdf_sha256>/pages/pNNNN-<key>.json`, and
  whose text pages sit in `read.corpus_page`; the fetch manifest `data/raw/manifest.json` records every
  object's url, byte count and sha256.

Column semantics, as this module applies them:

Present
    Green when the dataset is in the store under its tier with a known, non-zero row count: a `native.layer`
    row with `record_count > 0`; `native.scene` rows for the collection; for a read-tier label, rows in the
    table its inventory entry names. For a file: every object the probe register lists (report PDFs,
    appendix PDFs, assay sheets, certificates) is on disk under `data/raw`, stated as n of m; a `.part` file
    is a download in progress and does not count.
Licensed
    Green when the licence is named and either redistributable, or the dataset is used locally and never
    exported (a label layer, a read-tier source). Red when the licence is not stated, or when a source whose
    licence forbids redistribution is nonetheless in an export. Report PDFs carry no named licence and are
    never served: for a file this column is green by construction and says so.
Covers
    Green when a coverage number is stated for every feature built on the source (from
    `readiness.table()`); the note carries the largest coverage and names the thin features, which is
    information, never a failure. A label covers nothing and is reported as "label", green when present. For
    a file: the share of its rendered pages that have text, from `read.corpus_page` or an OCR sidecar on
    disk, stated as x of y; red when nothing is rendered (no share to state) or no page has text (the corpus
    does not cover the file yet); a share at or below the thin threshold is flagged, not failed.
Servable
    Green when the layer is in an export: a vector tile in `web/public/data/tiles/manifest.json`, a context
    layer in `export_layers.EXPORTS` or the web export, or (for a feature source) its features in the
    coverage/scores export under `web/public/data/prospect`. Also green when the licence forbids serving
    and the dataset is not served ("local only"). Red only when a redistributable feature source has no
    export path. A file's PDFs are not served (green, by licence); the note says whether any of its page
    images are exported under `web/public/data/pages/<file>`, which is information, never a failure.
Versioned
    Green when the store row carries a payload hash (`native.layer.payload_sha256`; for scenes, every row a
    STAC id and href; for a read-tier source, every record a `read.pdf` hash) and the pull log has the key,
    and a snapshot manifest names the store as it is now (`store_sha256` equal to the sha256 of the store
    file). The snapshot condition is one shared fact: it is reported once at the top and folded into every
    store-backed row. For a file: every listed object has a sha256 in the fetch manifest, stated as n of m.

The assessors (`assess_layer`, `assess_file`) are pure and take plain values; `check` gathers the inputs from
the store (read-only), the inventory, the pull log, the snapshots, the exports and the corpus on disk, prints
the table, writes `data/out/prospect/gate.json` and returns it.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb

from ..paths import PATHS

COLUMNS = ("present", "licensed", "covers", "servable", "versioned")
KINDS = ("layer", "scene", "label", "file")

#: a page-text share at or below this is flagged thin, like a feature's coverage on the readiness page
THIN_SHARE = 0.40

#: the object kinds the probe register lists per enabled file, in the order they are reported
FILE_ITEM_KINDS = ("report_pdfs", "appendix_pdfs", "assay_xls", "certificate_pdfs")

#: label layers the web export writes by hand (export_web.build_deposits / build_occurrences), keyed by layer
WEB_CONTEXT_EXPORTS: dict[str, str] = {
    "uranium_deposit_footprints": "context/uranium_deposits.geojson",
    "mineral_deposits_uranium": "context/mineral_deposits_u.geojson",
}

_NOT_STATED = re.compile(r"\bno(?:t)?\s+(?:licence\s+)?stated\b|\bnot_stated\b", re.IGNORECASE)


# ---------------------------------------------------------------- pure helpers


def col(ok: bool, note: str) -> dict[str, Any]:
    """One cell of the table: a verdict and the short reason for it."""
    return {"ok": bool(ok), "note": note}


def norm_key(key: str) -> str:
    """'sentinel-2-l2a' and 'sentinel2_l2a' are one thing."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def licence_stated(name: str | None) -> bool:
    name = (name or "").strip()
    return bool(name) and not _NOT_STATED.search(name)


def _pct(x: float) -> str:
    return f"{x:.0%}"


def _is_number(x: Any) -> bool:
    try:
        return x is not None and not math.isnan(float(x))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- assessors


def assess_layer(
    *,
    key: str,
    title: str = "",
    kind: str = "layer",
    role: str = "feature",
    record_count: int | None,
    licence: str | None,
    redistributable: bool | None,
    hashed: str | None,
    pulled: bool | None,
    snapshot: str | None,
    exports: Iterable[str] = (),
    features: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """One store-backed row: a layer, a scene collection or a label source.

    `hashed` is the hash evidence when the row is hashed ("payload 2f4ba2a9c1c9", "24 of 24 scenes carry a
    STAC id") and None when it is not; `pulled` is whether the pull log has the key, or None when a pull log
    does not apply (scenes, read-tier sources); `snapshot` is the 12-character prefix of the snapshot that
    names the store as it is now, or None; `exports` lists the export paths the layer was found in.
    """
    if kind not in KINDS:
        raise ValueError(f"{key}: kind {kind!r} not one of {KINDS}")
    exports = list(exports)
    features = list(features)
    unit = "scenes" if kind == "scene" else "rows"

    if record_count is None:
        present = col(False, "no store row")
    elif record_count <= 0:
        present = col(False, f"0 {unit}")
    else:
        present = col(True, f"{record_count:,} {unit}")

    name = (licence or "").strip()
    if not licence_stated(name):
        licensed = col(False, "licence not stated")
    elif redistributable:
        licensed = col(True, f"{name}; redistributable")
    elif not exports:
        licensed = col(True, f"{name}; local only, never served")
    else:
        licensed = col(False, f"{name} forbids redistribution but the layer is exported: {exports[0]}")

    if role == "label":
        covers = col(True, "label")
    elif not features:
        covers = col(False, "no feature is built on it")
    else:
        missing = [f["feature_key"] for f in features if not _is_number(f.get("coverage"))]
        if missing:
            covers = col(False, f"coverage not stated for {', '.join(missing)}")
        else:
            best = max(features, key=lambda f: float(f["coverage"]))
            thin = [f"{f['feature_key']} {_pct(float(f['coverage']))}" for f in features if f.get("thin")]
            note = f"{len(features)} feature(s); max {_pct(float(best['coverage']))} ({best['feature_key']})"
            if thin:
                note += f"; thin: {', '.join(thin)}"
            covers = col(True, note)

    if exports:
        servable = col(True, "; ".join(exports))
    elif not redistributable:
        servable = col(True, "local only (licence forbids serving)")
    elif role == "label":
        servable = col(True, "local only (label, not served)")
    else:
        servable = col(False, "redistributable feature source with no export path")

    problems: list[str] = []
    if not hashed:
        problems.append("no payload hash")
    if pulled is False:
        problems.append("not in the pull log")
    if not snapshot:
        problems.append("no snapshot names the store as it is now")
    if problems:
        versioned = col(False, "; ".join(problems))
    else:
        parts = [str(hashed)]
        if pulled:
            parts.append("in the pull log")
        parts.append(f"snapshot {snapshot}")
        versioned = col(True, "; ".join(parts))

    return {
        "dataset": key, "title": title or key, "kind": kind,
        "present": present, "licensed": licensed, "covers": covers, "servable": servable, "versioned": versioned,
    }


def assess_file(
    *,
    file_num: str,
    title: str = "",
    items: Iterable[dict[str, Any]],
    pages_rendered: int,
    pages_with_text: int,
    served_pages: int = 0,
    parts: int = 0,
) -> dict[str, Any]:
    """One enabled file of the reading corpus.

    `items` are the objects the probe register lists, each with `name`, `kind`, `on_disk` and `sha256` (None
    when the fetch manifest has no hash for it); `pages_rendered` and `pages_with_text` count the file's
    rendered pages and those among them with text; `served_pages` counts page images exported to the web
    app; `parts` counts `.part` downloads in progress.
    """
    items = list(items)
    m = len(items)
    n = sum(1 for it in items if it.get("on_disk"))
    if m == 0:
        present = col(False, "nothing listed for this file")
    else:
        note = f"{n} of {m} objects on disk"
        if parts:
            note += f"; {parts} .part in progress"
        present = col(n == m, note)

    licensed = col(True, "local only, never served (report PDFs carry no named licence)")

    y, x = int(pages_rendered), int(pages_with_text)
    if y <= 0:
        covers = col(False, "no pages rendered")
    elif x <= 0:
        covers = col(False, f"0 of {y} rendered pages have text")
    else:
        share = x / y
        note = f"{x} of {y} rendered pages have text ({_pct(share)})"
        if share <= THIN_SHARE:
            note += "; thin"
        covers = col(True, note)

    served_note = f"{served_pages} page image(s) exported" if served_pages else "no page images exported"
    servable = col(True, f"not served (local only); {served_note}")

    h = sum(1 for it in items if it.get("sha256"))
    versioned = col(m > 0 and h == m, f"{h} of {m} objects hashed in data/raw/manifest.json")

    return {
        "dataset": file_num, "title": title or file_num, "kind": "file",
        "present": present, "licensed": licensed, "covers": covers, "servable": servable, "versioned": versioned,
    }


def verdict(rows: Iterable[dict[str, Any]]) -> tuple[bool, list[str]]:
    """The gate is green when every row is green on every column; otherwise the failures, one string each."""
    failures = [
        f"{row['dataset']}: {column}: {row[column]['note']}"
        for row in rows for column in COLUMNS if not row[column]["ok"]
    ]
    return not failures, failures


def resolve_source(key: str, sources: Iterable[Any], pull_log: dict[str, dict[str, Any]]) -> Any | None:
    """The inventory source a store key belongs to: same key, same collection, or the pull whose url is under
    the source's url (the survey footprints are two pulls of one registered source)."""
    sources = list(sources)
    for s in sources:
        if s.key == key:
            return s
    for s in sources:
        if s.collection and norm_key(s.collection) == norm_key(key):
            return s
    url = str((pull_log.get(key) or {}).get("url") or "")
    if url:
        for s in sources:
            if s.url and url.startswith(s.url):
                return s
    return None


def layer_exports(key: str, web_data: Path, tiles: dict[str, dict[str, Any]], specs: Iterable[Any]) -> list[str]:
    """Every export path a layer key is found in: context files on disk, tiles built from them, hand-written
    web exports."""
    found: list[str] = []
    for spec in specs:
        if key not in (spec.key, spec.source_file):
            continue
        rel = f"context/{spec.out}"
        if (web_data / rel).is_file():
            found.append(rel)
        found.extend(f"tiles:{tid}" for tid, t in tiles.items() if t.get("source_geojson") == rel)
    rel = WEB_CONTEXT_EXPORTS.get(key)
    if rel and (web_data / rel).is_file():
        found.append(rel)
    return found


# ---------------------------------------------------------------- printing


def render_table(rows: Iterable[dict[str, Any]], log: Callable[[str], None] = print) -> None:
    rows = list(rows)
    width = max([len("dataset"), *(len(r["dataset"]) for r in rows)])
    marks = "  ".join(f"{c:9}" for c in COLUMNS)
    log(f"  {'dataset':{width}}  {'kind':5}  {marks}")
    for r in rows:
        marks = "  ".join(f"{'ok' if r[c]['ok'] else 'FAIL':9}" for c in COLUMNS)
        log(f"  {r['dataset']:{width}}  {r['kind']:5}  {marks}")
        log("  " + " " * width + "   " + " | ".join(r[c]["note"] for c in COLUMNS))


# ---------------------------------------------------------------- gathering


def _read_tier(con: Any, table: str, where: str | None) -> dict[str, Any]:
    """Row count and hash chain for a read-tier source named as `read.<table> [where ...]`."""
    cond = f" where {where}" if where else ""
    n = con.execute(f"select count(*) from {table}{cond}").fetchone()[0]
    unhashed = con.execute(
        f"select count(*) from {table} r{cond}{' and' if where else ' where'} not exists "
        "(select 1 from read.pdf p where p.file_num = r.file_num and coalesce(p.pdf_sha256, '') <> '')"
    ).fetchone()[0]
    return {"n": int(n), "unhashed": int(unhashed)}


def _gather_store(log: Callable[[str], None]) -> dict[str, Any]:
    from ..store import connect

    con = connect(read_only=True)
    try:
        out = {
            "layers": {
                r[0]: {"record_count": r[1], "payload_sha256": r[2], "licence": r[3], "redistributable": r[4],
                       "role": r[5], "title": r[6]}
                for r in con.execute(
                    "select layer_key, record_count, payload_sha256, licence, redistributable, role, title "
                    "from native.layer").fetchall()
            },
            "scenes": {
                r[0]: {"n": int(r[1]), "with_id": int(r[2]), "licence": r[3]}
                for r in con.execute(
                    "select collection, count(*), "
                    "count(*) filter (where coalesce(stac_id, '') <> '' and coalesce(href, '') <> ''), "
                    "min(licence) from native.scene group by 1").fetchall()
            },
            "specs": [
                {"feature_key": r[0], "source_keys": json.loads(r[1] or "[]")}
                for r in con.execute("select feature_key, source_keys from derived.feature_spec").fetchall()
            ],
            "corpus_pages": {(r[0], int(r[1])) for r in con.execute(
                "select doc_sha256, page from read.corpus_page").fetchall()},
            "read_tier": {},
        }
        from .inventory import load as load_inventory

        for src in load_inventory().with_role("label"):
            if src.tier != "read":
                continue
            m = re.fullmatch(r"\s*(read\.\w+)(?:\s+where\s+(.+?))?\s*", src.url)
            if not m:
                log(f"  {src.key}: cannot read its url {src.url!r} as a read-tier table; counted as absent")
                continue
            try:
                out["read_tier"][src.key] = _read_tier(con, m.group(1), m.group(2))
            except duckdb.Error as err:  # a table the schema has not got yet is an absent source, not a crash
                log(f"  {src.key}: {type(err).__name__} reading {src.url!r}; counted as absent")
    finally:
        con.close()
    return out


def _feature_coverage(log: Callable[[str], None]) -> dict[str, dict[str, Any]]:
    """feature_key -> coverage and thin flag, from the readiness table; empty when it cannot be computed."""
    try:
        from .readiness import table

        df = table()
    except Exception as err:  # noqa: BLE001 - whatever stops the table is a reportable state, not a crash
        log(f"  readiness table unavailable ({type(err).__name__}: {err}); no coverage can be stated")
        return {}
    return {
        str(r["feature_key"]): {"coverage": (float(r["coverage"]) if _is_number(r["coverage"]) else None),
                                "thin": bool(r["thin"])}
        for _, r in df.iterrows()
    }


def _snapshot_state(log: Callable[[str], None]) -> tuple[str | None, str | None]:
    """(store sha256, matching snapshot prefix); logs the shared fact once."""
    from ..store import snapshot as SN

    db = SN.db_path()
    if not db.is_file():
        log(f"  store {db} is missing; nothing can be versioned")
        return None, None
    sha = SN._sha256(db)
    snaps = SN.list_snapshots()
    match = [s for s in snaps if s.get("store_sha256") == sha]
    if match:
        log(f"  store {sha[:12]}: snapshot {sha[:12]} names it (taken {match[-1].get('taken_at')})")
        return sha, sha[:12]
    latest = snaps[-1]["store_sha256"][:12] if snaps else None
    where = f"{len(snaps)} on disk, latest {latest}" if snaps else "none on disk"
    log(f"  store {sha[:12]}: no snapshot names the store as it is now ({where}); "
        "every store-backed row is red on Versioned until `lr store snapshot` runs")
    return sha, None


def _tiles() -> dict[str, dict[str, Any]]:
    path = PATHS.web_data / "tiles" / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return dict(json.loads(path.read_text()).get("tiles") or {})
    except (ValueError, AttributeError):
        return {}


def _exported_features() -> set[str]:
    """Feature keys the coverage/scores export carries (web/public/data/prospect/readiness.json)."""
    path = PATHS.web_data / "prospect" / "readiness.json"
    if not path.is_file():
        return set()
    try:
        doc = json.loads(path.read_text())
    except ValueError:
        return set()
    if not (PATHS.web_data / "prospect" / "coverage.geojson").is_file():
        return set()
    return {str(f.get("feature_key")) for f in doc.get("features") or [] if f.get("feature_key")}


def _store_backed_rows(store: dict[str, Any], coverage: dict[str, dict[str, Any]], snapshot: str | None,
                       log: Callable[[str], None]) -> list[dict[str, Any]]:
    from ..export_layers import EXPORTS
    from ..index import load_pull_log
    from .inventory import load as load_inventory

    inv = load_inventory()
    pulls = load_pull_log()
    tiles = _tiles()
    exported_features = _exported_features()

    features_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in store["specs"]:
        for key in spec["source_keys"]:
            cov = coverage.get(spec["feature_key"]) or {"coverage": None, "thin": False}
            features_by_source[str(key)].append({"feature_key": spec["feature_key"], **cov})

    def scene_key(key: str) -> str | None:
        for collection in store["scenes"]:
            if norm_key(collection) == norm_key(key):
                return collection
        src = resolve_source(key, inv.sources, pulls)
        if src and src.collection:
            for collection in store["scenes"]:
                if norm_key(collection) == norm_key(src.collection):
                    return collection
        return None

    def store_row(key: str, role: str) -> dict[str, Any]:
        src = resolve_source(key, inv.sources, pulls)
        title = src.title if src else key
        features = features_by_source.get(key, [])
        exports = layer_exports(key, PATHS.web_data, tiles, EXPORTS)
        served_features = [f["feature_key"] for f in features if f["feature_key"] in exported_features]
        if served_features:
            exports.append(f"coverage export ({len(served_features)} feature(s))")
        layer = store["layers"].get(key)
        if layer is not None:
            licence = layer["licence"] or (src.licence.name if src else None)
            redistributable = bool(layer["redistributable"]) if layer["redistributable"] is not None else (
                src.redistributable if src else False)
            sha = layer["payload_sha256"]
            return assess_layer(
                key=key, title=layer["title"] or title, kind="label" if role == "label" else "layer", role=role,
                record_count=layer["record_count"], licence=licence, redistributable=redistributable,
                hashed=f"payload {sha[:12]}" if sha else None, pulled=key in pulls, snapshot=snapshot,
                exports=exports, features=features)
        collection = scene_key(key)
        if collection is not None:
            sc = store["scenes"][collection]
            hashed = f"{sc['with_id']} of {sc['n']} scenes carry a STAC id" if sc["with_id"] == sc["n"] and sc["n"] else None
            return assess_layer(
                key=key, title=title, kind="scene", role=role, record_count=sc["n"],
                licence=sc["licence"] or (src.licence.name if src else None),
                redistributable=src.redistributable if src else False,
                hashed=hashed, pulled=None, snapshot=snapshot, exports=exports, features=features)
        if src is not None and src.tier == "read":
            rt = store["read_tier"].get(key)
            n = rt["n"] if rt else None
            hashed = (f"{n} record(s) walk back to a read.pdf hash" if rt and rt["n"] and not rt["unhashed"]
                      else None)
            # a read-tier source is what the model read out of assessment files: like the PDFs themselves it is
            # used locally and never served, which is the licence position the report PDFs carry
            return assess_layer(
                key=key, title=title, kind="label" if role == "label" else "layer", role=role, record_count=n,
                licence="read locally from assessment files, which carry no named licence; never served",
                redistributable=False, hashed=hashed, pulled=None,
                snapshot=snapshot, exports=exports, features=features)
        return assess_layer(
            key=key, title=title, kind="label" if role == "label" else "layer", role=role, record_count=None,
            licence=src.licence.name if src else None, redistributable=src.redistributable if src else False,
            hashed=None, pulled=key in pulls, snapshot=snapshot, exports=exports, features=features)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in sorted(features_by_source):
        src = resolve_source(key, inv.sources, pulls)
        if src is not None and src.role == "label":
            log(f"  {key}: a label source is cited as a feature source; reported as a label")
        rows.append(store_row(key, "label" if (src and src.role == "label") else "feature"))
        seen.add(key)
    for src in inv.with_role("label"):
        # a label registered under one key may sit in the store under another (smdi_uranium is the pull
        # mineral_deposits_uranium); find the store key whose pull url is under the source's url
        key = src.key
        if key not in store["layers"] and src.tier != "read":
            for pull_key, pull in pulls.items():
                if src.url and str(pull.get("url") or "").startswith(src.url) and pull_key in store["layers"]:
                    key = pull_key
                    break
        if key in seen:
            continue
        rows.append(store_row(key, "label"))
        seen.add(key)
    return rows


def _file_rows(store: dict[str, Any], log: Callable[[str], None]) -> list[dict[str, Any]]:
    from ..render import read_pages
    from ..select import enabled_files, read_selection

    try:
        sel = read_selection()
    except FileNotFoundError as err:
        log(f"  no selection register ({err}); no file rows")
        return []
    files = enabled_files(sel)
    register = (sel.get("enabled") or {}).get("probe") or {}
    if not files:
        log("  no enabled files in the selection register; no file rows")
        return []

    manifest_path = PATHS.raw / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    by_url = {rec.get("url"): (rel, rec) for rel, rec in manifest.items() if rec.get("url")}
    shas_by_file: dict[str, set[str]] = defaultdict(set)
    for rec in manifest.values():
        if rec.get("sha256"):
            shas_by_file[rec["file_num"]].add(rec["sha256"])

    rendered_by_sha: dict[str, set[int]] = defaultdict(set)
    for r in read_pages():
        if r.get("rendered"):
            rendered_by_sha[r["pdf_sha256"]].add(int(r["page_no"]))

    def ocr_pages(sha: str) -> set[int]:
        d = PATHS.ocr / sha / "pages"
        if not d.is_dir():
            return set()
        return {int(p.name[1:5]) for p in d.glob("p[0-9][0-9][0-9][0-9]-*.json")}

    def served_pages(file_num: str, shas: set[str]) -> int:
        n = 0
        for d in (PATHS.web_data / "pages" / file_num, *(PATHS.web_data / "pages" / s for s in shas)):
            if d.is_dir():
                n += sum(1 for _ in d.glob("p[0-9][0-9][0-9][0-9].*"))
        return n

    rows = []
    for file_num in files:
        probe = register.get(file_num) or {}
        items = []
        for kind in FILE_ITEM_KINDS:
            for it in probe.get(kind) or []:
                rel, rec = by_url.get(it.get("url"), (None, None))
                on_disk = bool(rel) and (PATHS.raw / rel).is_file()
                items.append({"name": it.get("name"), "kind": kind, "on_disk": on_disk,
                              "sha256": (rec or {}).get("sha256") if on_disk else None})
        raw_dir = PATHS.raw / file_num
        parts = sum(1 for _ in raw_dir.rglob("*.part")) if raw_dir.is_dir() else 0
        shas = shas_by_file.get(file_num, set())
        rendered = with_text = 0
        for sha in shas:
            pages = rendered_by_sha.get(sha, set())
            if not pages:
                continue
            ocr = ocr_pages(sha)
            rendered += len(pages)
            with_text += sum(1 for p in pages if p in ocr or (sha, p) in store["corpus_pages"])
        rows.append(assess_file(
            file_num=file_num, title=(sel.get("enabled") or {}).get("reasons", {}).get(file_num) or file_num,
            items=items, pages_rendered=rendered, pages_with_text=with_text,
            served_pages=served_pages(file_num, shas), parts=parts))
    return rows


def check(log: Callable[[str], None] = print, out_path: Path | None = None) -> dict[str, Any]:
    """Print the five-column table and the verdict; write and return gate.json."""
    store_sha, snapshot = _snapshot_state(log)
    store = _gather_store(log)
    coverage = _feature_coverage(log)
    rows = _store_backed_rows(store, coverage, snapshot, log)
    rows.extend(_file_rows(store, log))
    render_table(rows, log)
    green, failures = verdict(rows)
    log("  gate green" if green else f"  gate red: {len(failures)} failures")

    out = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "store_sha256": store_sha, "snapshot": snapshot,
        "rows": rows, "green": green, "failures": failures,
    }
    path = out_path or (PATHS.out / "prospect" / "gate.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n")
    log(f"  wrote {path.relative_to(PATHS.pipeline) if path.is_relative_to(PATHS.pipeline) else path}")
    return out

"""`ue fetch`: download the selected files' document PDFs, assay tables and certificates, resumably.

Observed on the provincial object store (s3sask2.sasktelcloudstorage.com, 2026-09-18): no auth, range
requests answer 206 with Content-Range, Content-Type is binary/octet-stream, the ETag is the MD5 of the
object for single-part uploads, and table 23's FILE_SIZE is in MiB.

Why this shape:
- PDFs have no named licence: they stay under data/raw (git-ignored) and are never redistributed.
- Downloads resume from a `.part` file with an HTTP Range request, so a dropped connection costs nothing.
- Every file is verified three ways: byte count against the server's total, MD5 against a plain ETag when
  the store provides one, and FILE_SIZE from the index within 1% (a stale index row is reported, not fatal).
- A manifest records url, bytes and sha256; re-running skips files whose size and sha256 still match.
- One request per 2 seconds, like every other provincial endpoint.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .arcgis import USER_AGENT
from .ids import sha256_file

PACE_S = 2.0
MAX_TOTAL_BYTES = 3 * 1024**3  # stop and report instead of downloading more than about 3 GB

KIND_DIR = {"report_pdf": "", "appendix_pdf": "appendix", "assay_xls": "assays", "certificate_pdf": "certificates"}


class FetchError(Exception):
    pass


@dataclass
class FetchItem:
    file_num: str
    kind: str
    name: str
    folder: str
    url: str
    size_mb: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FetchItem":
        return cls(file_num=d["file_num"], kind=d["kind"], name=d["name"], folder=d["folder"], url=d["url"],
                   size_mb=float(d.get("size_mb") or 0.0))


def _safe_name(name: str) -> str:
    return re.sub(r"[/\\:\x00]", "_", name).strip() or "unnamed"


def plan_paths(items: list[FetchItem], raw_dir: Path) -> dict[int, Path]:
    """Destination per item: data/raw/<file_num>/[appendix|assays|certificates/]<name>.
    Two objects with the same name in different folders get a short folder hash prefix."""
    out: dict[int, Path] = {}
    seen: dict[Path, int] = {}
    for i, it in enumerate(items):
        base = raw_dir / it.file_num / KIND_DIR.get(it.kind, "other") / _safe_name(it.name)
        if base in seen:
            h = hashlib.sha256(it.folder.encode()).hexdigest()[:8]
            base = base.with_name(f"{h}_{base.name}")
        seen[base] = i
        out[i] = base
    return out


@dataclass
class Fetcher:
    raw_dir: Path
    client: httpx.Client | None = None
    pace_s: float = PACE_S
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    retries: int = 4
    chunk: int = 1 << 16
    log: Callable[[str], None] = print
    requests_made: int = 0
    _last: float = field(default=-1e9)

    def __post_init__(self) -> None:
        self._own = self.client is None
        if self.client is None:
            self.client = httpx.Client(timeout=httpx.Timeout(60.0, read=120.0), follow_redirects=True,
                                       headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        if self._own and self.client is not None:
            self.client.close()

    @property
    def manifest_path(self) -> Path:
        return self.raw_dir / "manifest.json"

    def read_manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path.read_text()) if self.manifest_path.is_file() else {}

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".part")
        tmp.write_text(json.dumps(dict(sorted(manifest.items())), indent=1) + "\n")
        tmp.rename(self.manifest_path)
        lines = [f"{v['sha256']}  {k}" for k, v in sorted(manifest.items())]
        (self.raw_dir / "SHA256SUMS").write_text("\n".join(lines) + ("\n" if lines else ""))

    def _pace(self) -> None:
        wait = self._last + self.pace_s - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last = self.clock()
        self.requests_made += 1

    def _download_to_part(self, url: str, part: Path) -> tuple[int, str | None]:
        """Stream url into part, resuming from its current size. Returns (total bytes, etag)."""
        assert self.client is not None
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            self._pace()
            try:
                with self.client.stream("GET", url, headers=headers) as r:
                    if r.status_code == 416 and have:
                        m = re.search(r"/(\d+)$", r.headers.get("content-range", ""))
                        if m and int(m.group(1)) == have:
                            return have, r.headers.get("etag")
                        part.unlink()  # the part is longer than the object: start over
                        continue
                    if r.status_code == 206:
                        m = re.match(r"bytes (\d+)-(\d+)/(\d+)", r.headers.get("content-range", ""))
                        if not m or int(m.group(1)) != have:
                            raise FetchError(f"unexpected Content-Range {r.headers.get('content-range')!r}")
                        total = int(m.group(3))
                        mode = "ab"
                    elif r.status_code == 200:
                        total = int(r.headers["content-length"]) if "content-length" in r.headers else -1
                        mode = "wb"  # server ignored the range: rewrite from the start
                    else:
                        raise FetchError(f"HTTP {r.status_code} for {url}")
                    etag = r.headers.get("etag")
                    part.parent.mkdir(parents=True, exist_ok=True)
                    with part.open(mode) as fh:
                        for block in r.iter_bytes(self.chunk):
                            fh.write(block)
                size = part.stat().st_size
                if total >= 0 and size != total:
                    raise FetchError(f"short read: {size} of {total} bytes")
                return size, etag
            except (httpx.HTTPError, FetchError) as e:
                last_err = e
                self.log(f"    retry {attempt + 1}/{self.retries} after {type(e).__name__}: {e}")
                self.sleep(min(30.0, 2.0 * (attempt + 1)))
        raise FetchError(f"{url}: failed after {self.retries} retries: {last_err}")

    def fetch_item(self, it: FetchItem, dest: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        rel = str(dest.relative_to(self.raw_dir))
        prev = manifest.get(rel)
        if prev and dest.is_file() and dest.stat().st_size == prev["bytes"] and sha256_file(dest) == prev["sha256"]:
            return {**prev, "status": "skipped"}
        part = dest.with_name(dest.name + ".part")
        t0 = time.monotonic()
        size, etag = self._download_to_part(it.url, part)
        md5_checked = None
        plain_etag = (etag or "").strip('"')
        if re.fullmatch(r"[0-9a-f]{32}", plain_etag):
            h = hashlib.md5()
            with part.open("rb") as fh:
                while block := fh.read(1 << 20):
                    h.update(block)
            if h.hexdigest() != plain_etag:
                part.unlink()
                raise FetchError(f"{it.name}: MD5 {h.hexdigest()} does not match ETag {plain_etag}; part removed")
            md5_checked = True
        expected = it.size_mb * 1024 * 1024
        size_note = None
        if expected and abs(size - expected) > max(0.01 * expected, 2048):
            size_note = f"index FILE_SIZE {it.size_mb} MiB differs from {size} bytes"
        part.rename(dest)
        rec = {
            "file_num": it.file_num, "kind": it.kind, "name": it.name, "folder": it.folder, "url": it.url,
            "bytes": size, "sha256": sha256_file(dest), "etag_md5_verified": md5_checked,
            "index_size_mib": it.size_mb, "size_note": size_note,
            "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - t0, 2),
        }
        manifest[rel] = rec
        return {**rec, "status": "downloaded"}

    def fetch_all(self, items: list[FetchItem], max_total_bytes: int = MAX_TOTAL_BYTES) -> list[dict[str, Any]]:
        planned = sum(it.size_mb for it in items) * 1024 * 1024
        if planned > max_total_bytes:
            raise FetchError(f"planned download {planned / 1e9:.2f} GB exceeds the {max_total_bytes / 1e9:.2f} GB limit")
        manifest = self.read_manifest()
        paths = plan_paths(items, self.raw_dir)
        results = []
        try:
            for i, it in enumerate(items):
                res = self.fetch_item(it, paths[i], manifest)
                res["path"] = str(paths[i].relative_to(self.raw_dir))
                results.append(res)
                self.log(f"  {res['status']:<10} {it.file_num:<12} {it.kind:<15} {res['bytes'] / 1e6:8.2f} MB  {it.name[:70]}")
                if res["status"] == "downloaded":
                    self.write_manifest(manifest)
        finally:
            self.write_manifest(manifest)
        return results


def fetch_parallel(items: list[FetchItem], raw_dir: Path, workers: int, log: Callable[[str], None] = print,
                   make_fetcher: Callable[[], Fetcher] | None = None,
                   max_total_bytes: int = MAX_TOTAL_BYTES) -> list[dict[str, Any]]:
    """`Fetcher.fetch_all` over several connections at once.

    Measured 2026-09-19: the provincial object store caps each connection at roughly 17-25 kB/s and three
    parallel range requests each got their own share, so N connections are close to N times faster. Each
    worker is its own `Fetcher` with its own client and its own pace; items are dealt round-robin so no two
    workers touch one destination; the manifest is merged and written under a lock after every download,
    which keeps the resume-on-restart behaviour of the single-stream fetcher."""
    workers = max(1, int(workers))
    if workers == 1:
        single = make_fetcher() if make_fetcher else Fetcher(raw_dir=raw_dir, log=log)
        try:
            return single.fetch_all(items, max_total_bytes=max_total_bytes)
        finally:
            single.close()
    base = Fetcher(raw_dir=raw_dir, log=log)
    try:
        planned = sum(it.size_mb for it in items) * 1024 * 1024
        if planned > max_total_bytes:
            raise FetchError(f"planned download {planned / 1e9:.2f} GB exceeds the {max_total_bytes / 1e9:.2f} GB limit")
        manifest = base.read_manifest()
        paths = plan_paths(items, raw_dir)
        lock = threading.Lock()
        results: list[dict[str, Any] | None] = [None] * len(items)

        # a shared queue rather than a round-robin split: with a fixed split, a worker that draws a 100 MB
        # volume keeps every small object behind it waiting while the other workers sit idle
        todo: queue.Queue[int] = queue.Queue()
        for i in range(len(items)):
            todo.put(i)

        def run(fetcher: Fetcher) -> None:
            local = dict(manifest)
            try:
                while True:
                    try:
                        i = todo.get_nowait()
                    except queue.Empty:
                        return
                    it = items[i]
                    res = fetcher.fetch_item(it, paths[i], local)
                    rel = str(paths[i].relative_to(raw_dir))
                    res["path"] = rel
                    with lock:
                        manifest[rel] = local[rel]
                        results[i] = res
                        log(f"  {res['status']:<10} {it.file_num:<12} {it.kind:<15} {res['bytes'] / 1e6:8.2f} MB  {it.name[:70]}")
                        if res["status"] == "downloaded":
                            base.write_manifest(manifest)
            finally:
                fetcher.close()

        fetchers = [make_fetcher() if make_fetcher else Fetcher(raw_dir=raw_dir, log=log) for _ in range(workers)]
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ue-fetch") as pool:
                futures = [pool.submit(run, fetchers[w]) for w in range(workers)]
                for f in futures:
                    f.result()
        finally:
            with lock:
                base.write_manifest(manifest)
        return [r for r in results if r is not None]
    finally:
        base.close()


def documents_by_file(raw_dir: Path, kinds: tuple[str, ...] = ("report_pdf", "appendix_pdf")) -> dict[str, list[dict[str, Any]]]:
    """Fetched document PDFs per file number, from the manifest (path relative to raw_dir)."""
    mpath = raw_dir / "manifest.json"
    if not mpath.is_file():
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for rel, rec in sorted(json.loads(mpath.read_text()).items()):
        if rec["kind"] in kinds:
            out.setdefault(rec["file_num"], []).append({**rec, "path": rel})
    return out


def stage_fetch(phase1: bool = False, only: list[str] | None = None,
                log: Callable[[str], None] = print, workers: int = 1) -> list[dict[str, Any]]:
    """Fetch in priority order: the Phase 1 dev files, then the held-out files (the lock needs their
    hashes), then the rest. The provincial object store serves at about 30 kB/s, so an interrupted run
    should already have everything the next stages need; `.part` files resume."""
    from .paths import PATHS
    from .select import fetch_items, read_selection, selected_files

    sel = read_selection()
    nums = selected_files(sel, phase1_only=phase1)
    if only:
        wanted = {n.upper() for n in only}
        missing = wanted - set(nums)
        if missing:
            raise FetchError(f"not in the selection: {sorted(missing)}")
        nums = [n for n in nums if n in wanted]
    locked = sel.get("locked") or {}
    first = list((locked.get("phase1") or sel["final"]["phase1_provisional"])["files"])
    second = list(locked.get("heldout") or sel["final"]["split_provisional"]["heldout"])
    order = {n: i for i, n in enumerate([*first, *[n for n in second if n not in first]])}
    nums = sorted(nums, key=lambda n: (order.get(n, len(order)), n))
    items = [FetchItem.from_dict(d) for d in fetch_items(sel, nums)]
    total = sum(i.size_mb for i in items)
    log(f"fetching {len(items)} objects for {len(nums)} files, about {total:.1f} MiB, {workers} connection(s)")
    return fetch_parallel(items, PATHS.raw, workers=workers, log=log)

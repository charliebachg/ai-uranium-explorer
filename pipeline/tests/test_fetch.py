"""Fetch: resume with Range, verification, skip on re-run, manifest and SHA256SUMS. No network."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from uranium_explorer.fetch import FetchError, Fetcher, FetchItem, documents_by_file, fetch_parallel, plan_paths

BODY = bytes(range(256)) * 400  # 102,400 bytes
MD5 = hashlib.md5(BODY).hexdigest()
SHA = hashlib.sha256(BODY).hexdigest()
URL = "https://store.example/nts/report.pdf"


def item(size_mb: float = len(BODY) / 1024 / 1024, url: str = URL, kind: str = "report_pdf") -> FetchItem:
    return FetchItem(file_num="74H09-0039", kind=kind, name="report.pdf", folder="x/Reports", url=url, size_mb=size_mb)


def transport(log: list[dict], body: bytes = BODY, etag: str | None = None, ignore_range: bool = False,
              fail_after: int | None = None) -> httpx.MockTransport:
    """A range-aware object store. `fail_after` drops the connection mid-body once."""
    state = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("Range")
        log.append({"range": rng, "url": str(request.url)})
        headers = {"etag": f'"{etag}"'} if etag else {}
        start = 0
        if rng and not ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(body):
                return httpx.Response(416, headers={**headers, "content-range": f"bytes */{len(body)}"})
            headers["content-range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
        chunk = body[start:]
        if fail_after is not None and not state["failed"]:
            state["failed"] = True
            # a truncated body with the full length advertised: the fetcher must notice and resume
            return httpx.Response(206 if (rng and not ignore_range) else 200,
                                  headers={**headers, "content-length": str(len(body) - start),
                                           **({"content-range": headers["content-range"]} if "content-range" in headers else {})},
                                  content=chunk[:fail_after])
        return httpx.Response(206 if (rng and not ignore_range) else 200,
                              headers={**headers, "content-length": str(len(chunk))}, content=chunk)

    return httpx.MockTransport(handler)


def fetcher(tmp_path, log, **kw) -> Fetcher:
    return Fetcher(raw_dir=tmp_path / "raw", client=httpx.Client(transport=transport(log, **kw)),
                   pace_s=0.0, sleep=lambda s: None, log=lambda *a: None)


def test_downloads_verifies_and_writes_the_manifest(tmp_path):
    log: list[dict] = []
    f = fetcher(tmp_path, log, etag=MD5)
    res = f.fetch_all([item()])
    assert res[0]["status"] == "downloaded"
    assert res[0]["bytes"] == len(BODY) and res[0]["sha256"] == SHA
    assert res[0]["etag_md5_verified"] is True and res[0]["size_note"] is None
    dest = f.raw_dir / "74H09-0039" / "report.pdf"
    assert dest.read_bytes() == BODY
    manifest = json.loads((f.raw_dir / "manifest.json").read_text())
    assert manifest["74H09-0039/report.pdf"]["sha256"] == SHA
    assert (f.raw_dir / "SHA256SUMS").read_text() == f"{SHA}  74H09-0039/report.pdf\n"
    assert log[0]["range"] is None and f.requests_made == 1


def test_resumes_from_a_part_file_with_a_range_request(tmp_path):
    log: list[dict] = []
    f = fetcher(tmp_path, log, etag=MD5)
    part = f.raw_dir / "74H09-0039" / "report.pdf.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(BODY[:40_000])
    res = f.fetch_all([item()])
    assert log[0]["range"] == "bytes=40000-"
    assert res[0]["sha256"] == SHA
    assert not part.exists()


def test_retries_and_resumes_after_a_dropped_connection(tmp_path):
    log: list[dict] = []
    f = fetcher(tmp_path, log, etag=MD5, fail_after=30_000)
    res = f.fetch_all([item()])
    assert len(log) == 2 and log[1]["range"] == "bytes=30000-"
    assert res[0]["sha256"] == SHA


def test_completed_part_answered_with_416_is_accepted(tmp_path):
    log: list[dict] = []
    f = fetcher(tmp_path, log, etag=MD5)
    part = f.raw_dir / "74H09-0039" / "report.pdf.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(BODY)
    res = f.fetch_all([item()])
    assert log[0]["range"] == f"bytes={len(BODY)}-"
    assert res[0]["sha256"] == SHA and res[0]["bytes"] == len(BODY)


def test_server_ignoring_the_range_restarts_the_file(tmp_path):
    log: list[dict] = []
    f = fetcher(tmp_path, log, etag=MD5, ignore_range=True)
    part = f.raw_dir / "74H09-0039" / "report.pdf.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(BODY[:1000])
    res = f.fetch_all([item()])
    assert res[0]["bytes"] == len(BODY) and res[0]["sha256"] == SHA


def test_second_run_skips_when_size_and_sha_match(tmp_path):
    log: list[dict] = []
    f = fetcher(tmp_path, log, etag=MD5)
    f.fetch_all([item()])
    again = fetcher(tmp_path, log2 := [], etag=MD5)
    res = again.fetch_all([item()])
    assert res[0]["status"] == "skipped" and log2 == []


def test_md5_mismatch_raises_and_removes_the_part(tmp_path):
    f = fetcher(tmp_path, [], etag="0" * 32)
    with pytest.raises(FetchError, match="does not match ETag"):
        f.fetch_all([item()])
    assert not (f.raw_dir / "74H09-0039" / "report.pdf").exists()
    assert not (f.raw_dir / "74H09-0039" / "report.pdf.part").exists()


def test_index_size_mismatch_is_reported_not_fatal(tmp_path):
    f = fetcher(tmp_path, [], etag=MD5)
    res = f.fetch_all([item(size_mb=99.0)])
    assert res[0]["status"] == "downloaded" and "differs" in res[0]["size_note"]


def test_total_size_guard(tmp_path):
    f = fetcher(tmp_path, [], etag=MD5)
    with pytest.raises(FetchError, match="exceeds"):
        f.fetch_all([item(size_mb=5000.0)], max_total_bytes=1024**3)


def test_paths_by_kind_and_name_collisions(tmp_path):
    items = [
        FetchItem("F1", "report_pdf", "a.pdf", "x/Reports", "u", 1),
        FetchItem("F1", "appendix_pdf", "b.pdf", "x/Digital Submissions/Appendix I", "u", 1),
        FetchItem("F1", "assay_xls", "c.xlsx", "x/Appendix B", "u", 1),
        FetchItem("F1", "certificate_pdf", "d.PDF", "x/Appendix A", "u", 1),
        FetchItem("F1", "report_pdf", "a.pdf", "x/Digital Submissions/Reports", "u", 1),
    ]
    paths = plan_paths(items, tmp_path)
    rel = [str(p.relative_to(tmp_path)) for p in paths.values()]
    assert rel[:4] == ["F1/a.pdf", "F1/appendix/b.pdf", "F1/assays/c.xlsx", "F1/certificates/d.PDF"]
    assert rel[4] != rel[0] and rel[4].endswith("_a.pdf")


def test_documents_by_file_reads_the_manifest(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "manifest.json").write_text(json.dumps({
        "F1/a.pdf": {"file_num": "F1", "kind": "report_pdf", "sha256": "aa", "bytes": 1, "name": "a.pdf"},
        "F1/assays/c.xlsx": {"file_num": "F1", "kind": "assay_xls", "sha256": "bb", "bytes": 1, "name": "c.xlsx"},
    }))
    docs = documents_by_file(raw)
    assert list(docs) == ["F1"] and [d["path"] for d in docs["F1"]] == ["F1/a.pdf"]


# ---------------------------------------------------------------- parallel connections


def many_items(n: int) -> list[FetchItem]:
    return [FetchItem(file_num="MAW00509", kind="appendix_pdf", name=f"PLS12-{i:03d}.pdf", folder="x/Appendix",
                      url=f"https://store.example/nts/PLS12-{i:03d}.pdf", size_mb=len(BODY) / 1024 / 1024)
            for i in range(n)]


def test_parallel_fetch_downloads_everything_once_and_keeps_one_manifest(tmp_path):
    log: list[dict] = []
    raw = tmp_path / "raw"
    make = lambda: Fetcher(raw_dir=raw, client=httpx.Client(transport=transport(log)), pace_s=0.0,
                           sleep=lambda s: None, log=lambda *a: None)
    res = fetch_parallel(many_items(7), raw, workers=3, log=lambda *a: None, make_fetcher=make)
    assert sorted(r["status"] for r in res) == ["downloaded"] * 7
    assert len({r["path"] for r in res}) == 7
    manifest = json.loads((raw / "manifest.json").read_text())
    assert len(manifest) == 7 and all(v["sha256"] == SHA for v in manifest.values())
    assert len((raw / "SHA256SUMS").read_text().splitlines()) == 7
    assert len({e["url"] for e in log}) == 7, "each object requested exactly once across the workers"


def test_a_second_parallel_run_skips_what_the_first_fetched(tmp_path):
    log: list[dict] = []
    raw = tmp_path / "raw"
    make = lambda: Fetcher(raw_dir=raw, client=httpx.Client(transport=transport(log)), pace_s=0.0,
                           sleep=lambda s: None, log=lambda *a: None)
    fetch_parallel(many_items(5), raw, workers=2, log=lambda *a: None, make_fetcher=make)
    n_requests = len(log)
    res = fetch_parallel(many_items(5), raw, workers=2, log=lambda *a: None, make_fetcher=make)
    assert [r["status"] for r in res] == ["skipped"] * 5
    assert len(log) == n_requests, "nothing is re-downloaded"


def test_one_worker_is_the_plain_fetcher(tmp_path):
    log: list[dict] = []
    res = fetch_parallel([item()], tmp_path / "raw", workers=1, log=lambda *a: None,
                         make_fetcher=lambda: fetcher(tmp_path, log))
    assert [r["status"] for r in res] == ["downloaded"]
    assert len(log) == 1, "one request, no threads, no network"

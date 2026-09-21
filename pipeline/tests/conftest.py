"""Shared fixtures: the three probe PDFs, rendered once per session, OCR'd on demand; and a sandbox that keeps
every test away from the real store's hash, snapshots and prospect output directory."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from uranium_explorer.ids import sha256_file
from uranium_explorer.paths import PATHS


@pytest.fixture(autouse=True)
def prospect_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """No test hashes the real store, reads the real snapshots, writes the real data/out/prospect, charges the
    real spend ledger or mirrors spans to the real MLflow store.

    The snapshot module's store path and snapshot directory, and the tracking module's output directory, point
    into the test's temporary directory; `make_store()` creates an empty store at that path for a test that
    wants a snapshot to name."""
    from uranium_explorer.prospect import tracking as TR
    from uranium_explorer.runtime import spend as SP
    from uranium_explorer.store import connect, write_meta
    from uranium_explorer.store import snapshot as SN

    root = tmp_path / "sandbox"
    db, snaps, out = root / "ue.duckdb", root / "snapshots", root / "out"
    snaps.mkdir(parents=True)
    monkeypatch.setattr(SN, "db_path", lambda: db)
    monkeypatch.setattr(SN, "snapshots_dir", lambda: snaps)
    monkeypatch.setattr(SN, "_git_commit", lambda: "deadbeef")
    monkeypatch.setattr(TR, "OUT_DIR", out)
    # the spend ledger and its ceilings are the sandbox's, and no test mirrors spans to the real MLflow store
    monkeypatch.setattr(SP, "ledger_path", lambda: root / "spend.jsonl")
    monkeypatch.setattr(SP, "legacy_ledger_path", lambda: root / "openai_spend.jsonl")
    monkeypatch.delenv("UE_MAX_SPEND_USD", raising=False)
    monkeypatch.setenv("UE_TRACING_MLFLOW", "0")

    def make_store() -> Path:
        con = connect(db)
        write_meta(con, "test")
        con.close()
        return db

    return SimpleNamespace(db=db, snapshots=snaps, out=out, make_store=make_store)

PROBE_DIR = PATHS.data / "probe"
PROBES = {
    "wollaston": PROBE_DIR / "wollaston.pdf",              # 1970s scan, no text layer
    "drilllog": PROBE_DIR / "drilllog_1980s.pdf",          # 1980 drill log rescanned 2016, corrupt OCR layer
    "modern": PROBE_DIR / "modern_geolmap.pdf",             # 2022 born-digital lab certificate
}


def probe_path(name: str) -> Path:
    path = PROBES[name]
    if not path.is_file():
        pytest.skip(f"probe PDF missing: {path}")
    return path


@pytest.fixture(scope="session")
def probe_pdfs() -> dict[str, Path]:
    missing = [n for n, p in PROBES.items() if not p.is_file()]
    if missing:
        pytest.skip(f"probe PDFs missing: {missing}")
    return dict(PROBES)


@pytest.fixture(scope="session")
def probes(probe_pdfs: dict[str, Path]) -> dict[str, dict]:
    from uranium_explorer.pdfprobe import probe_pdf

    return {n: probe_pdf(p, sha256_file(p)) for n, p in probe_pdfs.items()}


@pytest.fixture(scope="session")
def rendered(tmp_path_factory, probe_pdfs, probes) -> dict[tuple[str, int], dict]:
    """A few pages of each probe PDF, rendered at 200 dpi into a session temp directory."""
    from uranium_explorer.render import render_pdf

    root = tmp_path_factory.mktemp("pages")
    wanted = {"drilllog": (1, 6), "modern": (1, 3), "wollaston": (3,)}
    out: dict[tuple[str, int], dict] = {}
    for name, pages in wanted.items():
        pdf = probe_pdfs[name]
        probe = dict(probes[name])
        probe["pages"] = [p for p in probe["pages"] if p["page_no"] in pages]
        rows = render_pdf(pdf, probe["sha256"], name, root, probe, check_heldout=None, log=lambda *a: None)
        for r in rows:
            r["image_abs"] = root / probe["sha256"] / f"p{r['page_no']:04d}.png"
            out[(name, r["page_no"])] = r
    return out


@pytest.fixture(scope="session")
def ocred(tmp_path_factory, rendered) -> dict[tuple[str, int], dict]:
    from uranium_explorer.ocr import cache_path_for, ocr_image, vision_available

    if not vision_available():
        pytest.skip("Apple Vision (ocrmac) not available")
    root = tmp_path_factory.mktemp("ocr")
    out = {}
    for key, row in rendered.items():
        cache = cache_path_for(row["pdf_sha256"], row["page_no"], row["image_sha256"], root)
        out[key] = ocr_image(row["image_abs"], row["image_sha256"], cache)
    return out


def real_store_present() -> bool:
    """The analytics store is not in git; a test that reads it skips on a clone rather than failing."""
    from uranium_explorer.store import db_path

    return db_path().is_file()


def skip_without_store() -> None:
    import pytest

    if not real_store_present():
        pytest.skip("no analytics store on this machine (pipeline/data/ue.duckdb): see README, Quick start")

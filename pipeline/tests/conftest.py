"""Shared fixtures: the three probe PDFs, rendered once per session, OCR'd on demand."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacy_reader.ids import sha256_file
from legacy_reader.paths import PATHS

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
    from legacy_reader.pdfprobe import probe_pdf

    return {n: probe_pdf(p, sha256_file(p)) for n, p in probe_pdfs.items()}


@pytest.fixture(scope="session")
def rendered(tmp_path_factory, probe_pdfs, probes) -> dict[tuple[str, int], dict]:
    """A few pages of each probe PDF, rendered at 200 dpi into a session temp directory."""
    from legacy_reader.render import render_pdf

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
    from legacy_reader.ocr import cache_path_for, ocr_image, vision_available

    if not vision_available():
        pytest.skip("Apple Vision (ocrmac) not available")
    root = tmp_path_factory.mktemp("ocr")
    out = {}
    for key, row in rendered.items():
        cache = cache_path_for(row["pdf_sha256"], row["page_no"], row["image_sha256"], root)
        out[key] = ocr_image(row["image_abs"], row["image_sha256"], cache)
    return out

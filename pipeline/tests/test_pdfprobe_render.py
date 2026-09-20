"""pdfprobe on the three probe PDFs, and rendering one page of each into tmp_path."""

from __future__ import annotations

import json

import pytest
from PIL import Image

from uranium_explorer import render
from uranium_explorer.pdfprobe import classify_page, large_format, probe_cached
from uranium_explorer.render import HeldOutError, guard_not_heldout, page_id, read_pages, render_pdf, write_pages


def test_text_layer_kinds(probes):
    assert probes["wollaston"]["text_layer_kind"] == "none"
    assert probes["drilllog"]["text_layer_kind"] == "ocr_layer"
    assert probes["modern"]["text_layer_kind"] == "born_digital"


def test_scan_and_page_facts(probes):
    w, d, m = probes["wollaston"], probes["drilllog"], probes["modern"]
    assert w["n_pages"] == 26 and w["scanned"] and w["scan_dpi_estimate"] == 200
    assert all(p["text_chars"] == 0 for p in w["pages"])
    assert d["n_pages"] == 51 and d["scanned"] and d["scan_dpi_estimate"] == 300
    assert d["ocr_signature"] and not d["real_embedded_fonts"]
    assert {p["rotate"] for p in d["pages"]} >= {0, 90, 270}       # rescanned pages carry /Rotate
    assert not m["scanned"] and m["scan_dpi_estimate"] is None
    assert m["page_kinds"] == {"born_digital": 11}
    assert not m["ocr_signature"]


def test_drill_log_text_layer_holds_local_grid_strings(probes):
    hits = probes["drilllog"]["text_datum_hits"]
    assert hits["local_grid"] >= 5
    assert hits["nad27"] == 0 and hits["nad83"] == 0
    assert probes["modern"]["text_datum_hits"] == {}


@pytest.mark.parametrize("w_in,h_in,dpi,expected", [
    (8.5, 11.0, 200, None),            # letter
    (14.0, 8.5, 200, None),            # legal landscape (the drill log's wide pages)
    (11.0, 17.0, 200, None),           # tabloid
    (24.0, 36.0, 200, "longer than"),  # a map sheet
    (8.5, 44.0, 150, "longer than"),   # 74F08-0021's radiometric strip log
    (6.0, 16.0, 200, "strip format"),  # a narrow strip inside tabloid length
    (18.0, 22.5, 200, "megapixels"),
    (23.7, 30.6, 72, None),            # 74H09-0039: a letter-sized scan on an oversized page box
])
def test_large_format(w_in, h_in, dpi, expected):
    got = large_format(w_in * 72, h_in * 72, dpi)
    assert (got is None) == (expected is None), got
    if expected:
        assert expected in got


def test_render_dpi_follows_the_scan_resolution():
    from uranium_explorer.pdfprobe import render_dpi_for_page

    assert render_dpi_for_page(300) == 200        # never above the base dpi
    assert render_dpi_for_page(200) == 200
    assert render_dpi_for_page(150) == 150        # no point interpolating a 150 dpi scan
    assert render_dpi_for_page(72) == 72          # the oversized-page-box case
    assert render_dpi_for_page(None) == 200       # born-digital text


def test_classify_page_rules():
    assert classify_page(0, 1.0, True, False) == "none"
    assert classify_page(900, 1.0, True, False) == "ocr_layer"
    assert classify_page(900, 1.0, False, True) == "born_digital"   # full-page figure in a real document
    assert classify_page(900, 0.1, False, True) == "born_digital"


def test_probe_cache_is_reused(tmp_path, probe_pdfs):
    from uranium_explorer.ids import sha256_file

    pdf = probe_pdfs["modern"]
    sha = sha256_file(pdf)
    first = probe_cached(pdf, sha, tmp_path)
    assert (tmp_path / f"{sha}.json").is_file()
    (tmp_path / f"{sha}.json").write_text(json.dumps({**first, "producer": "cache marker"}))
    assert probe_cached(pdf, sha, tmp_path)["producer"] == "cache marker"


def test_render_one_page_of_each_probe_pdf(rendered):
    for name, page_no in [("wollaston", 3), ("drilllog", 1), ("modern", 1)]:
        row = rendered[(name, page_no)]
        assert row["rendered"] and row["image_sha256"]
        with Image.open(row["image_abs"]) as im:
            assert im.mode == "L"
            assert im.width > 1000 and im.height > 1000
            # 200 dpi of a letter-ish page
            assert abs(im.width - row["width_pt"] / 72 * 200) < 30 or row["pdf_rotate"] % 180 == 90
        thumb = row["image_abs"].with_name(f"t{page_no:04d}.png")
        with Image.open(thumb) as t:
            assert t.width == pytest.approx(im.width * 72 / 200, abs=2)


def test_render_skips_large_format_but_keeps_a_row(tmp_path, probe_pdfs, probes):
    pdf = probe_pdfs["drilllog"]
    probe = dict(probes["drilllog"])
    page = dict(probe["pages"][3])
    page["large_format"] = "24.0 x 36.0 in: at least 17 x 22 in"   # pretend it is a map sheet
    probe["pages"] = [page]
    rows = render_pdf(pdf, probe["sha256"], "drilllog", tmp_path, probe, check_heldout=None, log=lambda *a: None)
    assert len(rows) == 1 and rows[0]["rendered"] is False
    assert rows[0]["large_format"] and rows[0]["image_sha256"] is None
    assert not list(tmp_path.glob("*/p*.png"))


def test_render_refuses_held_out_files(tmp_path, probe_pdfs, probes):
    lock = tmp_path / "heldout.lock"
    sha = probes["wollaston"]["sha256"]
    lock.write_text(json.dumps({"file_nums": ["74H06-0039"],
                                "heldout": [{"file_num": "74H06-0039", "report_pdfs": [{"sha256": sha}]}]}))
    guard_not_heldout("74H09-0039", "cafe" * 16, lock)                      # a dev file is fine
    with pytest.raises(HeldOutError):
        guard_not_heldout("74H06-0039", "cafe" * 16, lock)                  # by file number
    with pytest.raises(HeldOutError):
        guard_not_heldout("74H09-0039", sha, lock)                          # by PDF hash
    with pytest.raises(HeldOutError, match="missing"):
        guard_not_heldout("74H09-0039", sha, tmp_path / "absent.lock")      # no lock at all


def test_pages_table_round_trip_and_column_merge(tmp_path):
    path = tmp_path / "pages.parquet"
    rows = [{"page_id": page_id("ab" * 32, 1), "file_num": "F1", "pdf_name": "a.pdf", "page_no": 1, "rendered": True},
            {"page_id": page_id("ab" * 32, 2), "file_num": "F1", "pdf_name": "a.pdf", "page_no": 2, "rendered": False}]
    write_pages(rows, path)
    assert [r["page_no"] for r in read_pages(path)] == [1, 2]
    render.update_pages({rows[0]["page_id"]: {"route_class": "assay_table"}}, path)
    back = read_pages(path)
    assert back[0]["route_class"] == "assay_table" and back[1]["route_class"] is None

"""OCR on rendered probe pages, box frames, and the text-layer trust decision."""

from __future__ import annotations

import pytest

from uranium_explorer import textlayer
from uranium_explorer.ocr import (cache_path_for, choose_rotation, needs_rotation_check, reading_quality,
                               vision_available)
from uranium_explorer.textlayer import agreement, pdftotext_words, rotate_box, token_kind, trusted

needs_vision = pytest.mark.skipif(not vision_available(), reason="Apple Vision (ocrmac) not available")


# ---------------------------------------------------------------- geometry helpers


@pytest.mark.parametrize("rot,box,expected", [
    (0, (0.1, 0.2, 0.3, 0.4), (0.1, 0.2, 0.3, 0.4)),
    (90, (0.0, 0.0, 0.2, 0.1), (0.0, 0.8, 0.1, 1.0)),      # top-left goes to the bottom-left corner
    (180, (0.0, 0.0, 0.2, 0.1), (0.8, 0.9, 1.0, 1.0)),
    (270, (0.0, 0.0, 0.2, 0.1), (0.9, 0.0, 1.0, 0.2)),     # top-left goes to the top-right corner
])
def test_rotate_box(rot, box, expected):
    assert rotate_box(box, rot) == pytest.approx(expected)


def test_rotate_box_round_trip():
    box = (0.11, 0.22, 0.33, 0.44)
    assert rotate_box(rotate_box(box, 90), 270) == pytest.approx(box)
    with pytest.raises(ValueError):
        rotate_box(box, 45)


def test_token_kind_ignores_single_characters():
    assert token_kind("121.7") == "num"
    assert token_kind("<0.01") == "num"
    assert token_kind("7") is None            # OCR cannot be relied on for isolated single characters
    assert token_kind("hematite") == "word"
    assert token_kind("of") is None
    assert token_kind("L2L.-7") == "num"


def test_reading_quality_and_rotation_trigger():
    good = [{"text": "SUMMARY LOG", "conf": 1.0, "x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.12}] * 4
    q = reading_quality(good, 1700, 2200)
    assert q["mean_conf"] == 1.0 and q["vertical_share"] == 0.0 and not needs_rotation_check(q)
    sideways = [{"text": "SUMMARY", "conf": 0.5, "x0": 0.1, "y0": 0.1, "x1": 0.12, "y1": 0.6}] * 4
    qs = reading_quality(sideways, 1700, 2200)
    assert qs["vertical_share"] == 1.0 and needs_rotation_check(qs)
    assert needs_rotation_check(reading_quality([], 10, 10))


# ---------------------------------------------------------------- OCR on a real page


@needs_vision
def test_ocr_one_rendered_page(ocred, rendered):
    rec = ocred[("drilllog", 1)]
    row = rendered[("drilllog", 1)]
    assert rec["rotation_ccw"] == 0
    assert len(rec["livetext_tokens"]) > 50 and len(rec["vision_lines"]) > 20
    texts = " ".join(t["text"] for t in rec["livetext_tokens"])
    assert "SUMMARY" in texts and "ELEVATION" in texts
    for t in rec["livetext_tokens"]:
        assert 0.0 <= t["x0"] <= t["x1"] <= 1.001 and 0.0 <= t["y0"] <= t["y1"] <= 1.001
    # the title sits in the top fifth of the page, in the frame we store
    title = next(t for t in rec["livetext_tokens"] if t["text"] == "SUMMARY")
    assert title["y1"] < 0.2
    assert rec["upright_px"] == [row["width_px"], row["height_px"]]
    assert rec["engine_version"].startswith("ocr/v1")


@needs_vision
def test_ocr_cache_key_is_the_image_hash(tmp_path, rendered):
    row = rendered[("modern", 1)]
    a = cache_path_for(row["pdf_sha256"], 1, row["image_sha256"], tmp_path)
    b = cache_path_for(row["pdf_sha256"], 1, "00" * 32, tmp_path)
    assert a != b and a.name.startswith("p0001-")


@needs_vision
def test_rotation_is_chosen_for_a_sideways_page(rendered):
    from PIL import Image

    with Image.open(rendered[("drilllog", 1)]["image_abs"]) as im:
        upright = im.convert("L")
    rot, lines, tried = choose_rotation(upright.rotate(270, expand=True))   # hand it a sideways page
    assert rot == 90                                                        # turned back counter-clockwise
    assert set(tried) == {"0", "90", "270"}
    assert tried["90"]["score"] > tried["0"]["score"]
    assert "SUMMARY" in " ".join(ln["text"] for ln in lines)


# ---------------------------------------------------------------- text layer trust


@needs_vision
def test_corrupt_scanner_layer_is_untrusted_and_born_digital_is_trusted(ocred, rendered, probe_pdfs, probes):
    drill = pdftotext_words(probe_pdfs["drilllog"], {p["page_no"]: p["rotate"] for p in probes["drilllog"]["pages"]})
    rec = ocred[("drilllog", 1)]
    ag = agreement(drill[1], rec["livetext_tokens"], rec["vision_lines"])
    assert ag["eligible_tokens"] > 50
    assert ag["agreement"] < textlayer.TRUST_THRESHOLD
    assert ag["numeric_agreement"] < 0.6        # the depths are the worst part ("LZ.5" for 12.5)
    assert not trusted(ag["agreement"], "ocr_layer", probes["drilllog"]["ocr_signature"])

    modern = pdftotext_words(probe_pdfs["modern"])
    rec = ocred[("modern", 1)]
    ag_m = agreement(modern[1], rec["livetext_tokens"], rec["vision_lines"])
    assert ag_m["agreement"] >= textlayer.TRUST_THRESHOLD
    assert trusted(ag_m["agreement"], "born_digital", probes["modern"]["ocr_signature"])


def test_trust_needs_both_agreement_and_a_born_digital_signature():
    assert trusted(0.99, "born_digital", False)
    assert not trusted(0.99, "ocr_layer", False)       # a scanner layer is never trusted
    assert not trusted(0.99, "born_digital", True)     # nor is anything from a scanner producer
    assert not trusted(0.89, "born_digital", False)
    assert not trusted(None, "born_digital", False)


def test_agreement_counts_missing_text_separately():
    layer = [{"text": "121.7", "x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.12, "line_id": 0},
             {"text": "hematite", "x0": 0.3, "y0": 0.1, "x1": 0.5, "y1": 0.12, "line_id": 0},
             {"text": "155.8", "x0": 0.1, "y0": 0.3, "x1": 0.2, "y1": 0.32, "line_id": 1},
             {"text": "9999.9", "x0": 0.7, "y0": 0.8, "x1": 0.8, "y1": 0.82, "line_id": 2}]
    ocr = [{"text": "121.7", "x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.12, "line_id": 0, "conf": 1.0},
           {"text": "hernatite", "x0": 0.3, "y0": 0.1, "x1": 0.5, "y1": 0.12, "line_id": 0, "conf": 1.0},
           {"text": "155.8", "x0": 0.1, "y0": 0.3, "x1": 0.2, "y1": 0.32, "line_id": 1, "conf": 1.0}]
    ag = agreement(layer, ocr)
    assert ag["ocr_missing"] == 1                 # nothing was read where the layer has 9999.9
    assert ag["eligible_tokens"] == 6             # three layer tokens and three OCR tokens
    # four exact matches plus the hematite/hernatite pair scored by character ratio, both directions
    assert ag["agreement"] == pytest.approx((4 * 1.0 + 2 * 0.8235) / 6, abs=0.001)
    assert ag["numeric_tokens"] == 4
    assert ag["numeric_agreement"] is None        # fewer than five numeric tokens: not reported


def test_pdftotext_words_normalise_to_the_rendered_frame(probe_pdfs, probes):
    rotations = {p["page_no"]: p["rotate"] for p in probes["drilllog"]["pages"]}
    words = pdftotext_words(probe_pdfs["drilllog"], rotations, first=2, last=2)
    assert words[2]
    for w in words[2]:
        assert 0.0 <= w["x0"] <= w["x1"] <= 1.0 and 0.0 <= w["y0"] <= w["y1"] <= 1.0


def test_pdftotext_words_on_a_pdf_that_breaks_bbox_output(probe_pdfs):
    """poppler's -bbox writer aborts on this file; the TSV reader must still work."""
    words = pdftotext_words(probe_pdfs["modern"], {}, first=1, last=1)
    assert len(words[1]) > 100
    assert any("Geomap" in w["text"] for w in words[1])
    assert max(w["line_id"] for w in words[1]) > 5

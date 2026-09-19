"""The corpus text index: the text layer where it can be trusted, the OCR pass everywhere else it reached."""

from __future__ import annotations

from pathlib import Path

from legacy_reader.ocr import write_words
from legacy_reader.prospect import corpus as C


def _words(page: int, lines: list[str], engine: str = "vision") -> list[dict]:
    return [{"page_no": page, "engine": engine, "text": t, "x0": 0.1, "y0": 0.1 + 0.05 * i, "x1": 0.9,
             "y1": 0.12 + 0.05 * i, "conf": 0.9, "line_id": i, "word_no": i, "rotation_ccw": 0}
            for i, t in enumerate(lines)]


def test_ocr_page_texts_reads_vision_lines_in_reading_order(tmp_path: Path) -> None:
    sha = "a" * 64
    words = _words(3, ["Drill hole PLS12-023 collar", "Depth 412.5 m, azimuth 030", "Graphitic pelite from 380 m"])
    words += _words(3, ["Drill", "hole"], engine="livetext")          # tokens are not lines: ignored
    words += _words(4, ["Fig. 2"])                                     # too short to be a page of text
    write_words(words, tmp_path / sha / "words.parquet")
    out = C.ocr_page_texts(sha, root=tmp_path)
    assert [p for p, _ in out] == [3]
    assert out[0][1].splitlines() == ["Drill hole PLS12-023 collar", "Depth 412.5 m, azimuth 030", "Graphitic pelite from 380 m"]
    assert C.ocr_page_texts("b" * 64, root=tmp_path) == []


def test_merge_prefers_the_text_layer_unless_it_is_distrusted() -> None:
    layer = [(1, "born digital page one"), (2, "L2L.-7 garbage from the scanner's own OCR")]
    ocr = [(2, "121.7 read by the second reader"), (3, "a scanned page only the OCR pass covered")]
    merged = C.merge_page_texts(layer, ocr, untrusted={2})
    assert merged == [(1, "born digital page one", "text_layer"),
                      (2, "121.7 read by the second reader", "ocr"),
                      (3, "a scanned page only the OCR pass covered", "ocr")]
    # trusted everywhere: the text layer wins on the pages it has, OCR fills the rest
    assert [s for _, _, s in C.merge_page_texts(layer, ocr)] == ["text_layer", "text_layer", "ocr"]


def test_a_document_filed_twice_is_indexed_once() -> None:
    rows = [{"file_num": "A", "doc_name": "r.pdf", "doc_sha256": "s", "page": 1, "text": "x"},
            {"file_num": "B", "doc_name": "copy of r.pdf", "doc_sha256": "s", "page": 1, "text": "x"},
            {"file_num": "A", "doc_name": "r.pdf", "doc_sha256": "s", "page": 2, "text": "y"}]
    kept, dropped = C.dedupe_pages(rows)
    assert dropped == 1 and [(r["file_num"], r["page"]) for r in kept] == [("A", 1), ("A", 2)]

"""Cell-level report text for benchmark v3: outcomes, numbers and names redacted, passages chosen by family from
the redacted text, the same cap for every cell, a placebo that gives each cell another's passages. The page
extraction (poppler, Apple Vision) is replaced by fixed pages; the store is a fake with three cells."""

from __future__ import annotations

import pandas as pd
import pytest

from uranium_explorer.bench import celltext as CT
from uranium_explorer.bench.build import outcome_leaks
from uranium_explorer.bench.spec import TextSpec

SPEC = TextSpec(radius_km=2.0, fallback_km=5.0, files_per_cell=2, max_pages=5, passages=4, passage_chars=220,
                min_chars=60)

PAGE = ("The basement comprises graphitic pelitic gneiss with garnet and cordierite beneath the unconformity. "
        "Hole MC-123 intersected 4.2% U3O8 over 3.5 m at 512 m depth. "
        "The sandstone above the unconformity is bleached and strongly clay altered with illite and dravite. "
        "A broad shear zone with graphite and breccia offsets the unconformity by about 30 m. "
        "Radiometric anomalies were traced for 800 m along the conductor. "
        "Chlorite and hematite alteration extends into the regolith of the basement rocks here. "
        "The McClean property was drilled by the operator in 1979 with nine holes.")


def scrub(t: str) -> str:
    return t.replace("McClean", "[redacted]").replace("MC-123", "[redacted]")


def test_a_sentence_that_states_an_outcome_is_dropped_and_numbers_become_n() -> None:
    kept = [CT.redact(s, scrub) for s in CT.sentences(PAGE) if CT.prose(s)]
    text = " ".join(k for k in kept if k)
    assert "U3O8" not in text and "Radiometric" not in text and "intersected" not in text
    assert "graphitic pelitic gneiss" in text and "illite and dravite" in text
    assert "about [n] m" in text and "in [n] with nine holes" in text
    assert "McClean" not in text


def test_outcome_leaks_flags_what_redaction_should_have_removed() -> None:
    payload = {"passages": [{"passage_id": "p-01", "text": "Strong illite alteration."},
                            {"passage_id": "p-02", "text": "The ore zone lies at depth."},
                            {"passage_id": "p-03", "text": "A shear offset of 30 m."}]}
    got = outcome_leaks(payload)
    assert any("p-02" in g and "ore" in g for g in got) and any("p-03" in g and "digit" in g for g in got)
    assert not any("p-01" in g for g in got)


def test_passages_are_chosen_by_family_in_turn_up_to_the_cap() -> None:
    cands = CT.cell_passages([{"file": "74H-0001", "doc_sha256": "a" * 64, "distance_km": 0.4,
                               "pages": [{"page": 3, "source": "text_layer", "text": PAGE}]}], SPEC, scrub)
    red = cands["redacted"]
    assert red and all(CT.OUTCOME.search(c["text"]) is None for c in red)
    bm25 = CT.BM25([c["text"] for c in red])
    chosen = CT.select(red, bm25, 0, SPEC)
    assert 1 <= len(chosen) <= SPEC.passages and len({c["text"] for c in chosen}) == len(chosen)
    rows, sources = CT.passage_rows(chosen, numbers_allowed=False)
    assert rows[0]["passage_id"] == "p-01" and "file" not in rows[0] and "doc_sha256" not in rows[0]
    assert sources[0]["file"] == "74H-0001" and sources[0]["page"] == 3, "the sources keep what the rows drop"


def test_the_placebo_never_gives_a_cell_its_own_passages() -> None:
    by = {"b-0001": [1], "b-0002": [1], "b-0003": [1], "b-0004": []}
    m = CT.swapped(by, seed=7)
    assert set(m) == {"b-0001", "b-0002", "b-0003"} and all(a != b for a, b in m.items())
    assert sorted(m.values()) == sorted(m), "a permutation: every cell's passages go to exactly one other"
    assert CT.swapped(by, seed=7) == m, "fixed by the seed"


def test_files_are_ranked_by_holes_in_reach_and_fall_back_to_five_km() -> None:
    holes = pd.DataFrame([("A", 100.0, 0.0, "h1"), ("A", 200.0, 0.0, "h2"), ("B", 50.0, 0.0, "h3"),
                          ("C", 4000.0, 0.0, "h4")], columns=["file", "x", "y", "hole"])
    got = CT.cell_files({"near": (0.0, 0.0), "far": (7000.0, 0.0), "none": (90_000.0, 0.0)}, SPEC, holes)
    assert [f for f, _d in got["near"]] == ["A", "B"], "two holes beat one closer hole"
    assert got["far"] == [("C", 3.0)], "nothing within 2 km, so 5 km"
    assert got["none"] == []


def test_build_texts_caps_every_cell_and_keeps_both_views(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from uranium_explorer.prospect import corpus as C

    class Con:
        def execute(self, sql: str, params: list | None = None):
            class R:
                def fetchall(self_inner):
                    return [("0001_0001", 0.0, 0.0), ("0001_0002", 50_000.0, 0.0)]
            return R()

    holes = pd.DataFrame([("A", 100.0, 0.0, "MC-123")], columns=["file", "x", "y", "hole"])
    monkeypatch.setattr(CT, "hole_table", lambda: holes)
    monkeypatch.setattr(C, "documents_on_disk", lambda log=None: {"A": [{"path": tmp_path / "a.pdf", "name": "a.pdf"}]})
    monkeypatch.setattr(CT, "document_pages", lambda pdf, n: {"doc_sha256": "a" * 64, "pages": [
        {"page": 1, "source": "ocr", "text": PAGE * 3}]})
    import uranium_explorer.bench.pack as P

    monkeypatch.setattr(P, "hole_names", lambda con: set())
    out = CT.build_texts([("b-0001", "0001_0001"), ("b-0002", "0001_0002")], SPEC, Con(), {"McClean"},
                         log=lambda _m: None)
    assert 1 <= len(out["b-0001"]["redacted"]) <= SPEC.passages and out["b-0002"]["redacted"] == []
    assert not outcome_leaks({"passages": out["b-0001"]["redacted"]})
    raw = " ".join(r["text"] for r in out["b-0001"]["raw"])
    assert "MC-123" not in raw and "McClean" not in raw, "the raw view keeps outcomes, never names"
    assert out["b-0001"]["sources"][0]["ocr"] is True


def test_no_digit_survives_and_a_unit_glued_to_a_number_is_still_an_outcome() -> None:
    s = "The L4 zone near conductor A2 and hole 2O7 (Fig. 3A) sits in the Manitou Fal1s sandstone with strong clay."
    out = CT.redact(s, scrub)
    assert out is not None and not any(c.isdigit() for c in out)
    assert "zone near conductor [n] and hole [n]" in out
    assert CT.redact("Readings of 1000cps were logged in the altered sandstone above the unconformity.", scrub) is None

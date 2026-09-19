"""Enabling files outside the original shortlist: they ride along as dev files, never held-out ones."""

from __future__ import annotations

from pathlib import Path

import pytest

from legacy_reader import extract, select
from legacy_reader.select import enabled_files, fetch_items, probe_entry, register_enabled, selected_files


def listing(name: str = "MAW509_report.pdf", size_mb: float = 3.0, extra: list[dict] | None = None) -> list[dict]:
    """A table-23 listing in the shape classify_listing reads: one report PDF in a Reports folder.
    FILE_SIZE is already in MiB on the live listing, so it is here too."""
    base = {"FILE_LCATN": "NTS 74 FILES/NTS 74-F/74-F-11/MAW509/Reports", "FILE_NAME": name,
            "FILE_SIZE": size_mb, "FILE_LNK": f"https://example.invalid/{name}"}
    return [base, *(extra or [])]


def selection() -> dict:
    return {"final": {"selected": ["A1", "B2"], "phase1_provisional": {"files": ["A1"]}},
            "locked": {"phase1": {"files": ["A1"]}, "heldout": ["B2"]},
            "probe": {"files": {"A1": {"report_pdfs": [{"name": "a.pdf", "folder": "f", "size_mb": 1.0,
                                                          "url": "u", "kind": "report_pdf"}],
                                       "appendix_pdfs": [], "assay_xls": [], "certificate_pdfs": []}}}}


def test_an_enabled_file_rides_along_as_a_dev_file_in_both_views() -> None:
    sel = selection()
    register_enabled(sel, "MAW00509", listing(), "enabled cell", heldout=set(), now="2026-09-19T00:00:00+00:00")
    assert enabled_files(sel) == ["MAW00509"]
    assert selected_files(sel, phase1_only=True) == ["A1", "MAW00509"]
    assert selected_files(sel) == ["A1", "B2", "MAW00509"]


def test_a_held_out_file_is_never_enabled() -> None:
    sel = selection()
    with pytest.raises(PermissionError):
        register_enabled(sel, "B2", listing(), "why", heldout=set())
    with pytest.raises(PermissionError):
        register_enabled(sel, "Z9", listing(), "why", heldout={"Z9"})
    assert "enabled" not in sel


def test_fetch_items_reads_the_enabled_listing_and_skips_a_data_dvd() -> None:
    sel = selection()
    dvd = {"FILE_LCATN": "NTS 74 FILES/NTS 74-F/74-F-11/MAW509/Digital Submissions", "FILE_NAME": "data.pdf",
           "FILE_SIZE": 900.0, "FILE_LNK": "https://example.invalid/data.pdf"}
    entry = register_enabled(sel, "MAW00509", listing(extra=[dvd]), "why", heldout=set(), max_item_mb=60.0)
    items = fetch_items(sel, ["MAW00509"])
    assert [i["name"] for i in items] == ["MAW509_report.pdf"], "the 900 MiB object is not queued"
    assert entry["skipped_over_mb"] and entry["skipped_over_mb"][0]["name"] == "data.pdf"
    assert items[0]["file_num"] == "MAW00509"


def test_probe_entry_fails_loudly_for_a_file_nobody_listed() -> None:
    with pytest.raises(KeyError):
        probe_entry(selection(), "NOPE")


def test_enabling_twice_keeps_one_entry_and_the_latest_reason() -> None:
    sel = selection()
    register_enabled(sel, "MAW00509", listing(), "first", heldout=set(), now="t1")
    register_enabled(sel, "MAW00509", listing(), "second", heldout=set(), now="t2")
    assert enabled_files(sel) == ["MAW00509"]
    assert sel["enabled"]["reasons"]["MAW00509"] == "second"


# ---------------------------------------------------------------- workers in the extract config


def test_the_config_reads_workers_and_defaults_to_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "configs_dir", lambda: tmp_path)
    (tmp_path / "one.toml").write_text('[extract]\nmodel = "claude-opus-5"\nworkers = 1\n')
    (tmp_path / "two.toml").write_text('[extract]\nmodel = "claude-opus-5"\n')
    (tmp_path / "zero.toml").write_text('[extract]\nmodel = "claude-opus-5"\nworkers = 0\n')
    assert extract.load_config("one").workers == 1
    assert extract.load_config("two").workers == extract.WORKERS == 2
    assert extract.load_config("zero").workers == 1, "a worker count below one is clamped, not honoured"
    assert extract.load_config("one").as_dict()["workers"] == 1


def test_the_opus_config_on_disk_is_opus_only_with_one_worker() -> None:
    cfg = extract.load_config("opus1")
    assert cfg.model == "claude-opus-5" and cfg.workers == 1

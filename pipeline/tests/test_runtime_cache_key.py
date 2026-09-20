"""B22: the cache key covers the prompt and schema texts, and old records move to the key a live request computes."""

from __future__ import annotations

import json
from pathlib import Path

from uranium_explorer import wire
from uranium_explorer.backends.base import ExtractionRequest, ExtractionResponse, hash_schema, hash_text
from uranium_explorer.backends.cache import RECORD_VERSION, CachedBackend, build_record, record_path, write_atomic
from uranium_explorer.extract import carry_from_result, context_hash
from uranium_explorer.prompts import prompt_version, system_prompt, user_prompt
from uranium_explorer.runtime import rekey as RK

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}, "required": ["a"]}


def make_request(tmp_path: Path, **over) -> ExtractionRequest:
    img = over.pop("image", None)
    if img is None:
        img = tmp_path / "page.png"
        if not img.exists():
            img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 64)
    base = dict(task="extract_page", images=(img,), system_prompt="sys", user_prompt="Read {STAGE_DIR}/page.png",
                schema=SCHEMA, schema_version="s1", prompt_version="p1", model="claude-sonnet-5",
                effort="medium", context_hash="", render_params={"dpi": 200})
    base.update(over)
    return ExtractionRequest(**base)


class FakeBackend:
    family = "claude_cli"

    def __init__(self, structured=None):
        self.structured = structured or {"ok": True}
        self.calls = 0

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        self.calls += 1
        return ExtractionResponse(structured=self.structured, envelope={}, backend="claude_cli",
                                  backend_version="0", model_requested=req.model, model_resolved=req.model,
                                  num_turns=4, duration_s=1.0, usage={"output_tokens": 10}, cost_usd=0.05,
                                  cache_key=req.cache_key(self.family))


# ---------------------------------------------------------------- the key


def test_the_key_changes_with_each_text_and_holds_for_an_equal_request(tmp_path):
    base = make_request(tmp_path).cache_key("claude_cli")
    assert make_request(tmp_path).cache_key("claude_cli") == base, "an equal request keeps its key"
    changed = {
        "system prompt": make_request(tmp_path, system_prompt="sys, edited"),
        "user prompt": make_request(tmp_path, user_prompt="Read {STAGE_DIR}/page.png carefully"),
        "schema": make_request(tmp_path, schema={**SCHEMA, "properties": {"b": {"type": "string"}}, "required": ["b"]}),
    }
    keys = {name: req.cache_key("claude_cli") for name, req in changed.items()}
    for name, key in keys.items():
        assert key != base, f"editing the {name} must change the key"
    assert len(set(keys.values())) == 3, "each edit lands on its own key"


def test_the_hashes_are_of_the_texts_exactly_as_given(tmp_path):
    req = make_request(tmp_path)
    assert req.system_hash() == hash_text("sys")
    assert req.user_hash() == hash_text("Read {STAGE_DIR}/page.png"), "the placeholder, not a temp path, is hashed"
    assert req.schema_hash() == hash_schema(SCHEMA)
    reordered = {"required": ["a"], "properties": {"a": {"type": "string"}}, "additionalProperties": False, "type": "object"}
    assert hash_schema(reordered) == hash_schema(SCHEMA), "key order is not a schema change"
    assert hash_schema({**SCHEMA, "required": []}) != hash_schema(SCHEMA)


def test_a_new_record_is_v2_and_carries_the_three_hashes(tmp_path):
    req = make_request(tmp_path)
    resp = CachedBackend(FakeBackend(), root=tmp_path / "cache").call(req)
    rec = json.loads(record_path(resp.cache_key, tmp_path / "cache").read_text())
    assert rec["version"] == RECORD_VERSION == "call-record/v2"
    assert rec["request"]["system_hash"] == req.system_hash()
    assert rec["request"]["user_hash"] == req.user_hash()
    assert rec["request"]["schema_hash"] == req.schema_hash()


# ---------------------------------------------------------------- the rekey

PDF = "f" * 64


def _v1_record(root: Path, req: ExtractionRequest, structured: dict) -> str:
    """Write a record the way the old code did: version v1, no text hashes, the old key. Returns that key."""
    resp = FakeBackend(structured).call(req)
    rec = build_record(req, resp)
    for k in ("system_hash", "user_hash", "schema_hash"):
        rec["request"].pop(k)
    rec["request"].pop("stage_files")
    old_key = RK.v1_key(RK.key_fields_from_record(rec, "claude_cli"))
    rec["version"] = RK.V1
    rec["cache_key"] = old_key
    write_atomic(record_path(old_key, root), json.dumps(rec) + "\n")
    return old_key


def _extract_request(tmp_path: Path, name: str, classes: list[str], carry=(), **over) -> ExtractionRequest:
    img = tmp_path / name
    img.write_bytes(b"\x89PNG" + name.encode() * 8)
    fields = dict(image=img, system_prompt=system_prompt(), user_prompt=user_prompt(classes, list(carry)),
                  schema=wire.WIRE_SCHEMA, schema_version=wire.SCHEMA_VERSION, prompt_version=prompt_version(),
                  context_hash=context_hash(list(carry)), render_params={"dpi": 200, "mode": "gray"})
    return make_request(tmp_path, **{**fields, **over})


def _seed_cache(tmp_path: Path, monkeypatch) -> dict:
    """A cache with one record of each kind the rekey must tell apart, plus the results and index rows that
    let the two movable ones be rebuilt."""
    root = tmp_path / "cache"
    page1 = {"page_level": {"hole_id": {"printed": "printed", "value_as_printed": "R-78-27", "quote": "R-78-27"}},
             "tables": [{"column_headers_as_printed": ["From", "To", "U3O8"], "title_as_printed": "Assays"}]}
    carry = carry_from_result(page1, 1)
    assert carry, "the fixture must exercise the carry"

    plain = _extract_request(tmp_path, "p0001.png", ["assay_table", "lith_log"])
    cont = _extract_request(tmp_path, "p0002.png", ["assay_table"], carry)
    orphan = _extract_request(tmp_path, "p0009.png", ["assay_table"])
    stale = _extract_request(tmp_path, "p0003.png", ["assay_table"], prompt_version="000000000000")
    memo = make_request(tmp_path, task="prospect_memo_skeptic", system_prompt="rules", user_prompt="memo",
                        schema={"type": "object"}, schema_version="1.0.0", prompt_version="prospect/memo/v1")

    keys = {name: _v1_record(root, req, {"n": i})
            for i, (name, req) in enumerate({"plain": plain, "cont": cont, "orphan": orphan, "stale": stale,
                                             "memo": memo}.items())}
    results = {
        "pg:1": {"page_id": "pg:1", "cache_key": keys["plain"], "route_class": "assay_table", "chain_pos": 1,
                 "pdf_sha256": PDF, "page_no": 1, "result": page1},
        "pg:2": {"page_id": "pg:2", "cache_key": keys["cont"], "route_class": "assay_table", "chain_pos": 2,
                 "pdf_sha256": PDF, "page_no": 2, "result": {}},
        "pg:3": {"page_id": "pg:3", "cache_key": keys["stale"], "route_class": "assay_table", "chain_pos": 1,
                 "pdf_sha256": PDF, "page_no": 3, "result": {}},
    }
    index = [{"page_id": "pg:1", "route_class": "assay_table", "route_candidates": "lith_log"},
             {"page_id": "pg:2", "route_class": "lith_log", "route_candidates": ""},   # re-routed since it was read
             {"page_id": "pg:3", "route_class": "assay_table", "route_candidates": ""}]
    monkeypatch.setattr(RK, "results_rows", lambda: results)
    monkeypatch.setattr(RK, "page_rows", lambda: index)
    return {"root": root, "keys": keys, "live": {"plain": plain, "cont": cont}}


def test_rekey_moves_matching_extract_records_and_leaves_the_rest_alone(tmp_path, monkeypatch):
    seed = _seed_cache(tmp_path, monkeypatch)
    root, keys = seed["root"], seed["keys"]
    lines: list[str] = []
    counts = RK.rekey(root, log=lines.append)
    assert counts == {"files": 5, "rekeyed": 2, "skipped_other_task": 1, "skipped_version_mismatch": 1,
                      "already_v2": 0, "already_rekeyed": 0, "unrebuildable": 1}
    assert any("no extract results row" in ln for ln in lines)

    for name, req in seed["live"].items():
        new_key = req.cache_key("claude_cli")
        moved = json.loads(record_path(new_key, root).read_text())
        assert moved["version"] == "call-record/v2" and moved["cache_key"] == new_key
        assert moved["rekeyed_from"] == keys[name]
        assert moved["request"]["user_hash"] == req.user_hash()
        assert moved["request"]["system_hash"] == req.system_hash()
        assert moved["request"]["schema_hash"] == req.schema_hash()
        assert record_path(keys[name], root).is_file(), "the old file is left in place"
        # and the point of it all: a live request is now served from the moved record
        served = CachedBackend(FakeBackend(), root=root).cached(req)
        assert served is not None and served.structured == moved["response"]["structured"]
    for name in ("orphan", "stale", "memo"):
        assert record_path(keys[name], root).is_file()
        assert json.loads(record_path(keys[name], root).read_text())["version"] == RK.V1


def test_rekey_is_idempotent(tmp_path, monkeypatch):
    seed = _seed_cache(tmp_path, monkeypatch)
    first = RK.rekey(seed["root"], log=lambda *a: None)
    before = sorted(p.name for p in (seed["root"] / "calls").rglob("*.json"))
    second = RK.rekey(seed["root"], log=lambda *a: None)
    assert first["rekeyed"] == 2 and second["rekeyed"] == 0
    assert second["already_rekeyed"] == 2 and second["already_v2"] == 2
    assert second["files"] == 7
    assert sorted(p.name for p in (seed["root"] / "calls").rglob("*.json")) == before, "nothing new is written"


def test_rekey_dry_run_counts_without_writing(tmp_path, monkeypatch):
    seed = _seed_cache(tmp_path, monkeypatch)
    before = sorted(p.name for p in (seed["root"] / "calls").rglob("*.json"))
    counts = RK.rekey(seed["root"], log=lambda *a: None, dry_run=True)
    assert counts["rekeyed"] == 2
    assert sorted(p.name for p in (seed["root"] / "calls").rglob("*.json")) == before


def test_a_record_whose_own_key_does_not_recompute_is_not_moved(tmp_path, monkeypatch):
    seed = _seed_cache(tmp_path, monkeypatch)
    path = record_path(seed["keys"]["plain"], seed["root"])
    rec = json.loads(path.read_text())
    rec["request"]["effort"] = "high"     # the record no longer describes the call its key names
    path.write_text(json.dumps(rec))
    counts = RK.rekey(seed["root"], log=lambda *a: None)
    assert counts["rekeyed"] == 1 and counts["unrebuildable"] == 2


def test_stats_counts_records_by_task_and_version(tmp_path, monkeypatch):
    seed = _seed_cache(tmp_path, monkeypatch)
    RK.rekey(seed["root"], log=lambda *a: None)
    assert RK.stats(seed["root"]) == {("extract_page", RK.V1): 4, ("extract_page", "call-record/v2"): 2,
                                       ("prospect_memo_skeptic", RK.V1): 1}

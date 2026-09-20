"""Phase 0 backend probe: one real call that answers the questions the extraction design depends on.

- Does `claude -p --json-schema` return structured output for a page image read with the Read tool?
- What does the envelope look like (saved as a replay fixture)?
- How many input tokens does a 200 dpi letter page cost, and is small print legible?
- Does the model say "not printed" instead of inventing a datum for a local-grid collar?
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .backends.base import ExtractionRequest
from .backends.claude_cli import ClaudeCliBackend
from .ids import sha256_file, sha256_json, short
from .paths import PATHS


def _nullable(t: str) -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


_PRINTED = {"type": "string", "enum": ["printed", "not_printed", "illegible"]}
_CELL = {
    "type": "object", "additionalProperties": False,
    "required": ["value_as_printed", "quote", "printed"],
    "properties": {"value_as_printed": _nullable("string"), "quote": _nullable("string"), "printed": _PRINTED},
}

PROBE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["page_kind", "hole_id", "header_fields", "coordinates", "probe_peaks", "sample_zones",
                 "file_stamp", "legibility"],
    "properties": {
        "page_kind": {"type": "string", "enum": ["collar_or_summary_log", "lithology_log", "assay_table",
                                                  "probe_log", "certificate", "map", "text", "other"]},
        "hole_id": _CELL,
        "header_fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["label_as_printed", "value_as_printed", "unit_as_printed", "quote", "printed"],
            "properties": {"label_as_printed": {"type": "string"}, "value_as_printed": _nullable("string"),
                           "unit_as_printed": _nullable("string"), "quote": _nullable("string"),
                           "printed": _PRINTED}}},
        "coordinates": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "datum_as_printed", "value_as_printed", "quote"],
            "properties": {"kind": {"type": "string", "enum": ["utm", "geographic", "local_grid", "not_printed"]},
                           "datum_as_printed": _nullable("string"), "value_as_printed": _nullable("string"),
                           "quote": _nullable("string")}},
        "probe_peaks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["reading_as_printed", "depth_as_printed", "quote"],
            "properties": {"reading_as_printed": {"type": "string"}, "depth_as_printed": _nullable("string"),
                           "quote": {"type": "string"}}}},
        "sample_zones": _CELL,
        "file_stamp": _CELL,
        "legibility": {
            "type": "object", "additionalProperties": False,
            "required": ["smallest_text_readable", "notes"],
            "properties": {"smallest_text_readable": {"type": "boolean"}, "notes": {"type": "string"}}},
    },
}

SYSTEM = """You transcribe scanned mineral exploration records. You never interpret geology.
Rules:
- Use the Read tool once on the image path you are given, then answer.
- Every value is copied exactly as printed, including odd spacing, units and symbols. Do not convert units.
- Every value comes with a verbatim quote of the printed text it came from.
- A datum, unit, zone or coordinate system is reported only if it is printed on the page. If it is not printed, say not_printed. Never infer one.
- If something is printed but you cannot read it, mark it illegible instead of guessing.
- Answer only through the required JSON structure."""

USER = """Read the page image at {STAGE_DIR}/page.png.
Transcribe the page header fields (label, value, unit as printed), the hole identifier, the coordinates block (say whether it is UTM, geographic, a local grid, or not printed, and give the datum only if printed), every probe peak reading with its depth, the sample zones line, and the file number stamp near the bottom.
In legibility, say whether the smallest printed characters were readable and note anything you could not read."""


def render_page(pdf: Path, page: int, out_dir: Path, dpi: int = 200) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / f"p{page:04d}"
    subprocess.run(["pdftoppm", "-gray", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page),
                    "-singlefile", str(pdf), str(prefix)], check=True)
    return prefix.with_suffix(".png")


def run_probe(pdf: Path, page: int, model: str, effort: str) -> dict:
    img = render_page(pdf, page, PATHS.data / "probe" / "render")
    req = ExtractionRequest(
        task="probe",
        images=(img,),
        system_prompt=SYSTEM,
        user_prompt=USER,
        schema=PROBE_SCHEMA,
        schema_version=short(sha256_json(PROBE_SCHEMA)),
        prompt_version=short(sha256_json([SYSTEM, USER])),
        model=model,
        effort=effort,
        render_params={"dpi": 200, "mode": "gray", "tool": "pdftoppm"},
    )
    resp = ClaudeCliBackend().call(req)
    record = {
        "cache_key": resp.cache_key,
        "request": {"task": req.task, "model": req.model, "effort": req.effort,
                    "prompt_version": req.prompt_version, "schema_version": req.schema_version,
                    "image_sha256": sha256_file(img), "pdf": pdf.name, "page": page,
                    "render_params": req.render_params},
        "response": {"structured": resp.structured, "model_resolved": resp.model_resolved,
                     "num_turns": resp.num_turns, "duration_s": resp.duration_s, "usage": resp.usage,
                     "cost_usd": resp.cost_usd, "backend_version": resp.backend_version},
        "envelope": resp.envelope,
    }
    out = PATHS.pipeline / "tests" / "fixtures" / "replay" / "probe_drilllog_p1.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n")
    record["fixture_path"] = str(out)
    return record

"""V01 to V20: the checks that turn a plausible-looking table into a flagged one.

Three rules hold for every validator:
- **Nothing is dropped.** A finding sets a value's `status` to `flag` and appends a `ValidatorOutcome`
  to its lineage. The value, its quote and its box stay in the export, so a reviewer sees the evidence.
- **Findings are evidence, not opinions.** Each carries `details` with the numbers it compared.
- **Shadow mode is per validator.** Config A runs the row-count check (V09) in shadow: the finding is
  recorded in the run's validator report but does not flag the value. Config B enforces it. That
  difference is the "before and after" the failure card measures.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..paths import PATHS

VALIDATOR_VERSION = "validators/v1"

# Validators put in shadow by `validator_mode = "shadow"` (config A).
SHADOW_MODE_IDS = frozenset({"V09"})


@dataclass(frozen=True)
class Finding:
    validator: str
    version: str
    outcome: str                 # pass | flag | fail | na
    severity: str                # info | warn | error
    class_a: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    value_ids: tuple[str, ...] = ()
    scope: str = "value"         # value | interval | hole | table | file
    scope_id: str = ""

    def outcome_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"id": self.validator, "outcome": self.outcome}
        if self.severity:
            rec["severity"] = self.severity
        if self.class_a:
            rec["class_a"] = True
        if self.message:
            rec["message"] = self.message
        return rec

    def as_dict(self) -> dict[str, Any]:
        return {"validator": self.validator, "version": self.version, "outcome": self.outcome,
                "severity": self.severity, "class_a": self.class_a, "message": self.message,
                "details": self.details, "value_ids": list(self.value_ids), "scope": self.scope,
                "scope_id": self.scope_id}


@dataclass(frozen=True)
class Validator:
    id: str
    version: str
    title: str
    severity: str
    class_a: bool
    fn: Callable[[dict[str, Any], dict[str, Any]], Iterable[Finding]]

    def run(self, doc: dict[str, Any], ctx: dict[str, Any]) -> list[Finding]:
        return list(self.fn(doc, ctx))


REGISTRY: dict[str, Validator] = {}


def register(vid: str, title: str, severity: str = "warn", class_a: bool = False, version: str = "1") -> Callable:
    def deco(fn: Callable[[dict[str, Any], dict[str, Any]], Iterable[Finding]]) -> Callable:
        REGISTRY[vid] = Validator(vid, version, title, severity, class_a, fn)
        return fn
    return deco


def finding(vid: str, outcome: str, message: str, *, value_ids: Iterable[str] = (), details: Any = None,
            severity: str | None = None, class_a: bool | None = None, scope: str = "value",
            scope_id: str = "") -> Finding:
    v = REGISTRY.get(vid)
    return Finding(
        validator=vid, version=v.version if v else "1", outcome=outcome,
        severity=severity if severity is not None else (v.severity if v else "warn"),
        class_a=class_a if class_a is not None else (v.class_a if v else False),
        message=message, details=details or {}, value_ids=tuple(value_ids), scope=scope, scope_id=scope_id,
    )


# ------------------------------------------------------------------ running and applying

def run_validators(doc: dict[str, Any], ctx: dict[str, Any] | None = None,
                   only: Iterable[str] | None = None) -> list[Finding]:
    from . import checks  # noqa: F401  (registers every validator)

    ctx = ctx or {}
    wanted = set(only) if only else set(REGISTRY)
    out: list[Finding] = []
    for vid in sorted(wanted):
        validator = REGISTRY.get(vid)
        if validator is None:
            continue
        out.extend(validator.run(doc, ctx))
    return out


def apply_findings(doc: dict[str, Any], findings: list[Finding], shadow: Iterable[str] = ()) -> dict[str, Any]:
    """Attach outcomes to values. Shadowed validators are recorded but never change a status."""
    shadow = set(shadow)
    values = doc.get("values", {})
    counts: dict[str, int] = {}
    flagged: set[str] = set()
    for f in findings:
        counts[f.validator] = counts.get(f.validator, 0) + (1 if f.outcome in ("flag", "fail") else 0)
        if f.outcome not in ("flag", "fail"):
            continue
        if f.validator in shadow:
            continue
        for vid in f.value_ids:
            v = values.get(vid)
            if v is None:
                continue
            lineage = v.setdefault("lineage", {})
            outcomes = lineage.setdefault("validators", [])
            if not any(o.get("id") == f.validator and o.get("message") == f.message for o in outcomes):
                outcomes.append(f.outcome_record())
            if v.get("status") != "miss":
                v["status"] = "flag"
            flagged.add(vid)
    doc["validators"] = {
        "version": VALIDATOR_VERSION,
        "validated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "shadow": sorted(shadow),
        "counts": {k: v for k, v in sorted(counts.items()) if v},
        "flagged_values": len(flagged),
        "findings": [f.as_dict() for f in findings],
    }
    # intervals inherit the worst status of their own values
    for hole in doc.get("holes", []):
        for group in ("lith", "assays"):
            for interval in hole.get(group, []):
                ids = [interval.get("from"), interval.get("to"), interval.get("sample_id")]
                ids += [g["value"] for g in interval.get("grades", [])]
                if any(i in flagged for i in ids if i):
                    interval["status"] = "flag"
        hole_ids = [v for v in hole.get("collar", {}).values() if v] + [hole.get("name_vid")]
        hole["status"] = "flag" if (any(i in flagged for i in hole_ids if i)
                                    or any(i.get("status") == "flag" for i in hole.get("lith", []) + hole.get("assays", []))) else "pass"
    return doc


# ------------------------------------------------------------------ provincial context

def build_context(file_num: str) -> dict[str, Any]:
    """Provincial facts a validator may compare against. Never sent to the model."""
    from ..filenum import norm_file_num
    from ..index import read_features
    from ..nts import parse_nts_list

    ctx: dict[str, Any] = {"file_num": file_num, "geods_holes": [], "compilation_holes": [],
                           "nts_sheets": [], "work": {}}
    sel_path = PATHS.index / "selection.json"
    if sel_path.is_file():
        sel = json.loads(sel_path.read_text())
        for cand in sel.get("shortlist", {}).get("candidates", []):
            if cand.get("file_num") == file_num:
                feats = cand.get("features", {})
                ctx["nts_sheets"] = feats.get("nts_sheets") or parse_nts_list(feats.get("nts_from_file_num"))
                ctx["work"] = {
                    "hole_count": feats.get("work_hole_count"),
                    "hole_names": feats.get("work_hole_names") or [],
                    "metres": feats.get("work_metres"), "feet": feats.get("work_feet"),
                    "company": feats.get("company"), "year": feats.get("year"), "era": feats.get("era"),
                    "property": feats.get("property"), "description": feats.get("description"),
                    "geods_hole_names": feats.get("geods_hole_names") or [],
                    "compilation_hole_names": feats.get("compilation_hole_names") or [],
                }
                break
    try:
        for f in read_features("geods_holes"):
            p = f.get("properties", {})
            if norm_file_num(p.get("TEMP_ASSMNT_FILE_NUM") or "") == file_num:
                # the GeoDS service spells its object id "ObjectID" on this layer, the compilation "OBJECTID"
                ctx["geods_holes"].append({"id": int(p.get("OBJECTID") or p.get("ObjectID") or 0),
                                           "name": p.get("HOLE_NAME") or "",
                                           "total_depth_m": p.get("TOTL_MSRD_DPTH_M"),
                                           "inclination_deg": p.get("INCLNTN_DEG"), "azimuth_deg": p.get("AZM_DEG"),
                                           "datum": p.get("UTM_DATUM_TYPE"), "zone": p.get("UTM_PROJCTN_ZONE"),
                                           "lonlat": f.get("geometry", {}).get("coordinates"),
                                           "orig_lat": p.get("ORIGNL_LAT_DEG"), "orig_lon": p.get("ORIGNL_LON_DEG")})
    except FileNotFoundError:
        pass
    try:
        for f in read_features("compilation"):
            p = f.get("properties", {})
            src = str(p.get("SOURCE") or "")
            if file_num in src or norm_file_num(src) == file_num:
                ctx["compilation_holes"].append({"id": int(p.get("OBJECTID") or 0),
                                                 "name": p.get("DRILLHOLE_NAME") or "",
                                                 "total_depth_m": p.get("TOTAL_DH_LENGTH_M"),
                                                 "inclination_deg": p.get("DH_INCLINATION"),
                                                 "azimuth_deg": p.get("DH_AZIMUTH"),
                                                 "company": p.get("COMPANY"), "source": src,
                                                 "lonlat": f.get("geometry", {}).get("coordinates")})
    except FileNotFoundError:
        pass
    return ctx


# ------------------------------------------------------------------ stage

def validators_path(file_num: str) -> Path:
    return PATHS.out / "validate" / f"{file_num}.json"


def stage_validate(files: list[str] | None = None, mode: str = "enforce",
                   log: Callable[[str], None] = print) -> dict[str, Any]:
    from ..assemble import assembled_files, assembled_path, read_assembled

    shadow = SHADOW_MODE_IDS if mode == "shadow" else frozenset()
    targets = [f for f in assembled_files() if not files or f in files]
    if not targets:
        raise RuntimeError("nothing assembled; run `lr assemble` first")
    summary: dict[str, Any] = {"mode": mode, "shadow": sorted(shadow), "files": {}}
    for file_num in targets:
        from .checks import geometric_row_counts

        doc = read_assembled(file_num)
        ctx = build_context(file_num)
        ctx["positions"] = _load_positions(file_num)
        ctx["crosschecks"] = _load_crosschecks(file_num)
        ctx["geometric_rows"] = geometric_row_counts(doc)
        ctx["text_layer"] = _text_layer_by_page(doc)
        findings = run_validators(doc, ctx)
        apply_findings(doc, findings, shadow=shadow)
        assembled_path(file_num).write_text(json.dumps(doc, separators=(",", ":"), default=str))
        out = validators_path(file_num)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"file_num": file_num, "mode": mode, "shadow": sorted(shadow),
                                   "findings": [f.as_dict() for f in findings]}, indent=1, default=str))
        counts = doc["validators"]["counts"]
        summary["files"][file_num] = {"findings": len(findings), "flagged_values": doc["validators"]["flagged_values"],
                                      "counts": counts}
        log(f"  {file_num}: {len(findings)} findings, {doc['validators']['flagged_values']} values flagged "
            f"({', '.join(f'{k}:{v}' for k, v in counts.items()) or 'none'})")
    return summary


def _text_layer_by_page(doc: dict[str, Any]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """The PDF's own text layer for V13, keyed by (pdf sha256, page).

    One assessment file can hold sixteen PDFs, each with its own page 1, so the key has to carry the
    document. Untrusted pages are skipped: comparing two bad readings produces noise, not evidence.
    """
    from ..ocr import read_words
    from ..render import read_pages

    trusted = {(r["pdf_sha256"], int(r["page_no"])): bool(r.get("text_layer_trusted")) for r in read_pages()}
    out: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for sha in sorted({p["pdf_sha256"] for p in doc.get("pages", [])}):
        for w in read_words(sha):
            if w["engine"] != "pdftotext":
                continue
            page = int(w["page_no"])
            if not trusted.get((sha, page)):
                continue
            out.setdefault((sha, page), []).append(w)
    return out


def _load_positions(file_num: str) -> dict[str, Any]:
    p = PATHS.out / "crs" / f"{file_num}.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def _load_crosschecks(file_num: str) -> dict[str, Any]:
    p = PATHS.out / "crosscheck" / f"{file_num}.json"
    return json.loads(p.read_text()) if p.is_file() else {}


# Importing the checks at the end of this module registers V01 to V20 in REGISTRY. It sits here, after
# `register` and `finding` are defined, so `from . import checks` cannot see a half-built module.
from . import checks as _checks  # noqa: E402,F401

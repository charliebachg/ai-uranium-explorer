"""`lr assay-sheets`: the assay spreadsheets filed with modern digital submissions, into the native tier.

Post-2005 assessment files rarely print an assay table; they attach spreadsheets. Those are as-published
numbers and need no model, only a parser that finds the header row and names its columns. A row is kept when
the sheet has a hole or a sample column and a depth pair or a uranium column; everything else is recorded as
not read, with the reason, so "no assay sheet was read for this file" is a statement and not a silence.

What is kept is deliberately narrow: hole, sample, from, to, interval, one U3O8 column and one U column, each
with the printed text beside the parsed number and the header it came from. The other sixty columns a lab
sheet carries are not lost (the file is on disk, hashed) but they are not values in the store until a
criterion needs them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .paths import PATHS
from .store import append_frame, connect

TOOL = "assay_sheets"
HEADER_SCAN_ROWS = 40

_HOLE = re.compile(r"^(hole|ddh|hole[ _]?(id|no|number|name)|drill[ _]?hole|bhid|dh)$")
_SAMPLE = re.compile(r"^(sample|sample[ _]?(id|no|number|#)|lab[ _]?tag[ _]?id|tag)$")
_FROM = re.compile(r"^(from|from[ _]?\(?m\)?|depth[ _]?\(?from\)?|start)$")
_TO = re.compile(r"^(to|to[ _]?\(?m\)?|depth[ _]?\(?to\)?|end)$")
_INTERVAL = re.compile(r"^(interval|interval[ _]?width|width|length|int)$")
_U3O8 = re.compile(r"u3o8|u308")
_U_PPM = re.compile(r"^u([ _(]|$)|^u[ _]?ppm|^u_icp|uranium")
_PCT = re.compile(r"%|perc|pct")
_PPM = re.compile(r"ppm")
_NUM = re.compile(r"^[<>]?\s*-?\d+(?:[.,]\d+)?$")


def _norm(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell).strip().lower()) if cell is not None and str(cell) != "nan" else ""


def _nonempty(cells: list[Any]) -> list[Any]:
    return [c for c in cells if c is not None and str(c) != "nan" and str(c).strip()]


def _key(cell: Any) -> str:
    """A header cell reduced to letters, digits and single separators, units kept for the unit check."""
    return re.sub(r"[^a-z0-9%()#_ ]", "", _norm(cell)).strip()


def map_columns(header: list[Any]) -> dict[str, int]:
    """Which column plays which role. First match wins; a U3O8 column is preferred to a bare U column."""
    roles: dict[str, int] = {}
    keys = [_key(c) for c in header]
    for i, k in enumerate(keys):
        base = k.replace("(m)", "").strip()
        if "hole" not in roles and _HOLE.match(base):
            roles["hole"] = i
        elif "sample" not in roles and _SAMPLE.match(base):
            roles["sample"] = i
        elif "from" not in roles and _FROM.match(base):
            roles["from"] = i
        elif "to" not in roles and _TO.match(base):
            roles["to"] = i
        elif "interval" not in roles and _INTERVAL.match(base):
            roles["interval"] = i
        elif "sample_type" not in roles and base in ("sample type", "sample_type", "type"):
            roles["sample_type"] = i
    for prefer_pct in (True, False):
        for i, k in enumerate(keys):
            if "u3o8" not in roles and _U3O8.search(k) and "dot" not in k and (not prefer_pct or _PCT.search(k)):
                roles["u3o8"] = i
    for i, k in enumerate(keys):
        if "u_ppm" not in roles and _U_PPM.search(k) and not _U3O8.search(k) and (_PPM.search(k) or "icp" in k):
            roles["u_ppm"] = i
    return roles


_HOLE_ID = re.compile(r"^[A-Z]{1,6}[- _]?\d{1,4}[- _]\d{2,4}[A-Z]?$|^[A-Z]{2,6}[- _]?\d{2,4}[A-Z]?$")


def guess_hole_column(df: pd.DataFrame, header_row: int, roles: dict[str, int], look: int = 25) -> int | None:
    """A column whose cells look like hole names (PLS12-001, HU-268, ND0703): companies name it anything."""
    best, best_share = None, 0.0
    for j in range(df.shape[1]):
        if j in roles.values():
            continue
        cells = [str(c).strip().upper() for c in df.iloc[header_row + 1:header_row + 1 + look, j].tolist()
                 if c is not None and str(c) != "nan" and str(c).strip()]
        if len(cells) < 3:
            continue
        share = sum(1 for c in cells if _HOLE_ID.match(c)) / len(cells)
        if share > best_share:
            best, best_share = j, share
    return best if best_share >= 0.8 else None


def find_header(df: pd.DataFrame, scan: int = HEADER_SCAN_ROWS) -> tuple[int, dict[str, int]] | None:
    """The first row that names a hole or sample column and either a depth pair or a uranium column.
    A sheet whose hole column has a house name (Fission_ID) gets it by the look of its values."""
    for i in range(min(scan, len(df))):
        roles = map_columns(df.iloc[i].tolist())
        if ("hole" in roles or "sample" in roles) and (("from" in roles and "to" in roles) or "u3o8" in roles or "u_ppm" in roles):
            if "hole" not in roles:
                j = guess_hole_column(df, i, roles)
                if j is not None:
                    roles["hole"] = j
            return i, roles
    return None


_QC = re.compile(r"standard|blank|duplicate|\brep\b|reference")


def read_certificate(df: pd.DataFrame, scan: int = HEADER_SCAN_ROWS) -> tuple[dict[str, Any], list[tuple[int, str, str | None, Any]]] | None:
    """The SRC Geoanalytical certificate form: a key-value block (Analyte, Unit, Detection) and then a table
    whose first column is the sample description and whose value column carries no header at all.

    Returns (block, rows) with rows as (row_no, sample, sample_type, raw_value), or None if it is not one."""
    block: dict[str, Any] = {}
    header_row = None
    for i in range(min(scan, len(df))):
        cells = _nonempty(df.iloc[i].tolist())  # the SRC sheets often leave column A blank
        first = _norm(cells[0]) if cells else ""
        if first in ("analyte", "unit", "detection", "suite/digestion", "group", "date", "samples"):
            block[first] = _norm(cells[1]) if len(cells) > 1 else ""
        if first in ("description", "sample", "sample id", "sample no", "sample number") and "analyte" in block:
            header_row = i
            break
    if header_row is None or "analyte" not in block:
        return None
    rows = []
    for r in range(header_row + 1, len(df)):
        cells = _nonempty(df.iloc[r].tolist())
        if not cells:
            continue
        sample = _norm(cells[0])
        kind = _norm(cells[1]) if len(cells) > 1 and not _NUM.match(str(cells[1]).strip()) else ""
        values = [c for c in cells[1:] if _NUM.match(str(c).strip())]
        rows.append((r, sample, kind or None, values[-1] if values else None))
    block["header_row"] = header_row
    return block, rows


def parse_number(cell: Any) -> tuple[float | None, str, bool]:
    """(value, as printed, below detection). '<0.005' keeps 0.005 as the limit and flags it."""
    printed = "" if cell is None or str(cell) == "nan" else str(cell).strip()
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return (None if pd.isna(cell) else float(cell)), printed, False
    if not printed or not _NUM.match(printed):
        return None, printed, False
    below = printed.startswith("<")
    return float(printed.lstrip("<>").strip().replace(",", ".")), printed, below


def read_sheet(file_num: str, doc_sha256: str, doc_name: str, sheet: str, df: pd.DataFrame,
               now: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One sheet into a sheet record and its rows. Pure: no I/O."""
    meta = {"file_num": file_num, "doc_sha256": doc_sha256, "doc_name": doc_name, "sheet": sheet,
            "status": "empty", "header_row": None, "n_rows": 0, "columns_json": None, "note": None, "format": None,
            "loaded_at": now}
    if df.empty:
        return meta, []
    found = find_header(df)
    if not found:
        cert = read_certificate(df)
        if cert is None:
            meta["status"] = "no_header"
            meta["note"] = "no row names a hole or sample column beside a depth pair or a uranium column, and it is not a certificate"
            return meta, []
        return _certificate_rows(meta, cert, file_num, doc_sha256, doc_name, sheet, now)
    h, roles = found
    header = df.iloc[h].tolist()
    meta.update(status="ingested", header_row=int(h), columns_json=json.dumps([_norm(c) for c in header]))
    rows: list[dict[str, Any]] = []
    for r in range(h + 1, len(df)):
        rec = df.iloc[r].tolist()
        hole = _norm(rec[roles["hole"]]) if "hole" in roles else ""
        sample = _norm(rec[roles["sample"]]) if "sample" in roles else ""
        if not hole and not sample:
            continue
        frm, _, _ = parse_number(rec[roles["from"]]) if "from" in roles else (None, "", False)
        to, _, _ = parse_number(rec[roles["to"]]) if "to" in roles else (None, "", False)
        iv, _, _ = parse_number(rec[roles["interval"]]) if "interval" in roles else (None, "", False)
        u3, u3p, u3b = parse_number(rec[roles["u3o8"]]) if "u3o8" in roles else (None, "", False)
        up, upp, upb = parse_number(rec[roles["u_ppm"]]) if "u_ppm" in roles else (None, "", False)
        if frm is None and to is None and u3 is None and up is None:
            continue
        if u3 is not None and "u3o8" in roles and _PPM.search(_key(header[roles["u3o8"]])) and not _PCT.search(_key(header[roles["u3o8"]])):
            u3 = u3 / 10_000.0  # a U3O8 column printed in ppm is kept in percent, the store's unit
        row_id = hashlib.sha256(f"{doc_sha256}|{sheet}|{r}".encode()).hexdigest()[:16]
        rows.append({
            "row_id": row_id, "file_num": file_num, "doc_sha256": doc_sha256, "doc_name": doc_name, "sheet": sheet,
            "row_no": int(r), "hole_id": hole.upper() or None, "sample_id": sample or None,
            "from_m": frm, "to_m": to, "interval_m": iv if iv is not None else (round(to - frm, 3) if frm is not None and to is not None else None),
            "u3o8_pct": u3, "u3o8_as_printed": u3p or None, "u3o8_column": _norm(header[roles["u3o8"]]) if "u3o8" in roles else None,
            "u_ppm": up, "u_ppm_as_printed": upp or None, "u_ppm_column": _norm(header[roles["u_ppm"]]) if "u_ppm" in roles else None,
            "below_detection": bool(u3b or upb), "sample_type": _norm(rec[roles["sample_type"]]) or None if "sample_type" in roles else None,
            "kind": "interval", "loaded_at": now,
        })
    meta["n_rows"] = len(rows)
    meta["format"] = "columns"
    if not rows:
        meta["status"] = "empty"
        meta["note"] = "header found but no row carried a depth or a uranium value"
    return meta, rows


def _certificate_rows(meta: dict[str, Any], cert: tuple[dict[str, Any], list], file_num: str, doc_sha256: str,
                      doc_name: str, sheet: str, now: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    block, raw = cert
    analyte, unit = block.get("analyte", ""), block.get("unit", "")
    is_u3o8 = bool(_U3O8.search(analyte))
    is_u = analyte.strip() in ("u", "uranium") or analyte.startswith("u ")
    column = f"{analyte} ({unit})".strip()
    rows = []
    for r, sample, kind, value in raw:
        v, printed, below = parse_number(value)
        if v is None:
            continue
        u3 = v if is_u3o8 else None
        if u3 is not None and _PPM.search(unit) and not _PCT.search(unit):
            u3 = u3 / 10_000.0
        up = v if (is_u and _PPM.search(unit)) else None
        if u3 is None and up is None:
            continue
        rows.append({
            "row_id": hashlib.sha256(f"{doc_sha256}|{sheet}|{r}".encode()).hexdigest()[:16], "file_num": file_num,
            "doc_sha256": doc_sha256, "doc_name": doc_name, "sheet": sheet, "row_no": int(r), "hole_id": None,
            "sample_id": sample, "from_m": None, "to_m": None, "interval_m": None,
            "u3o8_pct": u3, "u3o8_as_printed": printed if u3 is not None else None, "u3o8_column": column if u3 is not None else None,
            "u_ppm": up, "u_ppm_as_printed": printed if up is not None else None, "u_ppm_column": column if up is not None else None,
            "below_detection": below, "sample_type": kind, "kind": "certificate", "loaded_at": now,
        })
    meta.update(status="ingested" if rows else "empty", header_row=int(block["header_row"]), n_rows=len(rows),
                columns_json=json.dumps({k: v for k, v in block.items() if k != "header_row"}), format="certificate",
                note=None if rows else f"certificate for analyte {analyte!r} in {unit!r}: no uranium value rows")
    return meta, rows


def read_workbook(path: Path, file_num: str, doc_sha256: str, doc_name: str, now: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        book = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
    except Exception as e:  # noqa: BLE001 - the reason is recorded, the file is not a value
        return [{"file_num": file_num, "doc_sha256": doc_sha256, "doc_name": doc_name, "sheet": "*",
                 "status": "unreadable", "header_row": None, "n_rows": 0, "columns_json": None, "format": None,
                 "note": f"{type(e).__name__}: {str(e)[:160]}", "loaded_at": now}], []
    sheets, rows = [], []
    for name, df in book.items():
        meta, recs = read_sheet(file_num, doc_sha256, doc_name, str(name), df, now)
        sheets.append(meta)
        rows.extend(recs)
    return sheets, rows


def stage_assay_sheets(files: list[str] | None = None, log: Callable[[str], None] = print,
                       raw_dir: Path | None = None, write: bool = True) -> dict[str, Any]:
    """Every assay_xls object in the fetch manifest (or those of the given files) into native.assay_sheet*."""
    raw = raw_dir or PATHS.raw
    manifest = json.loads((raw / "manifest.json").read_text()) if (raw / "manifest.json").is_file() else {}
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    wanted = {f.upper() for f in (files or [])}
    sheets, rows = [], []
    for rel, rec in sorted(manifest.items()):
        if rec.get("kind") != "assay_xls" or (wanted and rec["file_num"] not in wanted):
            continue
        s, r = read_workbook(raw / rel, rec["file_num"], rec["sha256"], rec["name"], now)
        sheets.extend(s)
        rows.extend(r)
    by_status: dict[str, int] = {}
    for s in sheets:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    by_file: dict[str, int] = {}
    for r in rows:
        by_file[r["file_num"]] = by_file.get(r["file_num"], 0) + 1
    n_cert = sum(1 for r in rows if r["kind"] == "certificate")
    log(f"  assay sheets: {len(sheets)} sheets {by_status}; {len(rows)} rows ({len(rows) - n_cert} intervals, {n_cert} certificate); "
        f"{sum(1 for r in rows if r['u3o8_pct'] is not None)} with U3O8, "
        f"{sum(1 for r in rows if r['u_ppm'] is not None)} with U ppm, {sum(r['below_detection'] for r in rows)} below detection")
    for fn, n in sorted(by_file.items()):
        log(f"    {fn:<12} {n:>6} rows, holes {len({r['hole_id'] for r in rows if r['file_num'] == fn and r['hole_id']})}")
    if write:
        con = connect()
        try:
            append_frame(con, "native", "assay_sheet", pd.DataFrame(sheets), "native", replace=True)
            append_frame(con, "native", "assay_sheet_row", pd.DataFrame(rows), "native", replace=True)
        finally:
            con.close()
    return {"sheets": len(sheets), "by_status": by_status, "rows": len(rows), "by_file": by_file}

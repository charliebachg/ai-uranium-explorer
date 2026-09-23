"""Builders for Val records (the web contract's value registry). Every number the UI shows is one of these."""

from __future__ import annotations

from typing import Any

import pyproj

from . import __version__

TOOL = f"uranium_explorer {__version__}"
PROJ_TOOL = f"pyproj {pyproj.__version__} / PROJ {pyproj.proj_version_str}"


def stat(vid: str, value: int | float | str, fmt: str = "int", note: str | None = None, unit: str | None = None) -> dict[str, Any]:
    v: dict[str, Any] = {"id": vid, "kind": "stat", "as_printed": None, "value": value, "unit_as_printed": None,
                         "fmt": fmt}
    if unit:
        v["unit"] = unit
    if note:
        v["note"] = note
    return v


def derived(vid: str, value: int | float | None, fmt: str, op: str, inputs: list[str] | None = None,
            tool: str = TOOL, params: dict[str, Any] | None = None, unit: str | None = None,
            note: str | None = None) -> dict[str, Any]:
    v: dict[str, Any] = {
        "id": vid, "kind": "derived", "as_printed": None, "value": value, "unit_as_printed": None, "fmt": fmt,
        "derivation": {"op": op, "inputs": inputs or [], "tool": tool, **({"params": params} if params else {})},
    }
    if unit:
        v["unit"] = unit
    if note:
        v["note"] = note
    return v


def source(vid: str, value: int | float | str | None, fmt: str, dataset: str, record_id: int | str, field: str,
           retrieved_at: str, unit: str | None = None) -> dict[str, Any]:
    v: dict[str, Any] = {
        "id": vid, "kind": "source", "as_printed": None, "value": value, "unit_as_printed": None, "fmt": fmt,
        "source": {"dataset": dataset, "record_id": record_id, "field": field, "retrieved_at": retrieved_at},
    }
    if unit:
        v["unit"] = unit
    return v


def registry(*vals: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for v in vals:
        if v["id"] in out:
            raise ValueError(f"duplicate value id {v['id']}")
        out[v["id"]] = v
    return out

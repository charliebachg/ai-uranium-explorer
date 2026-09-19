"""`lr run-usage`: what one extraction run cost, page by page, from its own records.

Everything here is read from `calls.jsonl` and `events.jsonl` in the run directory. Nothing is estimated:
tokens and durations are what the backend reported per call, dollars are the backend's own accounting of
that call, and wall time is the span between the plan event and the last recorded call. A model check is
included because a run meant to measure one model is worthless if a call quietly resolved to another.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import PATHS

TOKEN_KINDS = ("input", "cache_creation", "cache_read", "output")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def summarise_run(run_dir: Path, expected_model: str | None = None) -> dict[str, Any]:
    """Per-call rows plus totals for one run directory. Pure over the files; safe on a partial run."""
    calls = _read_jsonl(run_dir / "calls.jsonl")
    events = _read_jsonl(run_dir / "events.jsonl")
    manifest = json.loads((run_dir / "manifest.json").read_text()) if (run_dir / "manifest.json").is_file() else {}
    expected = expected_model or (manifest.get("config") or {}).get("model")

    rows = []
    for c in calls:
        tokens = c.get("tokens") or {}
        rows.append({
            "file_num": c.get("file_num"), "page_no": c.get("page_no"), "attempt": c.get("attempt"),
            "status": c.get("status"), "model": c.get("model_resolved"),
            "duration_s": float(c.get("duration_s") or 0.0), "cost_usd": float(c.get("cost_usd") or 0.0),
            **{k: int(tokens.get(k) or 0) for k in TOKEN_KINDS},
            "turns": c.get("num_turns"), "tables": c.get("n_tables"), "rows": c.get("n_rows"),
            "page_kind": c.get("page_kind"),
        })

    ok = [r for r in rows if r["status"] == "ok"]
    totals = {
        "calls": len(rows), "ok": len(ok), "failed": len(rows) - len(ok),
        "model_time_s": round(sum(r["duration_s"] for r in rows), 1),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        **{k: sum(r[k] for r in rows) for k in TOKEN_KINDS},
    }
    if ok:
        totals["per_ok_page"] = {
            "duration_s": round(totals["model_time_s"] / len(ok), 1),
            "cost_usd": round(sum(r["cost_usd"] for r in ok) / len(ok), 4),
            "output_tokens": round(sum(r["output"] for r in ok) / len(ok)),
        }

    stamps = [_parse(e["at"]) for e in events if e.get("at")] + [_parse(c["at"]) for c in calls if c.get("at")]
    wall = (max(stamps) - min(stamps)).total_seconds() if len(stamps) > 1 else 0.0
    plan = next((e for e in events if e.get("event") == "plan"), {})

    models = sorted({r["model"] for r in rows if r["model"]})
    off_model = [r for r in rows if expected and r["model"] and r["model"] != expected]
    return {
        "run_id": run_dir.name, "config": manifest.get("config", {}).get("id") or manifest.get("config"),
        "expected_model": expected, "models_seen": models, "off_model_calls": len(off_model),
        "planned_pages": plan.get("n_pages"), "files": plan.get("files"),
        "wall_s": round(wall, 1), "totals": totals, "rows": rows,
    }


def latest_run_dir(runs_dir: Path | None = None) -> Path:
    root = runs_dir or PATHS.runs
    dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not dirs:
        raise FileNotFoundError(f"no runs under {root}")
    return dirs[-1]


def format_report(summary: dict[str, Any]) -> str:
    t = summary["totals"]
    lines = [
        f"run {summary['run_id']}  config {summary['config']}  files {summary['files']}",
        f"model expected {summary['expected_model']!r}, seen {summary['models_seen']}, "
        f"off-model calls {summary['off_model_calls']}",
        f"planned pages {summary['planned_pages']}, calls {t['calls']} (ok {t['ok']}, failed {t['failed']})",
        f"model time {t['model_time_s']} s, wall {summary['wall_s']} s, cost ${t['cost_usd']:.4f}",
        f"tokens  input {t['input']}  cache_creation {t['cache_creation']}  cache_read {t['cache_read']}  "
        f"output {t['output']}",
    ]
    if "per_ok_page" in t:
        p = t["per_ok_page"]
        lines.append(f"per ok page  {p['duration_s']} s, ${p['cost_usd']:.4f}, {p['output_tokens']} output tokens")
    lines.append("")
    lines.append(f"{'file':<12}{'page':>5}{'att':>4} {'status':<7}{'dur_s':>7}{'usd':>8}{'in':>6}{'c_cre':>7}"
                 f"{'c_read':>8}{'out':>7}{'turns':>6}{'tbl':>4}{'rows':>5}  model")
    for r in summary["rows"]:
        lines.append(f"{r['file_num'] or '':<12}{r['page_no'] or '':>5}{r['attempt'] or '':>4} {r['status'] or '':<7}"
                     f"{r['duration_s']:>7.1f}{r['cost_usd']:>8.4f}{r['input']:>6}{r['cache_creation']:>7}"
                     f"{r['cache_read']:>8}{r['output']:>7}{r['turns'] if r['turns'] is not None else '':>6}"
                     f"{r['tables'] if r['tables'] is not None else '':>4}{r['rows'] if r['rows'] is not None else '':>5}"
                     f"  {r['model'] or ''}")
    return "\n".join(lines)

"""Environment checks. Read-only."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass

import pyproj

from . import crs
from .paths import PATHS

EXPECTED_CLAUDE = "2.1.274"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def run_checks() -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("python", sys.version_info[:2] == (3, 13), platform.python_version()))

    # the CLI is a developer's option (`--backend claude`); the default backend is API first, so its absence
    # is noted, never a failure
    claude = shutil.which("claude")
    if claude:
        code, out = _run([claude, "--version"])
        version = out.split()[0] if out else "?"
        detail = f"{version} at {claude}"
        if version != EXPECTED_CLAUDE:
            detail += f" (last verified with {EXPECTED_CLAUDE})"
        checks.append(Check("claude CLI (optional)", code == 0, detail, required=False))
    else:
        checks.append(Check("claude CLI (optional)", False, "not on PATH; only --backend claude needs it", required=False))

    # the key the default backend needs, checked by name only: the value is never read here
    from .backends.openai_api import load_dotenv

    load_dotenv()
    has_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    checks.append(Check("OPENROUTER_API_KEY", has_key, "set" if has_key else "not set: the chat and the analyst jobs need it (.env, see .env.example)",
                        required=False))

    for tool in ("pdftoppm", "pdftotext", "pdfimages", "pdfinfo"):
        path = shutil.which(tool)
        checks.append(Check(tool, path is not None, path or "not found (brew install poppler)"))

    try:
        import ocrmac  # noqa: F401

        checks.append(Check("ocrmac (Apple Vision)", True, "importable"))
    except Exception as e:  # pragma: no cover - platform dependent
        checks.append(Check("ocrmac (Apple Vision)", False, str(e)))

    grid = PATHS.grids / crs.GRID_NAME
    if grid.is_file():
        try:
            tr = crs.Nad27ToNad83()
            s = tr.shift(crs.CANARY["lon"], crs.CANARY["lat"])
            checks.append(Check("NTv2 grid", True, f"pinned, canary {s.dist_m:.2f} m, PROJ {pyproj.proj_version_str}"))
        except crs.CrsError as e:
            checks.append(Check("NTv2 grid", False, str(e)))
    else:
        checks.append(Check("NTv2 grid", False, "missing; run `ue crs fetch-grid`"))

    key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
    checks.append(Check(
        "ANTHROPIC_API_KEY",
        True,
        "set in this shell; the claude_cli backend strips it from the child so calls use the subscription"
        if key_set else "not set (expected: the claude_cli backend uses the Claude Code login)",
        required=False,
    ))
    free = shutil.disk_usage(PATHS.root).free / 1e9
    checks.append(Check("disk", free > 10, f"{free:.0f} GB free", required=False))
    return checks

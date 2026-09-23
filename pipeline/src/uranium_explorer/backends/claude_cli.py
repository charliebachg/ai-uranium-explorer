"""Claude Code headless adapter: one `claude -p` subprocess per call, using the local subscription login.

Guarantees:
- The page image is staged in a fresh temp dir outside the repository with a neutral filename, so no
  CLAUDE.md, gold labels or file metadata can leak into the call. The dir is deleted afterwards.
- API-key environment variables are removed from the child, so billing cannot silently move to an API key.
- `--restricted` confines file tools to the stage dir; only the Read tool is available.
- A response is accepted only if the model actually used a tool turn (the image was read) and no
  permission was denied.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .base import (
    BackendConfigError,
    ExtractionRequest,
    ExtractionResponse,
    SchemaInvalidError,
    TransientBackendError,
    UsageLimitReached,
)

FAMILY = "claude_cli"
_LIMIT_RE = re.compile(r"(?i)(hit your (session|usage|weekly) limit|usage limit reached|limit\s*·\s*resets)")
_RESETS_RE = re.compile(r"(?i)resets\s+([^\n\"]+)")
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


def claude_version(binary: str = "claude") -> str:
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    return out.split()[0] if out else "unknown"


def staged_names(n: int) -> list[str]:
    return ["page.png"] if n == 1 else [f"page_{i + 1}.png" for i in range(n)]


def stage_inputs(req: ExtractionRequest, stage: Path) -> list[str]:
    """Copy a request's inputs into the stage directory under neutral names, and say what landed there.

    Page images keep their positional names so the page prompts are unchanged. Everything else keeps the name
    the caller chose, because an agent reading `cell.json` and `handbook.md` needs to know what it is opening.
    """
    names = staged_names(len(req.images))
    for src, name in zip(req.images, names, strict=True):
        shutil.copy2(src, stage / name)
    for src, name in req.stage_files:
        dest = stage / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        names.append(name)
    return names


def build_argv(req: ExtractionRequest, stage_dir: Path, binary: str = "claude",
               max_budget_usd: float = 0.60) -> list[str]:
    # The template carries {STAGE_DIR}; the concrete temp path never enters the cache key.
    prompt = req.user_prompt.replace("{STAGE_DIR}", str(stage_dir))
    return [
        binary, "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(req.schema, separators=(",", ":")),
        "--model", req.model,
        "--effort", req.effort,
        "--tools", "Read",
        "--allowedTools", "Read",
        "--restricted",
        "--add-dir", str(stage_dir),
        "--no-session-persistence",
        "--permission-prompts", "none",
        "--system-prompt", req.system_prompt,
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--max-budget-usd", f"{max_budget_usd:.2f}",
    ]


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def detect_usage_limit(*texts: str) -> UsageLimitReached | None:
    for t in texts:
        if t and _LIMIT_RE.search(t):
            m = _RESETS_RE.search(t)
            return UsageLimitReached(t.strip()[:300], m.group(1).strip() if m else None)
    return None


def parse_envelope(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise TransientBackendError("empty stdout from claude")
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        # stream noise before the JSON object: take the last line that parses
        for line in reversed(text.splitlines()):
            try:
                env = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        else:
            raise TransientBackendError(f"malformed envelope: {text[:200]!r}")
    if not isinstance(env, dict):
        raise TransientBackendError("envelope is not a JSON object")
    return env


def structured_from_envelope(env: dict[str, Any]) -> dict[str, Any]:
    so = env.get("structured_output")
    if isinstance(so, dict):
        return so
    result = env.get("result")
    if isinstance(result, str):
        body = result.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
        if fence:
            body = fence.group(1)
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise SchemaInvalidError("no structured output in envelope")


def resolve_model(env: dict[str, Any], requested: str) -> str | None:
    """The CLI also makes small side calls (a Haiku call was observed in the Phase 0 probe), so pick the
    requested model if present, otherwise the model that produced the most output tokens."""
    usage = env.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None
    if requested in usage:
        return requested
    return max(usage, key=lambda k: (usage[k] or {}).get("outputTokens", 0))


class ClaudeCliBackend:
    family = FAMILY

    def __init__(self, binary: str = "claude", timeout_s: int = 420, max_budget_usd: float = 0.60):
        self.binary = shutil.which(binary) or binary
        self.timeout_s = timeout_s
        self.max_budget_usd = max_budget_usd
        self.version = claude_version(self.binary)

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        stage = Path(tempfile.mkdtemp(prefix="ue_stage_"))
        try:
            stage_inputs(req, stage)
            argv = build_argv(req, stage, self.binary, self.max_budget_usd)
            t0 = time.monotonic()
            try:
                proc = subprocess.run(argv, cwd=stage, env=child_env(), stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=self.timeout_s)
            except subprocess.TimeoutExpired as e:
                raise TransientBackendError(f"claude timed out after {self.timeout_s} s") from e
            duration = time.monotonic() - t0

            limit = detect_usage_limit(proc.stdout, proc.stderr)
            if limit:
                raise limit
            env = parse_envelope(proc.stdout) if proc.stdout.strip() else {}
            if not env:
                raise TransientBackendError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
            limit = detect_usage_limit(str(env.get("result", "")))
            if limit:
                raise limit
            if env.get("is_error"):
                raise TransientBackendError(
                    f"claude reported an error ({env.get('subtype')}, {env.get('num_turns')} turns, "
                    f"${env.get('total_cost_usd')}): {str(env.get('result'))[:300]}")
            if env.get("permission_denials"):
                raise BackendConfigError(f"permission denied inside the call: {env['permission_denials']}")
            turns = env.get("num_turns")
            if isinstance(turns, int) and turns < 2:
                raise BackendConfigError("model answered without a tool turn: the page image was not read")

            structured = structured_from_envelope(env)
            resolved = resolve_model(env, req.model)
            return ExtractionResponse(
                structured=structured,
                envelope=env,
                backend=FAMILY,
                backend_version=self.version,
                model_requested=req.model,
                model_resolved=resolved,
                num_turns=turns if isinstance(turns, int) else None,
                duration_s=round(duration, 2),
                usage=env.get("usage") or {},
                cost_usd=env.get("total_cost_usd"),
                cache_key=req.cache_key(FAMILY),
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)

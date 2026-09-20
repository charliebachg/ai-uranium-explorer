"""OpenAI adapter for the dashboard's chat agent, behind the same Backend protocol as everything else.

This exists because the chat in the demo should answer in a couple of seconds from an ordinary API key, rather
than by starting a `claude -p` subprocess per turn. Nothing else moves: reading pages, the memo panel and the
evals stay on Claude Code. The two are kept apart by `family`, so a cache entry written by one backend can
never be served for the other.

Three differences from the CLI adapter, all forced by the fact that an API call has no filesystem:

* **Staged files are inlined.** The CLI hands the model a directory and the Read tool. Here the same bytes go
  into the message, with a visible marker if anything had to be cut, because a silently truncated handbook
  produces a confident answer about evidence the model never saw.
* **Spend is checked before the call, not after.** See `spend.py`: the ceiling is cumulative and on disk.
* **The response schema travels in the prompt.** The tool-loop schema has a free-form `args` object, which
  strict structured output does not allow, so the call asks for JSON and this module validates the shape it
  gets back. A reply that is not usable JSON is retried once and then raised, never guessed at.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .base import (
    BackendConfigError,
    ExtractionRequest,
    ExtractionResponse,
    SchemaInvalidError,
    TransientBackendError,
)
from .spend import Price, check, record

FAMILY = "openai"
ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODELS_ENDPOINT = "https://api.openai.com/v1/models"

#: Everything staged, together, must fit in this many characters. Roughly four characters to the token, so
#: this is about 40k tokens of context: generous for a handbook and four tool results, and a hard stop long
#: before a runaway bundle turns into a bill.
MAX_STAGED_CHARS = 160_000


def load_dotenv(start: Path | None = None) -> dict[str, str]:
    """Read the nearest .env into os.environ without overwriting anything already set.

    Deliberately tiny and dependency-free. Values are never logged, and the file itself is gitignored.
    """
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        path = folder / ".env"
        if not path.exists():
            continue
        found: dict[str, str] = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            found[key] = value
            os.environ.setdefault(key, value)
        return found
    return {}


def api_key() -> str:
    load_dotenv()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise BackendConfigError(
            "OPENAI_API_KEY is empty. Put the key in legacy-reader/.env (it is gitignored); "
            "see .env.example for the other settings."
        )
    return key


def model_name() -> str:
    load_dotenv()
    return os.environ.get("OPENAI_MODEL", "gpt-5-mini").strip()


def list_models(timeout_s: float = 30.0) -> list[str]:
    """What this key can actually reach. Free, and the right thing to run before spending anything."""
    r = httpx.get(MODELS_ENDPOINT, headers={"Authorization": f"Bearer {api_key()}"}, timeout=timeout_s)
    if r.status_code != 200:
        raise BackendConfigError(f"{MODELS_ENDPOINT} returned {r.status_code}: {r.text[:300]}")
    return sorted(str(m.get("id")) for m in (r.json().get("data") or []))


def _staged_text(req: ExtractionRequest) -> str:
    """The staged files as text, in the order they were staged, with any shortfall stated out loud."""
    if not req.stage_files:
        return ""
    budget = MAX_STAGED_CHARS
    parts: list[str] = []
    for path, name in req.stage_files:
        try:
            body = Path(path).read_text()
        except (OSError, UnicodeDecodeError) as err:
            parts.append(f"--- FILE {name} ---\n(could not be read: {err})")
            continue
        if len(body) > budget:
            cut = len(body) - budget
            body = body[:budget] + f"\n[... {cut} characters not shown: the bundle hit its size limit ...]"
        budget -= min(len(body), budget)
        parts.append(f"--- FILE {name} ---\n{body}")
        if budget <= 0:
            remaining = [n for _p, n in req.stage_files if n != name]
            if remaining:
                parts.append(f"[... not included, the bundle is full: {', '.join(remaining)} ...]")
            break
    return "\n\n".join(parts)


def _messages(req: ExtractionRequest) -> list[dict[str, str]]:
    staged = _staged_text(req)
    # the CLI prompt points at a directory; here the same files are already in the message
    user = req.user_prompt.replace("{STAGE_DIR}", "the attached files, named")
    schema = json.dumps(req.schema, indent=1)
    system = (
        f"{req.system_prompt}\n\n"
        "Reply with a single JSON object and nothing else: no prose before or after it, no code fence. "
        f"It must validate against this JSON Schema:\n{schema}"
    )
    content = f"{user}\n\n{staged}" if staged else user
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def _parse(text: str) -> dict[str, Any]:
    """Take the JSON object out of a reply, tolerating a code fence but never guessing at the content."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise SchemaInvalidError(f"no JSON object in the reply: {text[:200]!r}")
    return json.loads(body[start:end + 1])


class OpenAIBackend:
    """One HTTP call per request, with the spend ceiling checked before anything is sent."""

    family = FAMILY
    #: the ledger line is written here, not by the cache wrapper: a reply that fails to parse is still
    #: charged, and the retry is a second line
    records_spend = True

    def __init__(self, timeout_s: float = 120.0, max_output_tokens: int | None = None) -> None:
        load_dotenv()
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens or int(
            os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "1400")
        )
        self.price = Price.from_env()
        self.version = "openai/v1/chat.completions"

    def _stream(
        self, payload: dict[str, Any], on_delta: Callable[[str], None]
    ) -> tuple[dict[str, Any], int, str]:
        """Read a server-sent-event reply, handing each content fragment on, and rebuild the whole message."""
        parts: list[str] = []
        usage: dict[str, Any] = {}
        meta: dict[str, Any] = {}
        with httpx.stream(
            "POST",
            ENDPOINT,
            headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout_s,
        ) as response:
            if response.status_code != 200:
                return {}, response.status_code, response.read().decode(errors="replace")
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue  # a keep-alive or a fragment we cannot use; the text is rebuilt from the rest
                meta |= {k: event[k] for k in ("id", "model") if event.get(k)}
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
                        on_delta(piece)
        return {**meta, "usage": usage, "_text": "".join(parts)}, 200, ""

    def _worst_case_usd(self, messages: list[dict[str, str]]) -> float:
        chars = sum(len(m["content"]) for m in messages)
        return self.price.usd(int(chars / 3.5), self.max_output_tokens)

    def call(
        self, req: ExtractionRequest, on_delta: Callable[[str], None] | None = None
    ) -> ExtractionResponse:
        """One request. With `on_delta` the reply is streamed and each text fragment is handed over as it
        arrives, so a caller can show the model's reasoning forming rather than a spinner.

        Streaming changes nothing about what is checked: the fragments are re-assembled and parsed exactly as a
        whole reply would be, and the gate runs on the finished object. A partial answer is never published.
        """
        messages = _messages(req)
        model = req.model if req.model and not req.model.startswith("claude") else model_name()
        # the estimate is deliberately pessimistic: four characters to a token would flatter the check
        check(self._worst_case_usd(messages))

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_completion_tokens": self.max_output_tokens,
        }
        if on_delta is not None:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        started = time.monotonic()
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                if on_delta is not None:
                    body, status, text_body = self._stream(payload, on_delta)
                    r = None
                else:
                    r = httpx.post(
                        ENDPOINT,
                        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
                        json=payload,
                        timeout=self.timeout_s,
                    )
                    status, text_body = r.status_code, r.text
            except httpx.HTTPError as err:
                raise TransientBackendError(f"{type(err).__name__}: {err}") from err
            if status == 429 or status >= 500:
                raise TransientBackendError(f"{status} from OpenAI: {text_body[:300]}")
            if status == 404:
                raise BackendConfigError(
                    f"model {model!r} was not found for this key. "
                    f"Run `lr openai models` to see what it can reach."
                )
            if status != 200:
                raise BackendConfigError(f"{status} from OpenAI: {text_body[:300]}")

            if r is not None:
                body = r.json()
            usage = body.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("completion_tokens") or 0)
            # charged whether or not the reply parses: the tokens were spent either way
            usd = record(model, tokens_in, tokens_out, self.price, task=req.task)
            text = body.get("_text") or (
                ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            )
            try:
                structured = _parse(text)
            except (SchemaInvalidError, json.JSONDecodeError) as err:
                last = err
                if attempt == 2:
                    raise SchemaInvalidError(f"the reply was not usable JSON after two tries: {err}") from err
                check(self._worst_case_usd(messages))
                continue
            return ExtractionResponse(
                structured=structured,
                envelope={"id": body.get("id"), "usage": usage, "usd_estimated": round(usd, 6)},
                backend=FAMILY,
                backend_version=self.version,
                model_requested=model,
                model_resolved=str(body.get("model") or model),
                num_turns=1,
                duration_s=round(time.monotonic() - started, 2),
                usage=usage,
                cost_usd=usd,
                cache_key=req.cache_key(FAMILY),
            )
        raise SchemaInvalidError(str(last))

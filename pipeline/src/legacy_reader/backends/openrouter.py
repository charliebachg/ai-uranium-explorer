"""OpenRouter adapter: any OpenAI-compatible model behind the same Backend protocol, images included.

This exists so an analyst role can run on a cheap model without the Claude subscription's usage limit, and
so the choice of model is an arm switch rather than a rewrite. It is a sibling of the OpenAI adapter, not a
subclass, because three things differ and the parent's call is one piece:

* **Images travel.** The executor reads the map card, so a request's images go into the message as data
  URLs. Only vision models take them; `list_models` says which.
* **The provider prices the call.** OpenRouter reports the dollar cost of each call in its usage block
  when asked, and that figure goes on the ledger as measured, not as arithmetic over a price this code
  guessed. When a reply carries no cost, the model's published prices from the models endpoint are used,
  and failing that a deliberately pessimistic default, so the ceiling can never be flattered.
* **Structured output is asked for and then checked anyway.** The schema goes as `json_schema` for models
  that honour it and as a JSON-object instruction for the rest; either way the reply is parsed and the
  caller's gate validates it, so a model that ignores the schema is refused, never trusted.

The cache family is `openrouter`; a model id is part of every cache key, so two models never share an
answer, and a recording made through this adapter is never served for the CLI or for OpenAI.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from ..runtime.spend import Price, check, record
from .base import (
    BackendConfigError,
    ExtractionRequest,
    ExtractionResponse,
    SchemaInvalidError,
    TransientBackendError,
)
from .openai_api import _parse, _staged_text, load_dotenv

FAMILY = "openrouter"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"
VERSION = "openrouter/v1/chat.completions"
#: a card is 1000 px square; this is a pessimistic token allowance for one image in the pre-call estimate
IMAGE_TOKENS = 1600
#: the request's effort as the provider's reasoning effort: these models think before they answer, the
#: thinking is billed as completion tokens, and unbounded it can eat the whole allowance and return nothing
REASONING_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
#: what a call is priced at when neither the reply nor the models endpoint says: high on purpose
FALLBACK_PRICE = Price(per_mtok_in=2.0, per_mtok_out=8.0)
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def api_key() -> str:
    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise BackendConfigError(
            "OPENROUTER_API_KEY is empty. Put the key in legacy-reader/.env (it is gitignored); "
            "see .env.example for the other settings."
        )
    return key


def is_openrouter_model(model: str) -> bool:
    """OpenRouter ids are `vendor/model`; the CLI's are bare `claude-...`."""
    return "/" in model and not model.startswith("claude")


def list_models(timeout_s: float = 30.0, http: Any = None) -> list[dict[str, Any]]:
    """Every model OpenRouter serves, with its prices per million tokens and whether it takes images and
    honours a JSON schema. Public, so it needs no key; the right thing to read before choosing a model."""
    r = (http or httpx).get(MODELS_ENDPOINT, timeout=timeout_s)
    if r.status_code != 200:
        raise BackendConfigError(f"{MODELS_ENDPOINT} returned {r.status_code}: {r.text[:300]}")
    out: list[dict[str, Any]] = []
    for m in r.json().get("data") or []:
        pricing = m.get("pricing") or {}
        arch = m.get("architecture") or {}
        params = set(m.get("supported_parameters") or [])
        try:
            per_in, per_out = float(pricing.get("prompt") or 0) * 1e6, float(pricing.get("completion") or 0) * 1e6
        except (TypeError, ValueError):
            continue
        out.append({
            "id": str(m.get("id")), "name": str(m.get("name") or ""),
            "usd_per_mtok_in": per_in, "usd_per_mtok_out": per_out,
            "vision": any("image" in str(x) for x in (arch.get("input_modalities") or [])),
            "structured_outputs": "structured_outputs" in params,
            "context_length": m.get("context_length"),
        })
    return sorted(out, key=lambda x: x["id"])


def _image_part(path: Path) -> dict[str, Any]:
    mime = MIME.get(path.suffix.lower(), "image/png")
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def _messages(req: ExtractionRequest) -> list[dict[str, Any]]:
    """System: the prompt and the schema, stated in words as well as sent as `response_format`, so a model
    that ignores the latter still knows the shape. User: the prompt with the staged files inlined, then the
    images as parts."""
    staged = _staged_text(req)
    user = req.user_prompt.replace("{STAGE_DIR}", "the attached files, named")
    schema = json.dumps(req.schema, indent=1)
    system = (
        f"{req.system_prompt}\n\n"
        "Reply with a single JSON object and nothing else: no prose before or after it, no code fence. "
        f"It must validate against this JSON Schema:\n{schema}"
    )
    text = f"{user}\n\n{staged}" if staged else user
    if req.images:
        content: Any = [{"type": "text", "text": text}] + [_image_part(Path(p)) for p in req.images]
    else:
        content = text
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def _text_chars(messages: list[dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            n += len(c)
        else:
            n += sum(len(p.get("text", "")) for p in c if p.get("type") == "text")
    return n


class OpenRouterBackend:
    """One HTTP call per request, priced by the provider, with the ceilings checked before anything is sent."""

    family = FAMILY
    #: the ledger line is written here: a reply that fails to parse is still charged, and the retry is a
    #: second line
    records_spend = True

    def __init__(self, timeout_s: float = 180.0, max_output_tokens: int | None = None, structured: bool = True,
                 prices: dict[str, Price] | None = None, http: Any = None) -> None:
        load_dotenv()
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens or int(os.environ.get("OPENROUTER_MAX_OUTPUT_TOKENS", "8000"))
        self.structured = structured
        self.prices: dict[str, Price] = dict(prices or {})
        self.prices_fetched = prices is not None
        self.http = http or httpx
        self.version = VERSION

    def _price(self, model: str) -> Price:
        """The model's published prices, read once per process from the models endpoint; the pessimistic
        default when the endpoint cannot be read or does not list the model."""
        if model not in self.prices and not self.prices_fetched:
            self.prices_fetched = True
            try:
                for m in list_models(http=self.http):
                    self.prices[m["id"]] = Price(per_mtok_in=m["usd_per_mtok_in"], per_mtok_out=m["usd_per_mtok_out"])
            except (BackendConfigError, httpx.HTTPError):
                pass
        return self.prices.get(model, FALLBACK_PRICE)

    def _worst_case_usd(self, req: ExtractionRequest, messages: list[dict[str, Any]]) -> float:
        tokens_in = int(_text_chars(messages) / 3.5) + IMAGE_TOKENS * len(req.images)
        return self._price(req.model).usd(tokens_in, self.max_output_tokens)

    def _payload(self, req: ExtractionRequest, messages: list[dict[str, Any]], structured: bool,
                 effort: str | None = None, max_tokens: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": req.model, "messages": messages, "max_tokens": max_tokens or self.max_output_tokens,
            "usage": {"include": True},   # the provider's own dollar figure comes back in the usage block
            "reasoning": {"effort": REASONING_EFFORT.get(effort or req.effort, "medium")},
        }
        if structured:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": req.task, "strict": False, "schema": req.schema}}
        else:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        """One request; a schema-shaped ask first, a plain JSON ask if the provider refuses the shape, and one
        retry of a reply that is not usable JSON. Every attempt that reaches the provider is charged."""
        if not is_openrouter_model(req.model):
            raise BackendConfigError(f"{req.model!r} is not an OpenRouter model id (vendor/model)")
        messages = _messages(req)
        check(FAMILY, self._worst_case_usd(req, messages))
        headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
                   "X-Title": "legacy-reader analyst"}
        structured = self.structured
        effort: str | None = None
        max_tokens: int | None = None
        started = time.monotonic()
        last: Exception | None = None
        for attempt in (1, 2, 3):
            payload = self._payload(req, messages, structured, effort=effort, max_tokens=max_tokens)
            try:
                r = self.http.post(ENDPOINT, headers=headers, json=payload, timeout=self.timeout_s)
            except httpx.HTTPError as err:
                raise TransientBackendError(f"{type(err).__name__}: {err}") from err
            status, text_body = r.status_code, r.text
            if status == 429 or status >= 500:
                raise TransientBackendError(f"{status} from OpenRouter: {text_body[:300]}")
            if status in (401, 403):
                raise BackendConfigError(f"{status} from OpenRouter: the key was refused ({text_body[:200]})")
            if status == 404:
                raise BackendConfigError(f"model {req.model!r} was not found on OpenRouter; see list_models()")
            if status == 400 and structured and ("response_format" in text_body or "json_schema" in text_body
                                                 or "structured" in text_body.lower()):
                structured = False   # the provider does not take a schema: ask for a JSON object instead
                continue
            if status != 200:
                raise BackendConfigError(f"{status} from OpenRouter: {text_body[:300]}")
            body = r.json()
            usage = body.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("completion_tokens") or 0)
            cost = usage.get("cost")
            usd = float(cost) if cost is not None else self._price(req.model).usd(tokens_in, tokens_out)
            record(FAMILY, req.model, usd, tokens_in, tokens_out, req.task)
            choice = (body.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content")) or ""
            if choice.get("finish_reason") == "length" and not text.strip():
                # the thinking ate the allowance: ask again with less of it and more room, once
                last = SchemaInvalidError("the reply hit its length limit while reasoning and carried no answer")
                if attempt == 3:
                    raise last
                effort, max_tokens = "low", 2 * (max_tokens or self.max_output_tokens)
                check(FAMILY, self._worst_case_usd(req, messages))
                continue
            try:
                structured_out = _parse(text)
            except (SchemaInvalidError, json.JSONDecodeError) as err:
                last = err
                if attempt == 3:
                    raise SchemaInvalidError(f"the reply was not usable JSON after retries: {err}") from err
                check(FAMILY, self._worst_case_usd(req, messages))
                continue
            return ExtractionResponse(
                structured=structured_out,
                envelope={"id": body.get("id"), "provider": body.get("provider"), "finish_reason": choice.get("finish_reason"),
                          "structured_ask": structured, "cost_reported": cost is not None,
                          "reasoning_effort": payload["reasoning"]["effort"],
                          "reasoning_tokens": ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))},
                backend=FAMILY, backend_version=self.version,
                model_requested=req.model, model_resolved=str(body.get("model") or req.model),
                num_turns=1, duration_s=time.monotonic() - started, usage=usage, cost_usd=usd,
                cache_key=req.cache_key(FAMILY),
            )
        raise SchemaInvalidError(f"no usable reply: {last}")

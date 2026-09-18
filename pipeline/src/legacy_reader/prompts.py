"""Prompt templates and the prompt version that goes into every cache key.

`prompt_version = sha256(system_template + user_template)[:12]` over the *template sources*, not the
rendered text: two pages of the same report share a prompt version, and the per-page difference lives in
the cache key's `context_hash`. Editing a template invalidates every cached call, which is the point.

The user prompt never carries the file number, the company, expected hole names, provincial hole counts,
coordinates or any other outside fact. `carry_text` is the only context that crosses a page boundary,
and everything in it was printed on the previous page of the same table, with its page and quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from .ids import sha256_bytes, short
from .paths import PATHS

SYSTEM_TEMPLATE = "system.md.j2"
USER_TEMPLATE = "user.md.j2"

# Facts that must never reach the model. Checked by `assert_clean` on every rendered prompt.
_FORBIDDEN_HINTS = ("file number", "assessment file", "file_num")


def prompts_dir() -> Path:
    return PATHS.pipeline / "prompts"


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=None, undefined=StrictUndefined, keep_trailing_newline=True, trim_blocks=False,
        autoescape=False,
    )


@lru_cache(maxsize=4)
def _source(name: str) -> str:
    return (prompts_dir() / name).read_text()


@lru_cache(maxsize=1)
def prompt_version() -> str:
    return short(sha256_bytes((_source(SYSTEM_TEMPLATE) + _source(USER_TEMPLATE)).encode("utf-8")))


@dataclass(frozen=True)
class CarryItem:
    """One printed fact carried forward from the previous page of a continued table."""

    label: str
    value: str
    page: int
    quote: str

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value, "page": self.page, "quote": self.quote}


def system_prompt() -> str:
    return _env().from_string(_source(SYSTEM_TEMPLATE)).render().strip()


def user_prompt(route_classes: list[str], carry: list[CarryItem] | None = None,
                image_name: str = "page.png") -> str:
    text = _env().from_string(_source(USER_TEMPLATE)).render(
        route_classes=list(route_classes or []),
        carry=[c.as_dict() for c in (carry or [])],
        image_name=image_name,
    )
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def assert_clean(prompt: str, file_num: str, forbidden: list[str] | None = None) -> None:
    """Refuse to send a prompt that leaks the file's identity or the province's answers."""
    low = prompt.lower()
    bad = [file_num.lower(), *(f.lower() for f in (forbidden or [])), *_FORBIDDEN_HINTS]
    hits = sorted({b for b in bad if b and b in low})
    if hits:
        raise ValueError(f"prompt leaks outside context: {hits}")

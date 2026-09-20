"""API keys and scopes (PRD §E.3, principle 9): who may list which tools, and what a session is bound to.

Three scopes: `read` (open a session, the read tools, `check_claims`, `abstain`, every resource and prompt),
`record` (`record_insight`, which writes the store) and `run` (`run_analyst`). The register is the environment
variable `LR_MCP_KEYS`, read by name only; the format is `<key>:<scope>[,<scope>...]`, entries separated by
`;` (or newlines). A key never appears in a log, a span, a manifest or an error: a principal is named by the
first eight hex characters of the key's sha256, which is enough to tell two keys apart and useless to a reader.

Local-only by default: with no register configured, a loopback client and a stdio client are the `local`
principal with every scope, and a client from any other address gets nothing. With a register configured
every caller presents a key: `Authorization: Bearer <key>` over HTTP, `LR_MCP_KEY` in the environment of a
stdio server (the client launches that process and owns its environment, so over stdio a key selects a scope
set rather than proving anything).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Mapping

SCOPES: tuple[str, ...] = ("read", "record", "run")
ALL_SCOPES: frozenset[str] = frozenset(SCOPES)
#: the register: read by name, never printed
KEYS_VAR = "LR_MCP_KEYS"
#: the key a stdio server's client hands it, when a register is configured
KEY_VAR = "LR_MCP_KEY"
LOCAL = "local"


@dataclass(frozen=True)
class Principal:
    """Who is calling: a label that is not the key, and the scopes the key carries."""

    name: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


def key_label(key: str) -> str:
    """`key:<8 hex>`: names a key in a manifest or a span without carrying it."""
    return "key:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def parse_keys(text: str) -> dict[str, frozenset[str]]:
    """The register's format, checked entry by entry; an error names the entry's position, never its text."""
    out: dict[str, frozenset[str]] = {}
    for n, raw in enumerate(re.split(r"[;\n]", text), start=1):
        entry = raw.strip()
        if not entry:
            continue
        key, sep, scopes = entry.partition(":")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"{KEYS_VAR}: entry {n} is not <key>:<scope>[,<scope>...]")
        if any(c in key for c in ":;,") or any(c.isspace() for c in key):
            raise ValueError(f"{KEYS_VAR}: entry {n}: a key may not contain ':', ';', ',' or white space")
        wanted = {s.strip() for s in scopes.split(",") if s.strip()}
        unknown = sorted(wanted - ALL_SCOPES)
        if not wanted or unknown:
            raise ValueError(f"{KEYS_VAR}: entry {n} names no scope or a scope outside {SCOPES}: {unknown}")
        if key in out:
            raise ValueError(f"{KEYS_VAR}: entry {n} repeats an earlier key")
        out[key] = frozenset(wanted)
    return out


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Keyring:
    """The configured keys, and the principal a caller resolves to."""

    def __init__(self, keys: Mapping[str, frozenset[str]] | None = None) -> None:
        self._keys: dict[str, frozenset[str]] = dict(keys or {})

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Keyring":
        text = (environ if environ is not None else os.environ).get(KEYS_VAR, "")
        return cls(parse_keys(text) if text.strip() else None)

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    def for_key(self, key: str | None) -> Principal | None:
        """The principal a presented key resolves to; None when it is not in the register."""
        if not key:
            return None
        for known, scopes in self._keys.items():
            if hmac.compare_digest(known.encode("utf-8"), key.encode("utf-8")):
                return Principal(key_label(known), scopes)
        return None

    def local(self, environ: Mapping[str, str] | None = None) -> Principal | None:
        """A stdio or in-process caller: `local` with every scope when no register is configured, else the
        principal of the key in `LR_MCP_KEY`."""
        if not self.configured:
            return Principal(LOCAL, ALL_SCOPES)
        return self.for_key((environ if environ is not None else os.environ).get(KEY_VAR))

    def for_http(self, authorization: str | None, client_host: str | None) -> Principal | None:
        """An HTTP caller: loopback only when no register is configured, else the bearer key's principal."""
        if not self.configured:
            return Principal(LOCAL, ALL_SCOPES) if is_loopback(client_host) else None
        if not authorization:
            return None
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer":
            return None
        return self.for_key(token.strip())

    def refusal(self, client_host: str | None) -> str:
        """Why an HTTP caller got nothing, worded for the caller and without the register's contents."""
        if not self.configured:
            return (f"local clients only: {KEYS_VAR} is not configured, so the server answers loopback "
                    f"addresses alone (this request came from {client_host or 'an unknown address'})")
        return f"a bearer API key from {KEYS_VAR} is required: Authorization: Bearer <key>"

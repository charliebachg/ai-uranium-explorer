"""Who is calling the API, and what they may do: one key register, shared with the MCP server.

The register is `UE_MCP_KEYS` and the parser and keyring are `mcp.auth`'s; nothing
about a key is defined twice. The API speaks in roles because that is what a person
is handed: `viewer` is the `read` scope, `geologist` is `read` and `record`, `admin` is `read`, `record` and
`run`. A role is a set of scopes, so a key minted for the MCP server opens the same things on the API and a
route and `tools/list` refuse the same callers.

A caller presents the key as `Authorization: Bearer <key>` or `X-Api-Key: <key>`. With no register configured
a loopback client is the `local` principal with every role, the rule the MCP server applies, so a local
prototype works with no key at all. A refusal is a 401 (nobody could be resolved) or a 403 (a principal
without the role) with a plain reason; the key never appears in it, and a principal is named by its label.
"""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request

from ..mcp.auth import ALL_SCOPES, LOCAL, Keyring, Principal

#: the roles, each the set of scopes it stands for, from the least to the most
ROLES: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read"}),
    "geologist": frozenset({"read", "record"}),
    "admin": frozenset({"read", "record", "run"}),
}
ROLE_ORDER: tuple[str, ...] = tuple(ROLES)
#: the header a browser page sets, beside the bearer form an MCP client uses
KEY_HEADER = "x-api-key"

assert all(scopes <= ALL_SCOPES for scopes in ROLES.values())


def roles_of(principal: Principal) -> list[str]:
    """Every role the principal's scopes cover, least first: a `run` key without `record` holds no role
    above viewer, because a role is all of its scopes or none of it."""
    return [role for role in ROLE_ORDER if ROLES[role] <= principal.scopes]


def has_role(principal: Principal, role: str) -> bool:
    if role not in ROLES:
        raise ValueError(f"no role named {role!r}; the roles are {ROLE_ORDER}")
    return ROLES[role] <= principal.scopes


def resolve(request: Request) -> Principal | None:
    """The caller of this request, from the app's keyring: the bearer key, the `X-Api-Key` header, or the
    local principal for a loopback client when no register is configured."""
    keyring: Keyring = request.app.state.keyring
    client = request.client
    return keyring.for_http(request.headers.get("authorization"), client.host if client else None,
                            api_key=request.headers.get(KEY_HEADER))


def current(request: Request) -> Principal:
    """The FastAPI dependency: the principal, or a 401 that says what was missing."""
    who = resolve(request)
    if who is None:
        keyring: Keyring = request.app.state.keyring
        client = request.client
        raise HTTPException(401, keyring.refusal(client.host if client else None))
    return who


def check(principal: Principal, role: str, what: str) -> None:
    """A 403 when the principal lacks the role `what` needs, naming both without the key."""
    if not has_role(principal, role):
        held = ", ".join(roles_of(principal)) or "no role"
        raise HTTPException(403, f"{what} needs the {role} role; this key ({principal.name}) holds {held}")


def require(role: str) -> Callable[[Request], Principal]:
    """A dependency for a route that needs one fixed role: `Depends(require("geologist"))`."""
    if role not in ROLES:
        raise ValueError(f"no role named {role!r}; the roles are {ROLE_ORDER}")

    def dependency(request: Request) -> Principal:
        who = current(request)
        check(who, role, f"{request.method} {request.url.path}")
        return who

    dependency.__name__ = f"require_{role}"
    return dependency


__all__ = ["KEY_HEADER", "LOCAL", "ROLE_ORDER", "ROLES", "Keyring", "Principal", "check", "current", "has_role",
           "require", "resolve", "roles_of"]

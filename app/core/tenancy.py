"""Host header → tenancy context.

Mirrors `src/lib/tenancy.ts` in the web app. Three kinds of address, and which
one you are on decides what the server will even talk to you about:

*   ``admin.<apex>`` is the super-admin console. Platform staff sign in here
    and nowhere else, and `/platform/*` is refused on any other host.
*   ``<slug>.<apex>`` is exactly one clinic. `northgate.astrale.health` is
    Northgate and can never be anything else.
*   the apex itself is the public face of the product — no console, no clinic.

A reserved label is never a clinic. The tenant is a property of the URL, never
of a request body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.core.config import settings

RESERVED_SUBDOMAINS = frozenset(
    {"www", "app", "admin", "administrator", "api", "platform", "static", "assets", "mail"}
)

#: Labels that resolve to the super-admin console rather than to the apex.
ADMIN_SUBDOMAINS = frozenset({"admin", "administrator"})

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

HostKind = Literal["platform", "admin", "tenant"]


@dataclass(frozen=True)
class HostContext:
    kind: HostKind
    slug: str | None
    root: str
    port: str

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"


def _reserved(slug: str, root: str, port: str) -> HostContext:
    """A reserved label is the console when it names the console, the apex
    otherwise. `api.` and `www.` are not places a person signs in."""
    kind: HostKind = "admin" if slug in ADMIN_SUBDOMAINS else "platform"
    return HostContext(kind, None, root, port)


def resolve_host(host: str | None) -> HostContext:
    raw = (host or "").strip().lower()
    hostname, _, port = raw.partition(":")

    if not hostname or _IPV4.match(hostname) or hostname == "[::1]":
        return HostContext("platform", None, hostname or settings.app_domain, port)

    labels = hostname.split(".")

    if labels[-1] == "localhost":
        if len(labels) == 1:
            return HostContext("platform", None, "localhost", port)
        slug, root = labels[0], ".".join(labels[1:])
        if slug in RESERVED_SUBDOMAINS:
            return _reserved(slug, root, port)
        return HostContext("tenant", slug, root, port)

    if len(labels) <= 2:
        return HostContext("platform", None, hostname, port)

    slug, root = labels[0], ".".join(labels[1:])
    if slug in RESERVED_SUBDOMAINS:
        return _reserved(slug, root, port)
    return HostContext("tenant", slug, root, port)


def slug_problem(slug: str, taken: set[str]) -> str | None:
    """Why a subdomain cannot be claimed, or None when it is fine."""
    if not slug:
        return "Pick a subdomain."
    if len(slug) < 3:
        return "Subdomains need at least three characters."
    if not SLUG_RE.match(slug) or slug.endswith("-"):
        return "Use lowercase letters, numbers and hyphens."
    if slug in RESERVED_SUBDOMAINS:
        return "That subdomain is reserved by the platform."
    if slug in taken:
        return "Another clinic already uses that subdomain."
    return None

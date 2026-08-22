"""Host header → tenancy context.

Mirrors `src/lib/tenancy.ts` in the web app: the apex serves the platform
console, `<slug>.apex` serves exactly one clinic, and a reserved label is never
a clinic. The tenant is a property of the URL, never of a request body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.core.config import settings

RESERVED_SUBDOMAINS = frozenset(
    {"www", "app", "admin", "api", "platform", "static", "assets", "mail"}
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

HostKind = Literal["platform", "tenant"]


@dataclass(frozen=True)
class HostContext:
    kind: HostKind
    slug: str | None
    root: str
    port: str


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
            return HostContext("platform", None, root, port)
        return HostContext("tenant", slug, root, port)

    if len(labels) <= 2:
        return HostContext("platform", None, hostname, port)

    slug, root = labels[0], ".".join(labels[1:])
    if slug in RESERVED_SUBDOMAINS:
        return HostContext("platform", None, root, port)
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

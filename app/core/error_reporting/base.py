"""What an error reporter is.

A reporter takes one `Report` — something went wrong, and here is what we know
about where — and hands it to a service that collects them. It decides
nothing about *what* is worth reporting: that is the logging bridge's job (a
line logged at ERROR is a report), and keeping the decision out of here is
what makes a new service a single small file.

A reporter must never raise into the caller. The bridge guards against it as
well, but an adapter that swallows its own transport failures is the one that
can be trusted on the path of a request that is already failing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, Protocol, runtime_checkable

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]
Level = Literal["error", "critical"]


@dataclass(frozen=True)
class Report:
    #: The line as it was logged, arguments filled in.
    message: str
    #: The same line before its arguments were filled in. Services group on
    #: this, so "sweep failed for 41" and "sweep failed for 42" are one issue.
    template: str
    level: Level
    logger: str
    exc_info: ExcInfo | None = None
    #: request_id, tenant_id, actor_id, path, method — whichever the log line
    #: carried. Identifiers only: patient data is never logged, so never here.
    tags: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class Reporter(Protocol):
    """Implement this, register it, and a deployment can switch to it."""

    name: str

    def report(self, report: Report) -> None:
        """Send one report. Never raises."""
        ...

    def flush(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for anything still in flight."""
        ...


class NullReporter:
    """The default. Errors still reach the log; nothing leaves the process."""

    name = "none"

    def report(self, report: Report) -> None:
        pass

    def flush(self, timeout: float) -> None:
        pass

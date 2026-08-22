"""A small fixed-window limiter.

In-process, so it protects a single worker; put a shared limiter (Redis, or the
edge proxy) in front of a multi-worker deployment. It is here because an
unthrottled login endpoint is a password-guessing oracle, and that is worth
stopping even in one process.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from app.core.errors import RateLimited

_SPEC = re.compile(r"^(\d+)/(second|minute|hour)$")
_WINDOW = {"second": 1, "minute": 60, "hour": 3600}


@dataclass(frozen=True)
class Rate:
    limit: int
    window_seconds: int

    @classmethod
    def parse(cls, spec: str) -> Rate:
        m = _SPEC.match(spec.strip())
        if not m:
            raise ValueError(f"bad rate spec: {spec!r}")
        return cls(int(m.group(1)), _WINDOW[m.group(2)])


class FixedWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, rate: Rate) -> None:
        now = time.monotonic()
        with self._lock:
            count, started = self._hits.get(key, (0, now))
            if now - started >= rate.window_seconds:
                count, started = 0, now
            count += 1
            self._hits[key] = (count, started)
            if len(self._hits) > 50_000:  # crude bound; entries expire by time anyway
                cutoff = now - 3600
                self._hits = {k: v for k, v in self._hits.items() if v[1] > cutoff}
        if count > rate.limit:
            retry_after = int(rate.window_seconds - (now - started)) + 1
            raise RateLimited(
                "Too many requests. Try again shortly.", details={"retry_after_seconds": retry_after}
            )

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = FixedWindowLimiter()

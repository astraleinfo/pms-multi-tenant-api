"""AWS clients, built from settings.

S3 (file storage) and SES (transactional email) share one set of credentials.
Left empty, they fall through to boto3's own chain — an instance or task role,
`~/.aws`, or the standard environment variables — which is what a deployment
running on AWS should use instead of a key in its configuration.

boto3 is imported inside the call, not at the top: a deployment on the default
backends (database storage, SMTP mail) never touches AWS, and nothing on those
paths should depend on the SDK being importable.

boto3 clients are thread-safe, and every call to one is blocking, so callers
run them through `asyncio.to_thread` and share one client per configuration.
"""

from __future__ import annotations

import functools
from typing import Any

from app.core.config import settings


class AwsUnavailable(RuntimeError):
    """The SDK is missing, or the configuration cannot build a client."""


@functools.lru_cache(maxsize=8)
def _client(service: str, region: str, endpoint_url: str, key_id: str, secret: str, token: str) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - boto3 is a declared dependency
        raise AwsUnavailable("boto3 is not installed; `pip install boto3`.") from exc

    return boto3.session.Session(
        aws_access_key_id=key_id or None,
        aws_secret_access_key=secret or None,
        aws_session_token=token or None,
        region_name=region or None,
    ).client(
        service,
        endpoint_url=endpoint_url or None,
        # Standard retry mode backs off on throttling and transient 5xx, which
        # is every failure worth retrying; the timeouts keep a stalled AWS call
        # from holding a request (or a worker) open indefinitely.
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        ),
    )


def client(service: str, *, region: str = "", endpoint_url: str = "") -> Any:
    """A client for `service`, in `region` or else `AWS_REGION`."""
    return _client(
        service,
        region or settings.aws_region,
        endpoint_url,
        settings.aws_access_key_id,
        settings.aws_secret_access_key,
        settings.aws_session_token,
    )


def reset_clients() -> None:
    """Forget every cached client — for tests that change the settings."""
    _client.cache_clear()

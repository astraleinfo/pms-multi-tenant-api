"""Provider credentials at rest.

Fernet, with a key derived from `SECRETS_KEY` when set and from `JWT_SECRET`
otherwise, so development works without another thing to configure. Deriving
from JWT_SECRET has a consequence worth stating: rotating that secret makes
stored provider tokens unreadable, and they have to be re-entered. Set
SECRETS_KEY explicitly to decouple the two.

The decrypt side never raises past the caller — an unreadable token is treated
as absent, which surfaces as "credentials are not set" rather than a 500.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

log = logging.getLogger("pms.messaging")


def _fernet() -> Fernet:
    source = settings.secrets_key or settings.jwt_secret
    key = base64.urlsafe_b64encode(hashlib.sha256(source.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        log.warning("stored provider credential could not be decrypted; treating as unset")
        return ""

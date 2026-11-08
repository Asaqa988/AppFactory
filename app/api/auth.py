import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.core.config import settings


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. `name` is recorded as the reviewer in the approval audit trail."""
    name: str


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


async def require_api_key(x_api_key: str | None = Header(default=None)) -> Principal:
    # Compare fixed-length digests: constant-time, tolerant of any characters in the header
    # (the original secrets.compare_digest(str, str) raises TypeError on non-ASCII input -> HTTP 500).
    if x_api_key:
        supplied = _digest(x_api_key)
        match: str | None = None
        for key, name in settings.principals.items():
            if hmac.compare_digest(supplied, _digest(key)):
                match = name
        if match:
            return Principal(name=match)
    raise HTTPException(status_code=401, detail="Invalid or missing API key.", headers={"WWW-Authenticate": "ApiKey"})

"""Opt-in private playback: a reloadable device-token digest allowlist."""

import hashlib
import hmac
import json
from pathlib import Path

from fastapi import HTTPException, Request


def authorize(request: Request, token_file: str) -> None:
    """Only digests live in the file; reread it so revocation applies immediately."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "playback authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        path = Path(token_file)
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("token file must be private")
        digests = json.loads(path.read_text())
        if not isinstance(digests, dict) or not all(isinstance(value, str) for value in digests.values()):
            raise ValueError("invalid allowlist")
    except (OSError, ValueError):
        raise HTTPException(503, "playback authentication unavailable") from None
    supplied = hashlib.sha256(token.encode()).hexdigest()
    if not any(hmac.compare_digest(supplied, digest) for digest in digests.values()):
        raise HTTPException(403, "playback access denied")

"""Private cookie-only input for a fresh Photos browser, never a saved profile."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class AccountSessionRequiredError(Exception):
    """Stop for operator login; no credential submission or anonymous fallback."""


class AccountSessionMismatchError(AccountSessionRequiredError):
    """The browser or secret belongs to a different account/owner."""


class AccountCookieSecret(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["photos-account-secret-v1"]
    account_id: uuid.UUID
    owner: uuid.UUID
    account_email_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cookies: list[dict[str, Any]] = Field(min_length=1, max_length=256, repr=False)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def cookie_only(self) -> AccountCookieSecret:
        allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}
        for cookie in self.cookies:
            if (
                set(cookie) - allowed
                or not {"name", "value", "domain", "path"} <= set(cookie)
                or not isinstance(cookie["domain"], str)
                or cookie["domain"].lstrip(".") not in {"google.com", "photos.google.com", "accounts.google.com"}
                or not isinstance(cookie["name"], str)
                or not isinstance(cookie["value"], str)
                or not isinstance(cookie["path"], str)
                or not cookie["path"].startswith("/")
            ):
                raise ValueError("invalid Photos authentication cookie")
        return self


def read_account_secret(path: Path, account_id: uuid.UUID, owner: str) -> AccountCookieSecret:
    """Use a private regular file; never include input/validation errors in logs."""
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError()
        if path.parent.stat().st_mode & 0o077:
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 1024 * 1024:
                raise ValueError()
            secret = AccountCookieSecret.model_validate(json.load(handle))
        if secret.account_id != account_id or str(secret.owner) != owner:
            raise AccountSessionMismatchError()
        return secret
    except AccountSessionMismatchError:
        raise
    except Exception:
        raise AccountSessionRequiredError("private Photos session unavailable") from None


def verify_browser_account(page: Any, secret: AccountCookieSecret) -> None:
    """Read account UI only. Challenges/timeouts stop; this never signs in."""
    try:
        page.goto("https://photos.google.com/?hl=en", wait_until="domcontentloaded", timeout=60000)
        account = page.locator('[aria-label^="Google Account:"], [aria-label^="Google 帳戶"]')
        account.first.wait_for(state="visible", timeout=30000)
        labels = account.evaluate_all('(els)=>els.map(e=>e.getAttribute("aria-label"))')
        emails = [email.lower() for label in labels for email in re.findall(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+", label)]
        if len(emails) != 1 or hashlib.sha256(emails[0].encode()).hexdigest() != secret.account_email_sha256:
            raise AccountSessionMismatchError()
    except AccountSessionMismatchError:
        raise
    except Exception:
        raise AccountSessionRequiredError("Photos session requires operator login") from None

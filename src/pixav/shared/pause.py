"""Shared parsing for the operator-controlled system pause value."""

from __future__ import annotations

import json
from typing import Any


def is_paused_value(raw: Any) -> bool:
    """Accept legacy booleans and owned JSON pause records."""
    if raw is None:
        return False
    text = str(raw).strip()
    if not text:
        return False
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text.casefold() in {"1", "true", "yes", "on"}
    if isinstance(payload, dict):
        return payload.get("paused") is True
    return payload is True or payload == 1

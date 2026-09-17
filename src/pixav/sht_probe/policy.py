"""SourcePolicy: validated observations, hard eligibility and deterministic selection."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import AwareDatetime, BaseModel, Field

from pixav.shared.exceptions import SourceUnavailableError
from pixav.sht_probe.scoring import QualityScorer


class CandidateObservation(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    provider: str = Field(min_length=1, max_length=100)
    provider_id: str = Field(min_length=1, max_length=256)
    info_hash: str = Field(pattern=r"^[a-f0-9]{40}$")
    magnet_uri: str = Field(repr=False)
    title: str = Field(min_length=1)
    seeders: int = Field(default=0, ge=0, strict=True)
    size_bytes: int = Field(default=0, ge=0, strict=True)
    provenance: tuple[tuple[str, str], ...] = ()
    unavailable_until: AwareDatetime | None = None


class CandidateEvaluation(BaseModel):
    model_config = {"frozen": True}
    candidate: CandidateObservation
    score: int
    rejection_reasons: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.rejection_reasons


class SourceSelection(BaseModel):
    model_config = {"frozen": True}
    ranked: tuple[CandidateEvaluation, ...]
    selected: CandidateObservation | None
    blocked_reason: str | None = None
    recover_at: AwareDatetime | None = None


class AdapterError(BaseModel):
    model_config = {"frozen": True}
    index: int
    reason: str = "INVALID_PROVIDER_PAYLOAD"


class SourcePolicy:
    version = "source-policy-v1"

    def __init__(self, *, min_score: int = 0, scorer: QualityScorer | None = None) -> None:
        self.scorer = scorer or QualityScorer()
        self.min_score = min_score

    def normalize(self, payload: Mapping[str, Any]) -> CandidateObservation:
        """Retain only allowlisted provider facts; never serialize raw payloads."""
        provider = payload["provider"].strip().casefold()
        identifier = payload["provider_id"].strip()
        if not re.fullmatch(r"[a-z0-9_.-]+", provider) or not re.fullmatch(r"[\w.:-]+", identifier):
            raise ValueError("invalid provider identity")
        uri = payload["magnet_uri"]
        parsed = urlsplit(uri)
        hashes = parse_qs(parsed.query).get("xt", [])
        if parsed.scheme != "magnet" or len(hashes) != 1 or not hashes[0].lower().startswith("urn:btih:"):
            raise ValueError("invalid magnet")
        info_hash = hashes[0][9:]
        if re.fullmatch(r"[A-Z2-7]{32}", info_hash.upper()):
            info_hash = base64.b32decode(info_hash.upper()).hex()
        info_hash = info_hash.lower()
        supplied_hash = payload.get("info_hash", info_hash).upper()
        if re.fullmatch(r"[A-Z2-7]{32}", supplied_hash):
            supplied_hash = base64.b32decode(supplied_hash).hex()
        if supplied_hash.lower() != info_hash:
            raise ValueError("conflicting info hash")
        # Tracker URLs can contain credentials. Canonical identity contains only btih.
        return CandidateObservation(
            provider=provider,
            provider_id=identifier,
            info_hash=info_hash,
            magnet_uri=f"magnet:?xt=urn:btih:{info_hash}",
            title=payload["title"].strip(),
            seeders=payload.get("seeders", 0),
            size_bytes=payload.get("size_bytes", 0),
            provenance=(("provider", provider), ("provider_id", identifier)),
        )

    def normalize_batch(
        self, payloads: Iterable[Mapping[str, Any]]
    ) -> tuple[tuple[CandidateObservation, ...], tuple[AdapterError, ...]]:
        candidates, errors = [], []
        for index, payload in enumerate(payloads):
            try:
                candidates.append(self.normalize(payload))
            except (ValueError, TypeError, KeyError, AttributeError):
                errors.append(AdapterError(index=index))
        return tuple(candidates), tuple(errors)

    def evaluate(self, candidate: CandidateObservation, *, now: datetime) -> CandidateEvaluation:
        reasons = list(self.scorer.eligibility_reasons(candidate.title, candidate.size_bytes))
        score = self.scorer.score(candidate.title, candidate.seeders, candidate.size_bytes)
        if score < self.min_score:
            reasons.append("BELOW_SCORE_THRESHOLD")
        if candidate.unavailable_until is not None and candidate.unavailable_until > now:
            reasons.append("SOURCE_COOLDOWN")
        return CandidateEvaluation(candidate=candidate, score=score, rejection_reasons=tuple(reasons))

    def select(self, candidates: Iterable[CandidateObservation], *, now: datetime) -> SourceSelection:
        ranked = tuple(
            sorted(
                (self.evaluate(c, now=now) for c in candidates),
                key=lambda e: (-e.score, e.candidate.provider, e.candidate.provider_id, e.candidate.info_hash),
            )
        )
        selected = next((e.candidate for e in ranked if e.eligible), None)
        recovery = [
            e.candidate.unavailable_until
            for e in ranked
            if e.rejection_reasons == ("SOURCE_COOLDOWN",) and e.candidate.unavailable_until
        ]
        return SourceSelection(
            ranked=ranked,
            selected=selected,
            blocked_reason=None if selected else "SOURCE_UNAVAILABLE",
            recover_at=min(recovery) if recovery and selected is None else None,
        )

    def record_failure(
        self,
        candidate: CandidateObservation,
        failure: Exception,
        *,
        now: datetime,
        cooldown: timedelta = timedelta(hours=6),
    ) -> CandidateObservation:
        if not isinstance(failure, SourceUnavailableError):
            return candidate
        return CandidateObservation.model_validate({**candidate.model_dump(), "unavailable_until": now + cooldown})

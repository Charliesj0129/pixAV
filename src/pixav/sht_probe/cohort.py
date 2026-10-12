"""Private, finite acceptance scope; never an eligibility or admission override."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixav.sht_probe.source_session import origin


class CohortItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    slot: Literal["direct", "remux", "segmented"]
    page_url: str = Field(repr=False, min_length=1)
    info_hash: str = Field(repr=False, pattern=r"^[0-9a-f]{40}$")


class SourceCohort(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    policy_version: Literal["source-policy-v1"]
    min_score: Literal[0]
    items: tuple[CohortItem, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def unique_slots_and_candidates(self) -> SourceCohort:
        if (
            len({item.slot for item in self.items}) != 3
            or len({item.page_url for item in self.items}) != 3
            or len({item.info_hash for item in self.items}) != 3
        ):
            raise ValueError("cohort requires distinct slots, pages and candidate identities")
        return self

    def includes(self, page_url: str, info_hash: str) -> bool:
        return any(item.page_url == page_url and item.info_hash == info_hash for item in self.items)


def load_cohort(path: str, *, seeds: list[str], min_score: int, managed: bool, queries: str) -> SourceCohort | None:
    if not path:
        return None
    try:
        file = Path(path)
        if (
            file.is_symlink()
            or file.stat().st_mode & 0o777 != 0o600
            or file.parent.stat().st_mode & 0o777 != 0o700
            or file.stat().st_size > 1024 * 1024
        ):
            raise ValueError
        cohort = SourceCohort.model_validate_json(file.read_bytes())
        allowed = {origin(seed) for seed in seeds}
        if (
            not managed
            or queries.strip()
            or min_score != cohort.min_score
            or any(origin(item.page_url) not in allowed for item in cohort.items)
        ):
            raise ValueError
        return cohort
    except Exception:
        raise ValueError("invalid private source cohort or incompatible normal policy") from None

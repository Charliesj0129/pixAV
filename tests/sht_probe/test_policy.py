"""SourcePolicy contracts BDD-007–018; availability clock supplied by repository."""

from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest

from pixav.shared.exceptions import DownloadError, SourceUnavailableError
from pixav.sht_probe.policy import SourcePolicy

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def payload(provider="alpha", identity="release-1", title="synthetic 1080p h264 .mp4", info_hash="a" * 40):
    return dict(provider=provider, provider_id=identity, title=title, magnet_uri=f"magnet:?xt=urn:btih:{info_hash}")


def test_normalization_and_provider_provenance_bdd_007_009():
    policy = SourcePolicy()
    candidates, errors = policy.normalize_batch(
        [payload(), payload("beta"), {**payload(), "password": "not-persisted"}]
    )
    assert not errors
    assert candidates[0].info_hash == candidates[1].info_hash
    assert candidates[0].provenance != candidates[1].provenance
    assert "not-persisted" not in candidates[2].model_dump_json()


@pytest.mark.parametrize(
    "change",
    [
        {"magnet_uri": "invalid"},
        {"seeders": -1},
        {"size_bytes": "NaN"},
        {"title": " "},
        {"info_hash": "b" * 40},
        {"provider_id": "https://user:secret@example.invalid"},
    ],
)
def test_bad_payload_does_not_poison_batch_bdd_008(change):
    candidates, errors = SourcePolicy().normalize_batch([{**payload(), **change}, payload()])
    assert len(candidates) == len(errors) == 1
    assert errors[0].index == 0


def test_hard_rejection_and_stable_ranking_bdd_010_011_012():
    policy = SourcePolicy(min_score=-20000)
    candidates, _ = policy.normalize_batch([payload("beta"), payload("alpha"), payload("gamma", title="4k .iso")])
    for ordering in permutations(candidates):
        selection = policy.select(ordering, now=NOW)
        assert selection.selected.provider == "alpha"
        assert next(e for e in selection.ranked if e.candidate.provider == "gamma").rejection_reasons


def test_zero_seeds_and_infrastructure_keep_candidate_bdd_013_015():
    policy = SourcePolicy()
    candidate = policy.normalize(payload())
    assert policy.evaluate(candidate, now=NOW).eligible
    for failure in (DownloadError(), TimeoutError(), ConnectionError()):
        assert policy.record_failure(candidate, failure, now=NOW) == candidate


def test_cooldown_fallback_exhaustion_and_recovery_bdd_014_016_017_018():
    policy = SourcePolicy()
    first = policy.normalize(payload())
    alternate = policy.normalize(payload("beta", info_hash="b" * 40))
    cooled = policy.record_failure(first, SourceUnavailableError(), now=NOW)
    assert policy.select([cooled, alternate], now=NOW).selected == alternate
    exhausted = policy.select([cooled], now=NOW)
    assert exhausted.selected is None
    assert exhausted.blocked_reason == "SOURCE_UNAVAILABLE"
    assert exhausted.recover_at == NOW + timedelta(hours=6)
    assert policy.select([cooled], now=exhausted.recover_at).selected == cooled
    assert policy.select([cooled, alternate], now=NOW).selected == alternate


def test_base32_and_hex_are_the_same_torrent_identity_bdd_007():
    import base64

    value = base64.b32encode(bytes.fromhex("a" * 40)).decode()
    observation = SourcePolicy().normalize(
        {**payload(), "magnet_uri": f"magnet:?xt=urn:btih:{value}", "info_hash": value}
    )
    assert observation.info_hash == "a" * 40

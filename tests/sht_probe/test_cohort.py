"""A finite source scope must not manufacture eligibility or executable tasks."""

import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from pixav.sht_probe.cohort import SourceCohort, load_cohort
from pixav.sht_probe.policy import CandidateObservation, SourcePolicy
from pixav.sht_probe.service import ShtProbeService


def payload():
    return {
        "schema_version": 1,
        "policy_version": "source-policy-v1",
        "min_score": 0,
        "items": [
            {"slot": slot, "page_url": f"https://example.test/thread-{i}.html", "info_hash": letter * 40}
            for i, (slot, letter) in enumerate(zip(("direct", "remux", "segmented"), "abc", strict=True), 1)
        ],
    }


def test_private_manifest_is_bounded_and_uses_normal_policy(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "cohort.json"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload(), handle)
    args = dict(seeds=["https://example.test/board"], min_score=0, managed=True, queries="")
    cohort = load_cohort(str(path), **args)
    assert cohort.includes("https://example.test/thread-1.html", "a" * 40)
    assert "example.test" not in repr(cohort)
    for change in (
        {"min_score": 10},
        {"managed": False},
        {"queries": "other source"},
        {"seeds": ["https://other.test/"]},
    ):
        with pytest.raises(ValueError, match="invalid private"):
            load_cohort(str(path), **(args | change))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="invalid private"):
        load_cohort(str(path), **args)


def test_manifest_rejects_duplicate_candidates_and_slots():
    data = payload()
    data["items"][1]["info_hash"] = data["items"][0]["info_hash"]
    with pytest.raises(ValueError):
        SourceCohort.model_validate(data)


async def test_only_frozen_native_observations_flow_to_normal_authority():
    queue, repo = AsyncMock(), AsyncMock()
    cohort = SourceCohort.model_validate(payload())
    service = ShtProbeService(video_repo=repo, queue=queue, managed_media_workflow=True, cohort=cohort)
    selected = "magnet:?xt=urn:btih:" + "a" * 40
    replaced = "magnet:?xt=urn:btih:" + "d" * 40
    await service._publish_observations(
        [selected, replaced],
        results=[
            {"magnet_uri": selected, "source_url": cohort.items[0].page_url, "title": "Synthetic 2160p sample.iso"},
            {"magnet_uri": replaced, "source_url": cohort.items[0].page_url, "title": "Synthetic 2160p sample"},
        ],
    )
    queue.push.assert_awaited_once()
    message = queue.push.call_args.args[0]
    assert message["schema"] == "source-observation-v1"
    observation = CandidateObservation.model_validate(message["observation"])
    evaluation = SourcePolicy(min_score=0).evaluate(observation, now=datetime.now(timezone.utc))
    assert not evaluation.eligible
    assert "BLOCKED_FORMAT" in evaluation.rejection_reasons
    repo.insert.assert_not_awaited()
    with pytest.raises(ValueError, match="native discovery"):
        await service.run_search("other source")


async def test_declared_size_reaches_authority_hard_gate():
    from pixav.sht_probe.sehuatang import SehuatangExtractor

    repo, queue, crawler = AsyncMock(), AsyncMock(), AsyncMock()
    crawler.crawl.return_value = ["https://example.test/thread"]
    crawler.fetch_page_html.return_value = (
        '<span id="thread_subject">Synthetic 2160p MP4</span>'
        '<td id="postmessage_1">【影片容量】：20GB '
        '<a href="magnet:?xt=urn:btih:' + "a" * 40 + '">media</a></td>'
    )
    service = ShtProbeService(
        video_repo=repo, queue=queue, crawler=crawler, extractor=SehuatangExtractor(), managed_media_workflow=True
    )
    await service.run_crawl("https://example.test/board")
    observation = CandidateObservation.model_validate(queue.push.call_args.args[0]["observation"])
    assert observation.size_bytes == 20 * 1024**3
    evaluation = SourcePolicy(min_score=0).evaluate(observation, now=datetime.now(timezone.utc))
    assert not evaluation.eligible and "SIZE_LIMIT" in evaluation.rejection_reasons
    repo.insert.assert_not_awaited()

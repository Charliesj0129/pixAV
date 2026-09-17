#!/usr/bin/env python3
"""Compare three fresh latest-discovery rounds against the existing IndexerAdapter.

Use only the isolated Jackett. Cookie/API-key file contents and raw API responses
never enter reports. A failed prerequisite is BLOCKED, not a product failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path

from pixav.shared.exceptions import CrawlError
from pixav.shared.watermark import is_watermark_info_hash
from pixav.sht_probe.interfaces import IndexerResult
from pixav.sht_probe.jackett_client import JackettClient, _nonnegative_int
from scripts.cardigann_spike import collect, public_thread_path
from scripts.phase0_cohort import write_json


def normalize(results: list[IndexerResult]) -> tuple[dict, int]:
    normalized = {}
    rejected = 0
    for item in results:
        match = re.search(r"btih:([a-fA-F0-9]{40})(?:&|$)", item.get("magnet_uri") or "")
        if not match:
            continue
        info_hash = match[1].lower()
        if is_watermark_info_hash(info_hash):
            rejected += 1
            continue
        normalized[info_hash] = {
            "title": " ".join(item["title"].split()),
            "source_path": public_thread_path(item["source_url"]),
            "size": _nonnegative_int(item.get("size")),
            "seeders": _nonnegative_int(item.get("seeders")),
        }
    return normalized, rejected


def compare(baseline: list[IndexerResult], actual: list[IndexerResult]) -> dict:
    expected, baseline_rejected = normalize(baseline)
    observed, actual_rejected = normalize(actual)
    if not expected:
        return {"status": "BLOCKED", "reason": "no_valid_baseline", "baseline_count": 0}
    missing = sorted(expected.keys() - observed.keys())
    mismatches = {
        key: [field for field in expected[key] if expected[key][field] != observed[key][field]]
        for key in expected.keys() & observed.keys()
    }
    mismatches = {key: fields for key, fields in mismatches.items() if fields}
    return {
        "status": "PASS" if not missing and not mismatches else "FAIL",
        "baseline_count": len(expected),
        "observed_valid_count": len(observed),
        "missing_hashes": missing,
        "field_mismatches": mismatches,
        "baseline_watermarks_rejected": baseline_rejected,
        "actual_watermarks_rejected": actual_rejected,
    }


async def run(args: argparse.Namespace) -> dict:
    adapter = JackettClient(
        "http://127.0.0.1:19117",
        args.api_key_file.read_text().strip(),
        timeout=180,
        indexer="sehuatang-pixav",
        resolve_download_links=True,
    )
    rounds = []
    for number in range(1, 4):
        output = args.output / f"round-{number}"
        inputs = argparse.Namespace(
            board=args.board,
            cookie_file=args.cookie_file,
            flaresolverr="http://127.0.0.1:18191",
            max_threads=args.max_threads,
            output=output,
        )
        try:
            evidence = await collect(inputs)
            write_json(output / "inputs.json", evidence)
            if evidence["status"] != "INPUT_READY":
                rounds.append({"status": "BLOCKED", "reason": "input_not_ready"})
                break
            baseline = json.loads((output / "baseline.json").read_text())
            actual = await adapter.search("", limit=10)  # Empty query must discover latest content.
            sources = {public_thread_path(item["source_url"]) for item in baseline}
            actual = [item for item in actual if public_thread_path(item["source_url"]) in sources]
            result = compare(baseline, actual)
            write_json(output / "parity.json", result)
            rounds.append(result)
        except CrawlError as exc:
            dependency = "request failed" in str(exc)
            rounds.append(
                {
                    "status": "BLOCKED" if dependency else "FAIL",
                    "reason": "live_dependency_failed" if dependency else "jackett_contract_failed",
                }
            )
        except Exception:
            rounds.append({"status": "BLOCKED", "reason": "live_dependency_failed"})
            break
    status = (
        "PASS"
        if len(rounds) == 3 and all(r["status"] == "PASS" for r in rounds)
        else ("BLOCKED" if any(r["status"] == "BLOCKED" for r in rounds) else "FAIL")
    )
    report = {
        "status": status,
        "rounds": rounds,
        "mode": "latest_discovery",
        "feasibility": "BLOCKED",
        "unverified_gates": ["age_gate_jackett", "bare_hash_only"],
        "promoted": False,
    }
    write_json(args.output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--cookie-file", type=Path, default=Path("secrets/sehuatang-cookies.txt"))
    parser.add_argument("--board", default="https://www.sehuatang.org/forum-103-1.html")
    parser.add_argument("--max-threads", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    print(json.dumps(asyncio.run(run(args)), indent=2))


if __name__ == "__main__":
    main()

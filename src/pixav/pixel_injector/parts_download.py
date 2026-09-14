"""Cold Photos recovery process: mount only /work, never source or upload roots.

Input manifest travels over stdin. This process has no DB credential and does not
publish a database row; the parent checks the report before committing readiness.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from pixav.media_loader.video_parts import PartMedia, contained_file, require_space, retained_temporary, sha256
from pixav.pixel_injector.canary_download import extract_original
from pixav.shared.models import VideoPart


def quarantine(root: Path, *paths: Path) -> Path:
    """Move untrusted retrieval artifacts aside, preserving them for inspection.

    Deleting them would destroy the only evidence of what went wrong, and keeping
    them in place would block every later attempt. They are moved into a
    timestamped folder instead, which leaves the part free to be fetched again.
    """
    holding = root / "quarantine" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    holding.mkdir(mode=0o700, parents=True)
    for path in paths:
        if path.exists() or path.is_symlink():
            os.replace(path, holding / path.name)
    return holding


def download_original(part: VideoPart, root: Path) -> dict:
    from playwright.sync_api import sync_playwright

    if not part.share_url or not part.share_url.startswith(
        ("https://photos.app.goo.gl/", "https://photos.google.com/")
    ):
        raise ValueError("Photos share location missing")
    destination = root / part.filename
    receipt = root / (part.filename + ".json")
    stray = root / (part.filename + ".part")
    if destination.exists():
        contained_file(root, destination)
        if not receipt.is_file() or receipt.is_symlink():
            # Bytes without a receipt mean a crash between publishing the file and
            # recording how it arrived. The file cannot be trusted and cannot be
            # explained, so it is moved aside and fetched from Photos again.
            # Writing a receipt for it would be a fabrication.
            quarantine(root, destination, receipt, stray)
        else:
            report = json.loads(receipt.read_text())
            if (
                sha256(destination) != part.sha256
                or destination.stat().st_size != part.size_bytes
                or report.get("sha256") != part.sha256
                or report.get("size") != part.size_bytes
                or report.get("method") != "photos-original-browser"
            ):
                raise ValueError("cached Photos original is corrupt; retained for inspection")
            return report
    elif any(p.exists() or p.is_symlink() for p in (stray, receipt)):
        # A half-written extraction would make the retry fail its exclusive open.
        quarantine(root, stray, receipt)
    # Browser download, save_as archive and extraction can coexist. Retained
    # quarantine bytes are already charged to actual filesystem free space.
    require_space([(root, part.size_bytes * 3 + 16 * 1024**2)])
    with retained_temporary(prefix="browser-", directory=root) as temporary:
        # Linux container deadline includes ZIP construction and save_as, which
        # Playwright's click/download-event timeout does not bound.
        signal.alarm(21600)
        archive = Path(temporary) / "original.zip"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(downloads_path=str(temporary))
            try:
                page = browser.new_page(accept_downloads=True)
                page.goto(part.share_url, wait_until="domcontentloaded", timeout=60000)
                page.get_by_role("button", name="More options", exact=True).click(timeout=30000)
                with page.expect_download(timeout=120000) as pending:
                    page.get_by_role("menuitem", name="Download all", exact=True).click(timeout=30000)
                pending.value.save_as(archive)
                version = browser.version
            finally:
                browser.close()
        extract_original(archive, destination, part.filename, part.size_bytes, part.sha256)
        signal.alarm(0)
    report = {
        "method": "photos-original-browser",
        "size": part.size_bytes,
        "sha256": part.sha256,
        "at": datetime.now(timezone.utc).isoformat(),
        "browser": version,
    }
    # Crash after byte publication leaves an untrusted cached file, never inferred success.
    with receipt.open("x") as handle:
        json.dump(report, handle)
    return report


def prepare(manifest: dict, root: Path) -> dict:
    parts = [VideoPart.model_validate(item) for item in manifest["parts"]]
    if len(parts) < 2 or [p.part_index for p in parts] != list(range(len(parts))):
        raise ValueError("incomplete ordered manifest")
    if any(p.video_id != parts[0].video_id or p.manifest_version != parts[0].manifest_version for p in parts):
        raise ValueError("mixed manifest identities")
    cloud = root / "cloud"
    cloud.mkdir(mode=0o700, exist_ok=True)
    if cloud.is_symlink():
        raise ValueError("cloud root is a symlink")
    reports = [download_original(p, cloud) for p in parts]
    media = PartMedia()
    reference = manifest["reference"]
    destination = root / f"{parts[0].video_id}.mp4"
    if destination.exists():
        contained_file(root, destination)
        media.compare(reference, media.fingerprint(destination, root, reference["duration"]))
    else:
        require_space([(root, int(sum(p.size_bytes for p in parts) * 1.05) + int(reference["duration"] * 120 * 256))])
        with retained_temporary(prefix="merge-", directory=root) as temporary:
            partial = Path(temporary) / "complete.mp4"
            media.merge(
                [contained_file(cloud, cloud / p.filename) for p in parts],
                partial,
                reference["duration"],
                spans=[p.end_seconds - p.start_seconds for p in parts],
            )
            media.compare(reference, media.fingerprint(partial, root, reference["duration"]))
            with partial.open("rb") as handle:
                os.fsync(handle.fileno())
            os.link(partial, destination)
    return {
        "parts": reports,
        "content": "PASS",
        "cold_inputs": "photos-only",
        "filename": destination.name,
        "size": destination.stat().st_size,
        "sha256": sha256(destination),
        "at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    os.umask(0o077)
    try:
        result = prepare(json.load(sys.stdin), Path("/work"))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

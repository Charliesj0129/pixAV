"""Cold Photos recovery process: mount only /work, never source or upload roots.

Input manifest travels over stdin. This process has no DB credential and does not
publish a database row; the parent checks the report before committing readiness.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pixav.media_loader.preparation import inspect_media
from pixav.media_loader.video_parts import PartMedia, contained_file, require_space, retained_temporary, sha256
from pixav.pixel_injector.canary_download import (
    OriginalMismatchError,
    OriginalWorkConflictError,
    copy_original,
    decode_original,
)
from pixav.pixel_injector.photos_auth import (
    AccountCookieSecret,
    AccountSessionMismatchError,
    AccountSessionRequiredError,
    verify_browser_account,
)
from pixav.shared.cold_receipts import AUTHENTICATED_RECEIPT
from pixav.shared.media_file import (
    MediaFile,
    MediaFileChangedError,
    MediaFileUnsafeError,
    file_version,
    open_media_directory,
    open_media_file,
    open_media_output,
)
from pixav.shared.models import VideoPart
from pixav.shared.storage_models import TransferableFile


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


def observed_media(path: Path) -> dict:
    """Probe the bytes that were actually retrieved, not the ones expected.

    Size and hash prove these are the bytes the manifest asked for. They cannot
    say what media those bytes are, so whole-asset consistency -- duration,
    codecs, container -- has nothing to check against unless the read-back
    reports what it saw. ffprobe is already present in this image.

    This process is a synchronous container entry point with no running event
    loop of its own, so the shared async probe is driven directly rather than
    duplicated as a second ffprobe call site.
    """
    return json.loads(asyncio.run(inspect_media(str(path))).model_dump_json())


def _signed_out(url: str) -> bool:
    """A signed-out Photos visit lands on the sign-in page or Google's Photos product page."""
    parts = urlsplit(url)
    return parts.hostname == "accounts.google.com" or (
        parts.hostname in {"google.com", "www.google.com"} and parts.path.startswith("/photos")
    )


def _browser_original(
    part: TransferableFile, temporary: Path, original: Path, auth: AccountCookieSecret | None
) -> tuple[str, dict | None]:
    from playwright.sync_api import sync_playwright

    account_binding = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(downloads_path=str(temporary))
        try:
            if auth is None:
                page = browser.new_page(accept_downloads=True)
            else:
                context = browser.new_context(accept_downloads=True, locale="en-US")
                context.add_cookies(auth.cookies)
                page = context.new_page()
                verify_browser_account(page, auth)
            page.goto(part.share_url, wait_until="domcontentloaded", timeout=60000)
            if auth is not None:
                if _signed_out(page.url):
                    raise AccountSessionRequiredError("Photos session requires operator login")
                page.goto(
                    page.url + ("&" if "?" in page.url else "?") + "hl=en",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                if _signed_out(page.url):
                    raise AccountSessionRequiredError("Photos session requires operator login")
            # An independently inspected private item can be reopened by its
            # original account. The filename and complete bytes remain required.
            private_item = auth is not None and "/photo/" in urlsplit(page.url).path
            if not private_item:
                videos = page.get_by_role("link", name=re.compile(r"^Video -"))
                videos.first.wait_for(state="visible", timeout=30000)
                if videos.count() != 1:
                    raise OriginalMismatchError("Photos album must contain exactly one video")
                videos.click(timeout=30000)
            # Wait for the viewer before opening its menu. During navigation
            # the album's identically named menu still offers Download all.
            info = page.get_by_role("button", name=re.compile(r"^(?:Open info|顯示詳細資訊)$"))
            info.wait_for(state="visible", timeout=30000)
            if auth is not None:
                info.click(timeout=30000)
                filename = getattr(part, "provider_filename", part.filename)
                page.get_by_text(filename, exact=True).wait_for(state="visible", timeout=30000)
                account_binding = {
                    "mode": "original-account",
                    "account_id": str(auth.account_id),
                    "owner": str(auth.owner),
                    "account_email_sha256": auth.account_email_sha256,
                    "session_id": str(uuid.uuid4()),
                    "item_url_sha256": hashlib.sha256(page.url.encode()).hexdigest(),
                    "provider_filename_sha256": hashlib.sha256(filename.encode()).hexdigest(),
                    "original_sha256": part.sha256,
                    "fresh_context": True,
                    "cookie_only": True,
                    "local_media_inputs": False,
                }
                page.get_by_role("button", name=re.compile(r"^(?:Close info|隱藏詳細資訊)$")).click(timeout=30000)
            page.get_by_role("button", name=re.compile(r"^(?:More options|更多選項)$")).last.click(timeout=30000)
            # The individual original action has been verified against full
            # hashes. Download-all ZIP preparation can stall on a backed-up
            # video; do not substitute the browser's playback rendition.
            # Keep the start wait
            # bounded separately from the six-hour process deadline.
            with page.expect_download(timeout=600000) as pending:
                page.get_by_role(
                    "menuitem",
                    name=re.compile(r"^(?:Download(?: - Shift\+D)?|下載(?:\s*-\s*Shift\s*\+\s*D(?:\s*鍵)?)?)$"),
                ).click(timeout=30000)
            download = pending.value
            download.save_as(original)
            if download.suggested_filename != getattr(part, "provider_filename", part.filename):
                raise OriginalMismatchError("Photos original filename does not match provider identity")
            version = browser.version
        finally:
            browser.close()
    return version, account_binding


def _check_original(source: MediaFile, part: TransferableFile, version: tuple[int, int, int, int, int]) -> None:
    facts = source.facts()
    if file_version(os.fstat(source.descriptor)) != version or facts != (part.sha256, part.size_bytes):
        raise OriginalWorkConflictError("COLD_ORIGINAL_CHANGED_DURING_VERIFICATION")


def _reuse_original(part: TransferableFile, destination: Path, receipt: Path) -> dict:
    with open_media_file(destination) as source, open_media_file(receipt) as evidence:
        version = file_version(os.fstat(source.descriptor))
        receipt_version = file_version(os.fstat(evidence.descriptor))
        receipt_facts = evidence.facts()
        with evidence.reader() as reader:
            report = json.load(reader)
        if (
            source.facts() != (part.sha256, part.size_bytes)
            or report.get("sha256") != part.sha256
            or report.get("size") != part.size_bytes
            or report.get("method") != "photos-original-browser"
        ):
            raise OriginalWorkConflictError("cached Photos original is corrupt; retained for inspection")
        # The probe owns its child descriptor while this outer reader keeps the
        # original inode locked through observation and final receipt checks.
        observed = observed_media(destination)
        _check_original(source, part, version)
        evidence.assert_current()
        if file_version(os.fstat(evidence.descriptor)) != receipt_version or evidence.facts() != receipt_facts:
            raise OriginalWorkConflictError("COLD_ORIGINAL_RECEIPT_CHANGED_DURING_VERIFICATION")
        if observed.get("sha256") != part.sha256 or observed.get("size_bytes") != part.size_bytes:
            raise OriginalWorkConflictError("COLD_ORIGINAL_OBSERVATION_CHANGED")
        return {**report, "observed": observed}


def _publish_original_receipt(
    part: TransferableFile,
    source: MediaFile,
    version: tuple[int, int, int, int, int],
    receipt: Path,
    report: dict,
) -> None:
    pending = receipt.with_suffix(receipt.suffix + ".pending")
    encoded = json.dumps(report).encode()
    with open_media_output(pending, must_create=True) as output:
        with os.fdopen(os.dup(output.descriptor), "w") as handle:
            json.dump(report, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _check_original(source, part, version)
        if output.facts() != (hashlib.sha256(encoded).hexdigest(), len(encoded)):
            raise OriginalWorkConflictError("COLD_ORIGINAL_PENDING_RECEIPT_CHANGED")
        with open_media_directory(receipt, exclusive=True) as directory:
            source.assert_current()
            if file_version(os.fstat(source.descriptor)) != version:
                raise OriginalWorkConflictError("COLD_ORIGINAL_CHANGED_BEFORE_RECEIPT_PUBLICATION")
            output.assert_current()
            os.link(
                pending.name,
                receipt.name,
                src_dir_fd=directory.descriptor,
                dst_dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
            MediaFile(receipt, output.descriptor, directory.descriptor, True).assert_current()
            output.assert_current()
            os.unlink(pending.name, dir_fd=directory.descriptor)
            os.fsync(directory.descriptor)


def _record_original(
    part: TransferableFile, destination: Path, receipt: Path, browser_version: str, account_binding: dict | None
) -> dict:
    with open_media_file(destination) as source:
        version = file_version(os.fstat(source.descriptor))
        _check_original(source, part, version)
        observed = observed_media(destination)
        if observed.get("sha256") != part.sha256 or observed.get("size_bytes") != part.size_bytes:
            raise OriginalWorkConflictError("COLD_ORIGINAL_OBSERVATION_CHANGED")
        _check_original(source, part, version)
        report = {
            "method": "photos-original-browser",
            "size": part.size_bytes,
            "sha256": part.sha256,
            "at": datetime.now(timezone.utc).isoformat(),
            "browser": browser_version,
            "download_action": "single-video-original",
            "observed": observed,
        }
        if account_binding is not None:
            decode_original(source)
            report.update(
                receipt_version=AUTHENTICATED_RECEIPT,
                authentication=account_binding,
                cold_inputs="provider-only",
                cold_mounts=["/work:rw"],
                decode="PASS",
            )
        _publish_original_receipt(part, source, version, receipt, report)
        return report


def download_original(part: TransferableFile, root: Path, *, authentication: dict | None = None) -> dict:
    """Retain conflicted local work without reporting provider corruption."""
    try:
        return _download_original(part, root, authentication=authentication)
    except (OSError, MediaFileChangedError, MediaFileUnsafeError):
        raise OriginalWorkConflictError("COLD_ORIGINAL_WORK_REQUIRES_RECONCILIATION") from None


def _download_original(part: TransferableFile, root: Path, *, authentication: dict | None = None) -> dict:
    if not part.share_url or not part.share_url.startswith(
        ("https://photos.app.goo.gl/", "https://photos.google.com/")
    ):
        raise ValueError("Photos share location missing")
    destination = root / part.filename
    receipt = root / (part.filename + ".json")
    stray = root / (part.filename + ".part")
    pending_receipt = receipt.with_suffix(receipt.suffix + ".pending")
    auth = AccountCookieSecret.model_validate(authentication) if authentication is not None else None
    if auth is not None and getattr(part, "account_id", None) != auth.account_id:
        raise AccountSessionMismatchError()
    if auth is not None and any(p.exists() or p.is_symlink() for p in (destination, receipt, stray, pending_receipt)):
        raise ValueError("authenticated cold retrieval requires fresh inputs")
    account_binding = None
    if destination.exists():
        contained_file(root, destination)
        if not receipt.is_file() or receipt.is_symlink():
            # Bytes without a receipt mean a crash between publishing the file and
            # recording how it arrived. The file cannot be trusted and cannot be
            # explained, so it is moved aside and fetched from Photos again.
            # Writing a receipt for it would be a fabrication.
            quarantine(root, destination, receipt, stray, pending_receipt)
        else:
            # A receipt written before the observation was part of the contract
            # describes bytes that are still here and still hash correctly, so
            # the observation is recomputed rather than the download repeated.
            return _reuse_original(part, destination, receipt)
    elif any(p.exists() or p.is_symlink() for p in (stray, receipt, pending_receipt)):
        # A half-written extraction would make the retry fail its exclusive open.
        quarantine(root, stray, receipt, pending_receipt)
    # Browser download, save_as and verified publication can coexist. Retained
    # quarantine bytes are already charged to actual filesystem free space.
    require_space([(root, part.size_bytes * 3 + 16 * 1024**2)])
    with retained_temporary(prefix="browser-", directory=root) as temporary:
        # Linux container deadline includes provider preparation and save_as, which
        # Playwright's click/download-event timeout does not bound.
        signal.alarm(21600)
        try:
            original = Path(temporary) / "original.mp4"
            version, account_binding = _browser_original(part, Path(temporary), original, auth)
            with open_media_file(contained_file(Path(temporary), original)) as source, source.reader() as stream:
                copy_original(stream, destination, part.size_bytes, part.sha256)
            # Keep failed browser work, the original and any pending receipt for
            # reconciliation until the complete verification has committed.
            return _record_original(part, destination, receipt, version, account_binding)
        finally:
            signal.alarm(0)


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
            with open_media_file(partial) as source:
                os.fsync(source.descriptor)
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

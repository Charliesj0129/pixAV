#!/usr/bin/env python3
"""Headed Jellyfin UI capture, with real PulseAudio output and FFmpeg analysis.

Run only in the isolated capture image. The private config names one Jellyfin
item, its immutable asset binding, synthetic recipe, and UI login. No original
media is mounted. Failures preserve the recording and never produce PASS.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
import traceback
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pixav.shared.backup_files import create_backup_file
from pixav.strm_resolver.av_acceptance import FFMPEG, SyntheticAVSpec, analyze_window, decode_marker
from pixav.strm_resolver.real_av_acceptance import RealAVSpec


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _save(path: Path, payload: dict) -> None:
    with create_backup_file(path) as handle:
        json.dump(payload, handle, indent=2)


def _recording_clock(stream, clock: dict) -> None:
    """Use encoder media PTS; wall/device clocks may jump during a recording."""
    for line in iter(stream.readline, ""):
        if line.startswith("out_time_us="):
            value = line.split("=", 1)[1].strip()
            if value not in {"N/A", ""}:
                clock["latest"] = (max(0, int(value) / 1_000_000), time.monotonic())


def _checkpoint_time(clock: dict) -> float:
    if "latest" not in clock:
        raise ValueError("recorder media clock unavailable")
    seconds, sampled_at = clock["latest"]
    age = time.monotonic() - sampled_at
    if age > 1:
        raise ValueError("recorder media clock is stale")
    return seconds + age


async def _wait_recorder_clock(page, process, clock: dict, *, timeout_seconds: float = 60) -> None:
    """Require a live encoder and fresh media PTS before scheduling windows."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError("recorder exited before its media clock was ready")
        try:
            _checkpoint_time(clock)
            return
        except ValueError:
            await page.wait_for_timeout(100)
    raise ValueError("recorder media clock did not become ready within the startup deadline")


async def _picture_time(page) -> float:
    picture = await page.screenshot()
    decoded = await asyncio.to_thread(
        subprocess.run,
        [
            FFMPEG,
            "-v",
            "error",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-f",
            "image2pipe",
            "-i",
            "pipe:0",
            "-vf",
            "scale=640:360",
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        input=picture,
        capture_output=True,
        check=True,
        timeout=10,
    )  # noqa: S603 -- fixed FFmpeg argv, screenshot bytes only
    return decode_marker(decoded.stdout)


async def _presented_time(page) -> float:
    """Observe actual presented frames; this is scheduling, never content proof."""
    return float(await page.evaluate("""() => new Promise((resolve,reject) => {
        const v=document.querySelector('video');
        const timer=setTimeout(()=>reject(new Error('presented frame unavailable')),5000);
        v.requestVideoFrameCallback((_,m)=>{clearTimeout(timer);resolve(m.mediaTime)});
    })"""))


async def _wait_picture(page, target: float, *, picture_reader=None) -> None:
    deadline = time.monotonic() + 60
    previous: tuple[float, float] | None = None
    while time.monotonic() < deadline:
        try:
            picture = await (picture_reader or _picture_time)(page)
            sampled_at = time.monotonic()
            if previous is not None:
                elapsed = sampled_at - previous[1]
                progress = picture - previous[0]
                if abs(picture - target) <= 2 and elapsed * 0.65 <= progress <= elapsed * 1.3:
                    return
            previous = (picture, sampled_at)
        except ValueError:
            previous = None
        await page.wait_for_timeout(200)
    raise ValueError("client did not display the requested picture time")


def _real_seek_geometry(control: dict, target: float, duration: float) -> tuple[float, float]:
    """Use Jellyfin's actual percent track and step, without time injection."""
    if control["min"] != 0 or control["max"] != 100 or control["step"] <= 0:
        raise ValueError("unsupported Jellyfin seek control scale")
    if control["step"] * duration / 100 > 1:
        raise ValueError("Jellyfin seek precision is insufficient for the two-second bound")
    if control["width"] <= 0 or not 0 <= target <= duration:
        raise ValueError("invalid Jellyfin seek geometry")
    return control["left"] + control["width"] * target / duration, control["top"] + control["height"] / 2


async def _seek(page, target: float, duration: float, *, stuck: bool = False, picture_reader=None) -> None:
    await page.mouse.move(640, 360)
    await page.mouse.move(640, 660, steps=10)
    if not stuck:
        video = page.locator("video").first
        if not await video.evaluate("v => v.paused"):
            await page.get_by_role("button", name="Pause", exact=True).click(timeout=15000)
        await page.wait_for_function("() => document.querySelector('video')?.paused", timeout=5000)
        slider = (
            page.locator("input.osdPositionSlider")
            if picture_reader is _presented_time
            else page.get_by_role("slider").first
        )
        await slider.wait_for(state="visible", timeout=15000)
        if picture_reader is _presented_time:
            # Keep the actual position control exposed while the OSD animates.
            # Other range inputs include volume and hidden subtitle offsets.
            await slider.hover(timeout=15000)
            await page.wait_for_function(
                """() => {
                const e=document.querySelector('input.osdPositionSlider');
                return e && e.getBoundingClientRect().height>0 &&
                    (e.sliderBubbleTrack||e).getBoundingClientRect().width>0;
            }""",
                timeout=5000,
            )
        rect = await slider.bounding_box()
        if not rect:
            raise ValueError("Jellyfin seek control has no geometry")
        if picture_reader is _presented_time:
            control = await slider.evaluate("""e => {
                const r=(e.sliderBubbleTrack||e).getBoundingClientRect(),input=e.getBoundingClientRect();
                return {min:Number(e.min),max:Number(e.max),step:Number(e.step),
                    left:r.left,top:input.top,width:r.width,height:input.height};
            }""")
            x, y = _real_seek_geometry(control, target, duration)
            await page.mouse.click(x, y)
        else:
            await slider.click(
                position={
                    "x": max(1, min(rect["width"] - 1, rect["width"] * target / duration)),
                    "y": rect["height"] / 2,
                }
            )
        await page.get_by_role("button", name="Play", exact=True).click(timeout=15000)
    await page.wait_for_function(
        "() => {const v=document.querySelector('video');return v && v.readyState>=3 && !v.paused}", timeout=45000
    )
    # Allow the actual audio output to settle after a UI seek as well.
    # Picture readiness still enforces the same two-second position bound.
    await page.wait_for_timeout(200)
    if not stuck:
        await _wait_picture(page, target, picture_reader=picture_reader)


async def _play(page, details: str, *, resume: bool = False) -> None:
    await page.goto(details, wait_until="domcontentloaded", timeout=60000)
    if resume:
        # The details view can retain pre-stop UserData during hash navigation.
        # Reload and wait for its actual Resume action, never inject a position.
        await page.reload(wait_until="domcontentloaded", timeout=60000)
    selector = '.btnPlay[data-action="resume"][title="Resume"]:not(.hide)' if resume else ".btnPlay:not(.hide)"
    button = page.locator(selector).first
    await button.wait_for(state="visible", timeout=30000)
    await button.click()
    await page.wait_for_function(
        "() => {const v=document.querySelector('video');return v && v.readyState>=3 && !v.paused}", timeout=60000
    )
    await page.wait_for_timeout(600)


async def _wait_saved_resume(page, server_id: str, item_id: str, target: float) -> float:
    """Wait for Jellyfin's asynchronous stop receipt, without changing it."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        position = await page.evaluate(
            """async ({serverId,itemId}) => {
            const credentials=JSON.parse(localStorage.getItem('jellyfin_credentials')||'{}');
            const server=(credentials.Servers||[]).find(s=>s.Id===serverId && s.AccessToken);
            if(!server) throw new Error('client credential binding unavailable');
            const response=await fetch('/Users/'+server.UserId+'/Items/'+itemId,
                {headers:{Authorization:'MediaBrowser Token="'+server.AccessToken+'"'},cache:'no-store'});
            if(!response.ok) throw new Error('resume receipt unavailable');
            return ((await response.json()).UserData?.PlaybackPositionTicks||0)/10000000;
        }""",
            {"serverId": server_id, "itemId": item_id},
        )
        if abs(position - target) <= 2:
            return float(position)
        await page.wait_for_timeout(250)
    raise ValueError("Jellyfin did not persist the expected resume position")


async def _stop_playback(page) -> None:
    """Use Jellyfin's stop action so closing Chromium cannot orphan a stream."""
    if not await page.locator("video").count():
        return
    await page.mouse.move(640, 360)
    await page.mouse.move(640, 660)
    try:
        await page.get_by_role("button", name="Back", exact=True).click(timeout=5000)
    except Exception as error:
        if type(error).__name__ != "TimeoutError":
            raise
        # Pausing by fault injection can hide Jellyfin's player controls.
        # Browser history is still a real client navigation/stop operation.
        await page.keyboard.press("Alt+ArrowLeft")
    await page.wait_for_function(
        "() => {const v=document.querySelector('video');return !v || v.paused || v.ended || !v.getAttribute('src')}",
        timeout=5000,
    )


async def _close_capture_client(page, browser, output: Path, *, primary_failure: bool) -> None:
    """Retain stop failures without replacing the original capture failure."""
    try:
        if page is not None:
            await _stop_playback(page)
    except Exception as exc:
        _save(output / "stop-failure.json", {"error_type": type(exc).__name__})
        if not primary_failure:
            raise
    finally:
        await browser.close()


def _capture_targets(spec: SyntheticAVSpec | RealAVSpec, junction_seconds: list[float]) -> list[tuple[str, float]]:
    real = isinstance(spec, RealAVSpec)
    targets = [("middle", spec.duration_seconds / 2 - (5 if real else 0))]
    for boundary in spec.junction_seconds if real else junction_seconds:
        targets.append(("junction_crossing", float(boundary) - (7 if real else 3)))
    if real:
        targets.extend(("supplemental", target) for target in spec.supplemental_seconds)
    # Resume must follow the normal middle-of-item seek. A supplemental window
    # near the credits can exceed Jellyfin's bookmark retention threshold.
    targets.append(("seek", spec.duration_seconds * 0.3))
    return targets


async def _configure_capture_resolution(page, maximum_height: int) -> dict:
    """Set the native preference in this fresh client, never reuse a browser profile."""
    if maximum_height != 1080:
        raise ValueError("unsupported capture resolution contract")
    await page.get_by_role("button", name="User Menu", exact=True).click()
    await page.get_by_role("menuitem", name="Settings", exact=True).click()
    await page.get_by_text("Playback", exact=True).click(timeout=15000)
    resolution = page.get_by_label("Maximum Allowed Video Transcoding Resolution", exact=True)
    await resolution.wait_for(state="visible", timeout=15000)
    await page.locator(".selectSkipBackLength option").first.wait_for(state="attached", timeout=15000)
    await page.locator(".docspinner").wait_for(state="hidden", timeout=15000)
    await resolution.select_option(label="1080p")
    selected = await resolution.input_value()
    supported = page.get_by_label("Limit maximum supported video resolution", exact=True)
    if not await supported.is_checked():
        await page.get_by_text("Limit maximum supported video resolution", exact=True).click(timeout=15000)
    if not await supported.is_checked():
        raise ValueError("native client supported resolution limit was not selected")
    save = page.get_by_role("button", name="Save", exact=True)
    if await save.count() != 1:
        raise ValueError("native client settings save control unavailable")
    async with page.expect_response(
        lambda response: urlsplit(response.url).path.endswith("/Configuration") and response.request.method == "POST",
        timeout=15000,
    ) as saved:
        await save.click(timeout=15000)
    if (await saved.value).status != 204:
        raise ValueError("native client settings save was not acknowledged")
    await page.reload(wait_until="domcontentloaded")
    await resolution.wait_for(state="visible", timeout=15000)
    await page.locator(".selectSkipBackLength option").first.wait_for(state="attached", timeout=15000)
    await page.locator(".docspinner").wait_for(state="hidden", timeout=15000)
    if await resolution.input_value() != selected or not await supported.is_checked():
        raise ValueError("native client resolution preference was not retained")
    return {
        "maximum_height": maximum_height,
        "native_preference_retained": True,
        "limit_supported_resolution": True,
        "quality": "8 Mbps",
    }


async def _select_capture_quality(page, maximum_height: int) -> None:
    # Explicit bitrate selection requests transcoding; a resolution ceiling alone
    # does not constrain a direct-play original. All actions use the native UI.
    await page.mouse.move(640, 660)
    await page.locator(".btnVideoOsdSettings").click(timeout=15000)
    await page.get_by_text("Quality", exact=True).click(timeout=15000)
    await page.get_by_role("button", name="8 Mbps", exact=True).click(timeout=15000)
    await page.wait_for_function(
        "height => {const v=document.querySelector('video');return v && v.readyState>=3 && !v.paused"
        " && v.videoHeight>0 && v.videoHeight<=height}",
        arg=maximum_height,
        timeout=60000,
    )


async def _manual_login_field(page):
    """Handle Jellyfin's automatic login-view transition before entering secrets."""
    username = page.locator("#txtManualName")
    await page.locator("#txtManualName:visible, .btnManual:visible").first.wait_for(state="visible", timeout=30000)
    if not await username.is_visible():
        try:
            await page.locator(".btnManual").click(timeout=5000)
        except Exception as error:
            if type(error).__name__ != "TimeoutError" or not await username.is_visible():
                raise
    await username.wait_for(state="visible", timeout=30000)
    return username


async def _capture(  # noqa: C901 -- explicit UI acceptance steps
    config: dict, spec: SyntheticAVSpec | RealAVSpec, output: Path, fault: str | None
) -> dict:
    from playwright.async_api import async_playwright

    base = config["jellyfin_url"].rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname != "jellyfin" or parsed.port != 8096 or parsed.path:
        raise ValueError("capture only accepts the isolated Jellyfin endpoint")
    if config.get("browser_channel") not in (None, "chrome"):
        raise ValueError("unsupported capture browser channel")
    real = isinstance(spec, RealAVSpec)
    picture_reader = _presented_time if real else _picture_time
    trace = {
        "schema_version": 2 if real else 1,
        "item_id": config["item_id"],
        "checkpoints": [],
        "fault_injection": fault,
    }
    if real:
        trace.update(real_spec=spec.model_dump(mode="json"), binding=config["binding"], status="CAPTURED")

    async def seek(target: float, *, stuck: bool = False) -> None:
        operation_start = _checkpoint_time(clock) if real and trace.get("recording_timebase") else None
        await _seek(page, target, spec.duration_seconds, stuck=stuck, picture_reader=picture_reader)
        if operation_start is not None:
            trace.setdefault("controls", []).append(
                {
                    "kind": "seek",
                    "target_seconds": target,
                    "recording_start_seconds": operation_start,
                    "recording_end_seconds": _checkpoint_time(clock),
                }
            )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            channel=config.get("browser_channel"),
            ignore_default_args=["--mute-audio"],
            args=["--start-fullscreen", "--window-size=1280,720", "--autoplay-policy=no-user-gesture-required"],
        )
        primary_failure = False
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 720}, locale="en-US")
            page = await context.new_page()
            subprocess.run(
                ["/usr/bin/xdotool", "key", "F11"], check=True, capture_output=True, timeout=10
            )  # noqa: S603
            await page.wait_for_timeout(500)
            await page.goto(base + "/web/", wait_until="domcontentloaded", timeout=60000)
            username = await _manual_login_field(page)
            await username.fill(config["username"])
            await page.locator("#txtManualPassword").fill(config["password"])
            await page.locator("button[type=submit]").click()
            await page.wait_for_url("**/home*", timeout=45000)
            public = await context.request.get(base + "/System/Info/Public")
            info = await public.json()
            trace.update(client_version=browser.version, server_version=info["Version"], server_id=info["Id"])
            if real and config.get("max_client_height"):
                trace["native_client_quality"] = await _configure_capture_resolution(page, config["max_client_height"])
            details = base + "/web/#/details?id=" + config["item_id"] + "&serverId=" + info["Id"]
            await _play(page, details)
            if real and config.get("max_client_height"):
                await _select_capture_quality(page, config["max_client_height"])
            if await picture_reader(page) > 2:
                await seek(0)
            session = await page.evaluate(
                """async ({serverId,itemId}) => {
                const credentials=JSON.parse(localStorage.getItem('jellyfin_credentials')||'{}');
                const server=(credentials.Servers||[]).find(s=>s.Id===serverId && s.AccessToken);
                if(!server) throw new Error('client credential binding unavailable');
                const response=await fetch('/Sessions?ControllableByUserId='+server.UserId,
                    {headers:{Authorization:'MediaBrowser Token="'+server.AccessToken+'"'}});
                if(!response.ok) throw new Error('client session metadata unavailable');
                const rows=(await response.json()).filter(s=>s.UserId===server.UserId && s.NowPlayingItem?.Id===itemId);
                if(rows.length!==1) throw new Error('client session identity is ambiguous');
                return {session_id:rows[0].Id,device_id:rows[0].DeviceId,application_version:rows[0].ApplicationVersion};
            }""",
                {"serverId": info["Id"], "itemId": config["item_id"]},
            )
            trace.update(session)
            if real:
                dimensions = await page.locator("video").first.evaluate(
                    "v => ({width:v.videoWidth,height:v.videoHeight})"
                )
                trace["client_media_dimensions"] = dimensions
                if config.get("max_client_height") and dimensions["height"] > config["max_client_height"]:
                    raise ValueError("client media exceeds the bound capture resolution")
            # Capture only after login; credential-entry screens are excluded.
            recording = output / "client.mkv"
            capture_origin = time.time()
            with create_backup_file(output / "ffmpeg.log") as log:
                process = subprocess.Popen(  # noqa: S603 -- fixed OSS commands, private output paths
                    [
                        FFMPEG,
                        "-hide_banner",
                        "-nostdin",
                        "-y",
                        "-thread_queue_size",
                        "1024",
                        "-probesize",
                        "32",
                        "-analyzeduration",
                        "0",
                        "-copyts",
                        "-use_wallclock_as_timestamps",
                        "1",
                        "-f",
                        "x11grab",
                        "-framerate",
                        "25",
                        "-video_size",
                        "1280x720",
                        "-i",
                        ":99.0",
                        "-thread_queue_size",
                        "1024",
                        "-use_wallclock_as_timestamps",
                        "1",
                        "-f",
                        "pulse",
                        "-probesize",
                        "32",
                        "-analyzeduration",
                        "0",
                        "-i",
                        "pixav_capture.monitor",
                        "-vf",
                        f"setpts=PTS-{capture_origin}/TB",
                        "-af",
                        f"asetpts=PTS-{capture_origin}/TB",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "18",
                        "-threads",
                        "2",
                        "-c:a",
                        "pcm_s16le",
                        "-progress",
                        "pipe:1",
                        "-stats_period",
                        "0.1",
                        str(recording),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=log,
                    text=True,
                    bufsize=1,
                )
                clock: dict = {}
                clock_thread = threading.Thread(target=_recording_clock, args=(process.stdout, clock), daemon=True)
                clock_thread.start()
                trace["recording_timebase"] = "ffmpeg-media-pts"
                try:
                    await _wait_recorder_clock(page, process, clock)
                    await page.wait_for_timeout(200)
                    # Avoid restarting an already presented opening. A second
                    # zero seek can race the native transcoding player startup.
                    if await picture_reader(page) > 2:
                        await seek(0)
                    video = page.locator("video").first
                    if fault == "mute":
                        await video.evaluate("v => {v.muted=true}")
                    elif fault == "freeze":
                        await video.evaluate("v => v.pause()")
                    elif fault == "wrong-tone":
                        await page.evaluate(
                            """() => {document.querySelector('video').muted=true;
                        const c=new AudioContext(),o=c.createOscillator(),g=c.createGain();
                        o.frequency.value=1500;g.gain.value=.25;o.connect(g);g.connect(c.destination);o.start();window._faultAudio=c;}"""
                        )
                    if fault in {"mute", "wrong-tone"}:
                        # Jellyfin shows a temporary volume OSD on mute. Let
                        # that UI animation finish before decoding the marker;
                        # the injected audio fault remains active throughout.
                        await page.wait_for_timeout(3000)

                    async def checkpoint(kind: str, target: float, *, boundary: float | None = None) -> None:
                        sample_duration = 14 if real and boundary is not None else 10
                        rect = await video.bounding_box()
                        if not rect:
                            raise ValueError("client video has no geometry")
                        actual = await video.evaluate(
                            "v => ({width:v.videoWidth,height:v.videoHeight,duration:v.duration})"
                        )
                        if not actual["width"] or abs(actual["duration"] - spec.duration_seconds) > 2:
                            raise ValueError("Jellyfin item duration differs from the bound reference")
                        if config.get("max_client_height") and actual["height"] > config["max_client_height"]:
                            raise ValueError("checkpoint exceeds the bound capture resolution")
                        # Aspect-ratio fitting is performed by the browser inside this box.
                        scale = min(rect["width"] / actual["width"], rect["height"] / actual["height"])
                        width, height = round(actual["width"] * scale), round(actual["height"] * scale)
                        x, y = round(rect["x"] + (rect["width"] - width) / 2), round(
                            rect["y"] + (rect["height"] - height) / 2
                        )
                        trace["checkpoints"].append(
                            {
                                "kind": kind,
                                **({"boundary_seconds": boundary} if boundary is not None else {}),
                                "expected_seconds": target,
                                "recording_seconds": _checkpoint_time(clock),
                                **(
                                    {
                                        "duration_seconds": sample_duration,
                                        "presented_seconds": await picture_reader(page),
                                    }
                                    if real
                                    else {}
                                ),
                                "crop": [width, height, x, y],
                            }
                        )
                        await page.wait_for_timeout(
                            sample_duration * 1000 + 200 if real else 4300 if boundary is not None else 1500
                        )

                    # Sample the opening after device/AAC startup, still within
                    # the required first five seconds of actual picture time.
                    await page.wait_for_timeout(100 if real else 2200)
                    first = await picture_reader(page)
                    if first > 5 and fault not in {"mute", "wrong-tone"}:
                        raise ValueError("beginning checkpoint is outside the first five seconds")
                    await checkpoint(
                        "audio_fault_probe" if fault in {"mute", "wrong-tone"} else "beginning", 0 if real else first
                    )
                    if fault not in {"freeze", "mute", "wrong-tone"}:
                        targets = _capture_targets(spec, config.get("junction_seconds", []))
                        for kind, target in targets:
                            await seek(target, stuck=fault == "seek-stuck")
                            await checkpoint(
                                kind,
                                target,
                                boundary=target + (7 if real else 3) if kind == "junction_crossing" else None,
                            )
                        # Freeze the actual player through its UI before measuring
                        # the saved bookmark. Screenshot decoding and opening
                        # Back otherwise let this short clip advance by seconds.
                        await page.mouse.move(640, 360)
                        await page.mouse.move(640, 660, steps=10)
                        if not await video.evaluate("v => v.paused"):
                            await page.get_by_role("button", name="Pause", exact=True).click(timeout=15000)
                        await page.wait_for_function("() => document.querySelector('video')?.paused", timeout=5000)
                        resume_at = (
                            float(await video.evaluate("v => v.currentTime")) if real else await _picture_time(page)
                        )
                        await page.mouse.move(640, 660)
                        resume_operation_start = _checkpoint_time(clock) if real else None
                        await page.get_by_role("button", name="Back", exact=True).click(timeout=15000)
                        trace["saved_resume_seconds"] = await _wait_saved_resume(
                            page, info["Id"], config["item_id"], resume_at
                        )
                        await _play(page, details, resume=True)
                        if real:
                            trace.setdefault("controls", []).append(
                                {
                                    "kind": "resume",
                                    "target_seconds": resume_at,
                                    "recording_start_seconds": resume_operation_start,
                                    "recording_end_seconds": _checkpoint_time(clock),
                                }
                            )
                        video = page.locator("video").first
                        resumed_picture = await picture_reader(page)
                        trace["resume_first_seconds"] = resumed_picture
                        if fault == "resume-wrong":
                            await seek(0)
                        else:
                            if abs(resumed_picture - resume_at) > 2:
                                raise ValueError("initial resume picture exceeds the two-second tolerance")
                        # A ready HTML video can still show the first resume
                        # frame while decoding starts. Observe actual presented
                        # frames, without changing playback time or the capture
                        # tolerance, before opening its recording window.
                        await page.evaluate("""() => new Promise((resolve,reject) => {
                            const v=document.querySelector('video');
                            const timer=setTimeout(()=>reject(new Error('resume frames did not advance')),3000);
                            let first;
                            const next=(_,metadata)=>{
                                first ??= metadata.mediaTime;
                                if(metadata.mediaTime-first>=.08){clearTimeout(timer);resolve();}
                                else v.requestVideoFrameCallback(next);
                            };
                            v.requestVideoFrameCallback(next);
                        })""")
                        await checkpoint("resume", resume_at)
                        # Inspect the ending last so short clips cannot finish
                        # while the next seek/resume control is being opened.
                        ending = spec.duration_seconds - (10 if real else 4)
                        await seek(ending)
                        await checkpoint("end", ending)
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                    try:
                        await asyncio.to_thread(process.wait, timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait)
                    _save(output / "trace.json", trace)
                    clock_thread.join(timeout=2)
        except Exception:
            primary_failure = True
            if "page" in locals():
                # Diagnostic capture must not obscure the error being investigated.
                # Player state excludes URLs, credentials and source metadata.
                with suppress(Exception):
                    await page.screenshot(path=str(output / "failure-screen.png"))
                    controls = await page.evaluate("""() => [...document.querySelectorAll('input,button,[role=slider]')]
                    .filter(e=>e.getBoundingClientRect().width>0).map(e=>({tag:e.tagName,classes:e.className,
                    type:e.getAttribute('type'),label:e.getAttribute('aria-label'),title:e.getAttribute('title')}))""")
                    _save(output / "failure-controls.json", {"controls": controls})
                    videos = await page.evaluate("""() => [...document.querySelectorAll('video')].map(v=>({
                        paused:v.paused,ended:v.ended,readyState:v.readyState,networkState:v.networkState,
                        currentTime:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:null,
                        width:v.videoWidth,height:v.videoHeight,errorCode:v.error?.code??null,
                        visible:v.getBoundingClientRect().width>0}))""")
                    _save(output / "failure-video.json", {"videos": videos})
            raise
        finally:
            await _close_capture_client(locals().get("page"), browser, output, primary_failure=primary_failure)
    return trace


def _capture_spec(config: dict) -> SyntheticAVSpec | RealAVSpec:
    real = "real_spec" in config
    if real and "synthetic_spec" in config:
        raise ValueError("capture requires exactly one reference mode")
    spec = (
        RealAVSpec.model_validate(config["real_spec"])
        if real
        else SyntheticAVSpec.model_validate(config["synthetic_spec"])
    )
    if not real and spec.method != "pixav-synthetic-av-v2":
        raise ValueError("new client capture requires the unobstructed v2 synthetic marker")
    return spec


async def run(args: argparse.Namespace) -> None:
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    spec = _capture_spec(config)
    real = isinstance(spec, RealAVSpec)
    output = args.output / str(uuid.uuid4())
    output.mkdir(mode=0o700, parents=True)
    os.environ["DISPLAY"] = ":99"
    processes = []
    result = {
        "method": "jellyfin-real-capture-v2" if real else "automated-jellyfin-av-v1",
        "status": "FAIL",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        processes.append(
            subprocess.Popen(
                ["/usr/bin/Xvfb", ":99", "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )  # noqa: S603
        await asyncio.sleep(0.5)
        processes.append(
            subprocess.Popen(["/usr/bin/openbox", "--sm-disable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        )  # noqa: S603
        subprocess.run(
            ["/usr/bin/pulseaudio", "--start", "--exit-idle-time=-1"], check=True, capture_output=True, timeout=10
        )  # noqa: S603
        subprocess.run(
            ["/usr/bin/pactl", "load-module", "module-null-sink", "sink_name=pixav_capture"],
            check=True,
            capture_output=True,
            timeout=10,
        )  # noqa: S603
        subprocess.run(
            ["/usr/bin/pactl", "set-default-sink", "pixav_capture"], check=True, capture_output=True, timeout=10
        )  # noqa: S603
        await asyncio.sleep(0.5)
        trace = await asyncio.wait_for(_capture(config, spec, output, args.fault), timeout=1800 if real else 600)
        if real:
            result.update(
                status="CAPTURED",
                review="REVIEW_REQUIRED",
                binding=config["binding"],
                recording_sha256=_sha256(output / "client.mkv"),
                trace_sha256=_sha256(output / "trace.json"),
                client_version=trace["client_version"],
                server_version=trace["server_version"],
                item_id=trace["item_id"],
                session_id=trace["session_id"],
                device_id=trace["device_id"],
            )
            return
        analyses = []
        for window in trace["checkpoints"]:
            try:
                analyses.append(analyze_window(output / "client.mkv", spec, window))
            except ValueError as exc:
                analyses.append({**window, "status": "FAIL", "failure_code": str(exc)})
        _save(output / "analysis.json", {"spec_sha256": spec.sha256, "checkpoints": analyses})
        if any(window["status"] != "PASS" for window in analyses):
            raise ValueError("one or more recorded audiovisual checkpoints failed")
        kinds = {window["kind"] for window in analyses}
        if not {"beginning", "middle", "end", "seek", "resume"} <= kinds:
            raise ValueError("required playback checkpoints missing")
        result.update(
            status="PASS",
            binding=config["binding"],
            synthetic_spec_sha256=spec.sha256,
            recording_sha256=_sha256(output / "client.mkv"),
            analysis_sha256=_sha256(output / "analysis.json"),
            trace_sha256=_sha256(output / "trace.json"),
            client_version=trace["client_version"],
            server_version=trace["server_version"],
            session_id=trace["session_id"],
            device_id=trace["device_id"],
            application_version=trace["application_version"],
            item_id=trace["item_id"],
            checkpoints=analyses,
        )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        # Diagnostic locations exclude exception payloads and local variables,
        # which may contain client credentials or provider response bodies.
        result["failure_locations"] = [
            {"function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).name == "capture_jellyfin_av.py"
        ]
        if type(exc) is ValueError:
            result["failure_code"] = str(exc)
    finally:
        _save(output / "result.json", result)
        for process in processes:
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
    print("automated client capture " + result["status"] + "; private artifacts retained")
    if result["status"] != "PASS":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/work"))
    parser.add_argument("--fault", choices=["mute", "freeze", "wrong-tone", "seek-stuck", "resume-wrong"])
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

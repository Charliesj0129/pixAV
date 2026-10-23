"""Recording windows follow media PTS, and unavailable/stale clocks fail closed."""

from io import StringIO

import pytest

from scripts.capture_jellyfin_av import (
    _capture_targets,
    _checkpoint_time,
    _close_capture_client,
    _manual_login_field,
    _real_seek_geometry,
    _recording_clock,
    _stop_playback,
    _wait_picture,
    _wait_recorder_clock,
    _wait_saved_resume,
)


@pytest.mark.parametrize("automatic_transition", [True, False])
async def test_login_transition_race_only_continues_when_username_field_is_visible(automatic_transition):
    from unittest.mock import AsyncMock, Mock

    username = Mock(is_visible=AsyncMock(side_effect=[False, automatic_transition]), wait_for=AsyncMock())
    manual = Mock(click=AsyncMock(side_effect=TimeoutError("manual button disappeared")))
    initial = Mock(first=Mock(wait_for=AsyncMock()))
    page = Mock(
        locator=Mock(
            side_effect=lambda selector: (
                username if selector == "#txtManualName" else manual if selector == ".btnManual" else initial
            )
        )
    )
    if automatic_transition:
        assert await _manual_login_field(page) is username
        username.wait_for.assert_awaited_once()
    else:
        with pytest.raises(TimeoutError):
            await _manual_login_field(page)
        username.wait_for.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_recorder_startup_waits_for_fresh_encoder_pts(monkeypatch, stale):
    now = [10.0]
    clock = {"latest": (1, 0)} if stale else {}

    class Page:
        async def wait_for_timeout(self, milliseconds):
            now[0] += milliseconds / 1000
            clock["latest"] = (2, now[0])

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: now[0])
    await _wait_recorder_clock(Page(), Process(), clock)
    assert now[0] == pytest.approx(10.1)
    assert _checkpoint_time(clock) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [None, 1])
async def test_recorder_startup_refuses_dead_encoder_or_missing_pts(monkeypatch, exit_code):
    now = [0.0]

    class Page:
        async def wait_for_timeout(self, milliseconds):
            now[0] += milliseconds / 1000

    class Process:
        def poll(self):
            return exit_code

    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: now[0])
    with pytest.raises(ValueError, match="recorder"):
        await _wait_recorder_clock(Page(), Process(), {}, timeout_seconds=0.25)
    assert now[0] <= 0.3 + 1e-6


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_failure", [False, True])
async def test_stop_failure_preserves_original_error_but_cannot_accept_success(monkeypatch, tmp_path, primary_failure):
    closed = []

    async def stop(page):
        raise TimeoutError("player did not stop")

    class Browser:
        async def close(self):
            closed.append(True)

    monkeypatch.setattr("scripts.capture_jellyfin_av._stop_playback", stop)
    if primary_failure:
        await _close_capture_client(object(), Browser(), tmp_path, primary_failure=True)
    else:
        with pytest.raises(TimeoutError):
            await _close_capture_client(object(), Browser(), tmp_path, primary_failure=False)
    assert closed == [True]
    assert (tmp_path / "stop-failure.json").read_text().find("TimeoutError") != -1


def test_long_real_seek_uses_actual_track_without_a_one_pixel_start_offset():
    control = {"min": 0, "max": 100, "step": 0.01, "left": 50, "top": 650, "width": 1100, "height": 20}
    assert _real_seek_geometry(control, 0, 8381.4615) == (50, 660)
    assert _real_seek_geometry(control, 8381.4615 * 0.3, 8381.4615) == pytest.approx((380, 660))
    control["step"] = 1
    with pytest.raises(ValueError, match="precision"):
        _real_seek_geometry(control, 2500, 8381.4615)


def test_recorder_clock_tracks_media_jump_without_using_wall_time(monkeypatch):
    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: 100.0)
    clock = {}
    _recording_clock(StringIO("frame=10\nout_time_us=N/A\nout_time_us=5000000\nout_time_us=12000000\n"), clock)
    assert clock["latest"] == (12.0, 100.0)
    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: 100.2)
    assert _checkpoint_time(clock) == pytest.approx(12.2)


def test_missing_and_stale_media_clock_refuse_a_checkpoint(monkeypatch):
    with pytest.raises(ValueError, match="unavailable"):
        _checkpoint_time({})
    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: 100.0)
    with pytest.raises(ValueError, match="stale"):
        _checkpoint_time({"latest": (5, 98.9)})


@pytest.mark.asyncio
async def test_picture_readiness_waits_for_moving_frames_after_resume(monkeypatch):
    clock = [0.0]
    frames = iter([20.0, 20.0, 20.0, 20.2, 20.4, 20.6, 20.8])

    class Page:
        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    async def picture_time(page):
        return next(frames)

    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("scripts.capture_jellyfin_av._picture_time", picture_time)
    await _wait_picture(Page(), 20)
    assert clock[0] == pytest.approx(1.2)


@pytest.mark.asyncio
async def test_correct_but_permanently_frozen_resume_picture_is_rejected(monkeypatch):
    clock = [0.0]

    class Page:
        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    async def picture_time(page):
        return 20.0

    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("scripts.capture_jellyfin_av._picture_time", picture_time)
    with pytest.raises(ValueError, match="requested picture time"):
        await _wait_picture(Page(), 20)


@pytest.mark.asyncio
@pytest.mark.parametrize("burst_seconds", [0.2, 0.7])
async def test_short_moving_burst_before_a_frozen_resume_cannot_start_a_window(monkeypatch, burst_seconds):
    clock = [0.0]

    class Page:
        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    async def picture_time(page):
        return 20 + min(clock[0], burst_seconds)

    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: clock[0])
    with pytest.raises(ValueError, match="requested picture time"):
        await _wait_picture(Page(), 20, picture_reader=picture_time)


@pytest.mark.asyncio
async def test_resume_buffering_restarts_the_continuous_picture_readiness_window(monkeypatch):
    clock = [0.0]

    class Page:
        async def wait_for_timeout(self, milliseconds):
            clock[0] += milliseconds / 1000

    async def picture_time(page):
        return 20 + min(clock[0], 0.2) + max(0, clock[0] - 0.8)

    monkeypatch.setattr("scripts.capture_jellyfin_av.time.monotonic", lambda: clock[0])
    await _wait_picture(Page(), 20, picture_reader=picture_time)
    assert clock[0] >= 1.6 - 1e-9


@pytest.mark.asyncio
async def test_resume_waits_for_server_stop_receipt_without_writing_it():
    positions = iter([0, 0, 19.5])
    calls = []

    class Page:
        async def evaluate(self, script, identity):
            assert "fetch('/Users/'" in script and "method:" not in script
            assert identity == {"serverId": "server", "itemId": "item"}
            calls.append("read")
            return next(positions)

        async def wait_for_timeout(self, milliseconds):
            calls.append(milliseconds)

    assert await _wait_saved_resume(Page(), "server", "item", 20) == 19.5
    assert calls == ["read", 250, "read", 250, "read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_capture_closes_stream_through_jellyfin_ui_before_browser_exit(active):
    calls = []

    class Locator:
        async def count(self):
            return int(active)

        async def click(self, **kwargs):
            calls.append("stop")

    class Mouse:
        async def move(self, x, y):
            calls.append("show-controls")

    class Page:
        mouse = Mouse()

        def locator(self, selector):
            assert selector == "video"
            return Locator()

        def get_by_role(self, role, **kwargs):
            assert role == "button" and kwargs == {"name": "Back", "exact": True}
            return Locator()

        async def wait_for_function(self, script, **kwargs):
            assert "v.paused" in script and "v.ended" in script and "!v.getAttribute('src')" in script
            calls.append("stopped")

    await _stop_playback(Page())
    assert calls == (["show-controls", "show-controls", "stop", "stopped"] if active else [])


@pytest.mark.asyncio
async def test_hidden_player_back_uses_browser_navigation_to_stop():
    calls = []

    class Locator:
        async def count(self):
            return 1

        async def click(self, **kwargs):
            raise TimeoutError("hidden player control")

    class Mouse:
        async def move(self, x, y):
            pass

    class Keyboard:
        async def press(self, key):
            calls.append(key)

    class Page:
        mouse = Mouse()
        keyboard = Keyboard()

        def locator(self, selector):
            return Locator()

        def get_by_role(self, *args, **kwargs):
            return Locator()

        async def wait_for_function(self, script, **kwargs):
            calls.append("stopped")

    await _stop_playback(Page())
    assert calls == ["Alt+ArrowLeft", "stopped"]


def test_near_end_supplement_does_not_leave_resume_in_jellyfin_completed_region():
    from pixav.strm_resolver.real_av_acceptance import RealAVSpec

    spec = RealAVSpec(
        duration_seconds=3600,
        manifest_sha256="a" * 64,
        junction_seconds=(1200,),
        supplemental_seconds=(3580,),
    )
    targets = _capture_targets(spec, [])
    assert {kind for kind, _ in targets} == {"middle", "junction_crossing", "supplemental", "seek"}
    assert targets[-1][0] == "seek"
    assert 60 < targets[-1][1] + 10 < spec.duration_seconds * 0.9
    assert ("supplemental", spec.supplemental_seconds[0]) in targets

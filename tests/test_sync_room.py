"""Unit tests for SyncRoom authoritative playback, queue, ready-barrier, and speaker state."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from backend.audio_cache import AudioCache
from backend.config import server_now_ms
from backend.models import Track
from backend.sync_room import SyncRoom


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


def make_track(vid: str = "dQw4w9WgXcQ", duration: float = 180.0) -> Track:
    return Track(
        video_id=vid,
        title=f"Track {vid}",
        artist="Test Artist",
        duration_sec=duration,
        thumbnail="",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_playback_position_math(tmp_path: Path) -> None:
    room = SyncRoom(audio_cache=AudioCache(cache_dir=tmp_path))
    pb = room.playback
    pb.status = "playing"
    pb.anchor_position_sec = 10.0
    pb.anchor_server_ms = 5000.0
    pb.playback_rate = 1.0

    assert room.get_current_position_sec(now_ms=4800.0) == pytest.approx(10.0)
    assert room.get_current_position_sec(now_ms=7500.0) == pytest.approx(12.5)

    pb.status = "paused"
    assert room.get_current_position_sec(now_ms=15000.0) == pytest.approx(10.0)


@pytest.mark.anyio
async def test_speaker_registration_update_and_auto_stereo(tmp_path: Path) -> None:
    room = SyncRoom(audio_cache=AudioCache(cache_dir=tmp_path), owner_user_id="u_host")
    ws1, ws2, ws3 = FakeWebSocket(), FakeWebSocket(), FakeWebSocket()

    await room.register_speaker("host1", ws1, "Host PC", "desktop", user_id="u_host")  # type: ignore[arg-type]
    await room.register_speaker("sp2", ws2, "Phone A", "mobile", user_id="u_2")  # type: ignore[arg-type]
    await room.register_speaker("sp3", ws3, "Phone B", "mobile", user_id="u_3")  # type: ignore[arg-type]

    assert ws1.sent[0]["type"] == "welcome"
    assert ws1.sent[0]["is_leader"] is True
    assert ws2.sent[0]["is_leader"] is False

    await room.update_speaker("sp2", "sp2", {"offset_ms": 999, "volume": 1.8, "audio_unlocked": True})
    assert room.speakers["sp2"].state.offset_ms == 500.0
    assert room.speakers["sp2"].state.volume == 1.0
    assert room.speakers["sp2"].state.sync_status == "idle"

    await room.auto_assign_stereo("host1")
    assert room.speakers["host1"].state.role == "left"
    assert room.speakers["sp2"].state.role == "right"
    assert room.speakers["sp3"].state.role == "mono"


@pytest.mark.anyio
async def test_guest_control_enforcement(tmp_path: Path) -> None:
    room = SyncRoom(audio_cache=AudioCache(cache_dir=tmp_path), owner_user_id="u_host")
    ws_host, ws_guest = FakeWebSocket(), FakeWebSocket()
    await room.register_speaker("host", ws_host, "Host", "desktop", user_id="u_host")  # type: ignore[arg-type]
    await room.register_speaker("guest", ws_guest, "Guest", "mobile", user_id="u_guest")  # type: ignore[arg-type]

    # Leader Mode is strict by default
    assert room.allow_guest_control is False
    assert room.can_control_room("guest") is False

    await room.add_to_queue("guest", [make_track("track_aaaa1")], play_if_idle=False)
    assert len(room.queue) == 0
    await room.update_speaker("guest", "host", {"muted": True})
    assert room.speakers["host"].state.muted is False

    # Non-leader CAN update its own local offset/role/mute
    await room.update_speaker("guest", "guest", {"offset_ms": -45, "role": "right", "muted": True})
    assert room.speakers["guest"].state.offset_ms == -45
    assert room.speakers["guest"].state.role == "right"
    assert room.speakers["guest"].state.muted is True


@pytest.mark.anyio
async def test_ready_barrier_queue_transport_and_calibration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = AudioCache(cache_dir=tmp_path)
    room = SyncRoom(audio_cache=cache, owner_user_id="u_host")

    async def fake_ensure_track(video_id: str) -> dict[str, Any]:
        return {
            "video_id": video_id,
            "path": tmp_path / f"{video_id}.m4a",
            "mime_type": "audio/mp4",
            "duration_sec": 200.0,
        }

    monkeypatch.setattr(cache, "ensure_track", fake_ensure_track)

    ws_host, ws_guest = FakeWebSocket(), FakeWebSocket()
    await room.register_speaker("host", ws_host, "Host", "desktop", user_id="u_host")  # type: ignore[arg-type]
    await room.register_speaker("guest", ws_guest, "Phone", "mobile", user_id="u_guest")  # type: ignore[arg-type]
    await room.update_speaker("host", "host", {"audio_unlocked": True})
    await room.update_speaker("guest", "guest", {"audio_unlocked": True})

    t1 = make_track("vid00000001", 200.0)
    t2 = make_track("vid00000002", 210.0)
    await room.add_to_queue("host", [t1, t2], play_if_idle=True)

    await asyncio.sleep(0.05)
    gen = room.playback.generation
    assert room.playback.audio_ready is True
    assert room.playback.status == "loading"

    room.mark_track_ready("host", gen)
    room.mark_track_ready("guest", gen)
    await asyncio.sleep(0.05)

    assert room.playback.status == "playing"
    assert room.playback.anchor_server_ms > server_now_ms() - 500

    await room.pause("host")
    assert room.playback.status == "paused"

    await room.seek("host", 42.0)
    assert room.playback.anchor_position_sec == pytest.approx(42.0)

    await room.resume("host")
    assert room.playback.status == "playing"

    prev_gen = room.playback.generation
    await room.force_resync("host")
    assert room.playback.generation == prev_gen + 1

    await room.set_calibration("host", True)
    assert room.calibration.active is True
    assert room.playback.status == "paused"

    await room.set_repeat("host", "one")
    await room.next_track("host", auto=True)
    await asyncio.sleep(0.05)
    assert room.playback.queue_index == 0

    await room.move_queue_item("host", 0, 1)
    assert room.playback.queue_index == 1

    await room.clear_queue("host")
    assert len(room.queue) == 0
    assert room.playback.status == "stopped"


@pytest.mark.anyio
async def test_yt_iframe_circuit_breaker_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When yt-dlp fails on YouTube tracks, SyncRoom falls back to use_yt_iframe & opens circuit breaker."""
    import asyncio
    from backend.audio_cache import AudioCache
    from backend.config import server_now_ms
    from backend.models import Track
    from backend.sync_room import SyncRoom

    class _FakeWS:
        async def send_json(self, payload: dict) -> None:
            return None

    cache = AudioCache(cache_dir=tmp_path)

    async def _fail_ensure(*args, **kwargs):
        raise RuntimeError("Failed to extract any player response")

    monkeypatch.setattr(cache, "ensure_track", _fail_ensure)
    monkeypatch.setattr(cache, "prefetch", lambda *args, **kwargs: None)

    room = SyncRoom(audio_cache=cache, room_code="FALLBK")
    await room.register_speaker("host", _FakeWS(), "Host", "desktop", is_host=True)  # type: ignore[arg-type]
    room.queue.append(Track(video_id="jTAvdBi2jz4", title="Blocked Song", artist="Artist", duration_sec=210.0))
    room.queue.append(Track(video_id="gkc6NcgVCNk", title="Second Song", artist="Artist", duration_sec=200.0))

    await room.play_queue_index(0)
    if room._load_task is not None:
        await room._load_task

    assert room.playback.use_yt_iframe is True
    assert room.playback.audio_ready is True
    assert room.playback.audio_url == "yt:jTAvdBi2jz4"
    assert room.playback.status == "playing"
    assert room._ytdlp_blocked_until > server_now_ms()

    # Second track skips blocked yt-dlp immediately without calling ensure_track again
    called_second = False

    async def _fail_if_called(*args, **kwargs):
        nonlocal called_second
        called_second = True
        raise RuntimeError("should not run while circuit breaker is open")

    monkeypatch.setattr(cache, "ensure_track", _fail_if_called)
    await room.play_queue_index(1)
    if room._load_task is not None:
        await room._load_task

    assert called_second is False
    assert room.playback.use_yt_iframe is True
    assert room.playback.audio_url == "yt:gkc6NcgVCNk"
    assert room.playback.status == "playing"

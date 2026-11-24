"""Unit tests for YouTube Music URL parsing and AudioCache validation/dedup/LRU."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from backend.audio_cache import AudioCache, validate_video_id
from backend.ytmusic_service import parse_duration_text, parse_youtube_url


def test_parse_youtube_url_variants() -> None:
    assert parse_youtube_url("dQw4w9WgXcQ") == {
        "type": "video",
        "video_id": "dQw4w9WgXcQ",
        "playlist_id": None,
    }
    assert parse_youtube_url("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVMdQw4w9WgXcQ") == {
        "type": "video",
        "video_id": "dQw4w9WgXcQ",
        "playlist_id": "RDAMVMdQw4w9WgXcQ",
    }
    assert parse_youtube_url("https://youtu.be/9bZkp7q19f0?si=xyz") == {
        "type": "video",
        "video_id": "9bZkp7q19f0",
        "playlist_id": None,
    }
    assert parse_youtube_url("https://www.youtube.com/shorts/abcdefghijk") == {
        "type": "video",
        "video_id": "abcdefghijk",
        "playlist_id": None,
    }
    assert parse_youtube_url("https://music.youtube.com/playlist?list=PL1234567890") == {
        "type": "playlist",
        "video_id": None,
        "playlist_id": "PL1234567890",
    }
    assert parse_youtube_url("https://music.youtube.com/browse/MPREb_123456") == {
        "type": "album",
        "browse_id": "MPREb_123456",
        "video_id": None,
        "playlist_id": None,
    }
    assert parse_youtube_url("daft punk get lucky") is None


def test_parse_duration_text() -> None:
    assert parse_duration_text("3:45") == 225.0
    assert parse_duration_text("1:02:10") == 3730.0
    assert parse_duration_text(120) == 120.0
    assert parse_duration_text("invalid") == 0.0


def test_validate_video_id_rejects_traversal() -> None:
    assert validate_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    with pytest.raises(ValueError):
        validate_video_id("../etc/passwd")
    with pytest.raises(ValueError):
        validate_video_id("short")
    with pytest.raises(ValueError):
        validate_video_id("bad/slash123")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_audio_cache_deduplication_and_lru(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = AudioCache(cache_dir=tmp_path, max_files=2, max_bytes=10_000)
    calls = 0

    def fake_download(video_id: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        audio_file = tmp_path / f"{video_id}.m4a"
        audio_file.write_bytes(b"FAKE_M4A_" + video_id.encode("ascii"))
        return {
            "video_id": video_id,
            "path": audio_file,
            "mime_type": "audio/mp4",
            "duration_sec": 123.0,
        }

    monkeypatch.setattr(cache, "_download_sync", fake_download)

    # Concurrent calls for the same video_id must only trigger 1 download
    r1, r2, r3 = await asyncio.gather(
        cache.ensure_track("track000001"),
        cache.ensure_track("track000001"),
        cache.ensure_track("track000001"),
    )
    assert calls == 1
    assert r1["path"] == r2["path"] == r3["path"]

    # Create 3 files with explicit mtimes and trigger LRU prune (max_files=2)
    f1 = tmp_path / "old_track1.m4a"
    f2 = tmp_path / "mid_track2.m4a"
    f3 = tmp_path / "new_track3.m4a"
    f1.write_bytes(b"1" * 100)
    f2.write_bytes(b"2" * 100)
    f3.write_bytes(b"3" * 100)

    now = time.time()
    os.utime(tmp_path / "track000001.m4a", (now - 400, now - 400))
    os.utime(f1, (now - 300, now - 300))
    os.utime(f2, (now - 200, now - 200))
    os.utime(f3, (now - 100, now - 100))

    cache._prune_cache(protected_id="new_track3")
    remaining = sorted(p.name for p in tmp_path.glob("*.m4a"))
    assert remaining == ["mid_track2.m4a", "new_track3.m4a"]

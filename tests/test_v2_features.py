"""Tests for SoundMesh v2 features: Postgres SQL translation, Bearer auth, uploads, upvotes, reactions, lyrics, PWA."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend import main as app_main
from backend.audio_cache import AudioCache
from backend.auth_service import AuthService
from backend.db import Database, normalize_postgres_url
from backend.models import Track
from backend.room_manager import RoomManager
from backend.sync_room import SyncRoom


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(db_path=tmp_path / "v2.db", database_url="")
    auth = AuthService(db=db)
    cache = AudioCache(cache_dir=tmp_path / "cache")
    rm = RoomManager(db=db, audio_cache=cache)
    monkeypatch.setattr(app_main, "db", db)
    monkeypatch.setattr(app_main, "auth_service", auth)
    monkeypatch.setattr(app_main, "audio_cache", cache)
    monkeypatch.setattr(app_main, "room_manager", rm)
    return db


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_postgres_url_normalization_and_sql_translation(tmp_path: Path) -> None:
    norm = normalize_postgres_url("postgres://user:pass@aws-pooler.supabase.com:5432/postgres")
    assert norm.startswith("postgresql://")
    assert "sslmode=require" in norm

    db = Database(db_path=tmp_path / "local.db", database_url="")
    assert db._sql("SELECT * FROM users WHERE id=?") == "SELECT * FROM users WHERE id=?"
    db.is_postgres = True
    assert db._sql("SELECT * FROM users WHERE id=? AND provider=?") == "SELECT * FROM users WHERE id=%s AND provider=%s"
    db.is_postgres = False
    db.close()


def test_bearer_auth_presets_upload_lyrics_and_pwa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)

    async def fake_lyrics(video_id: str) -> dict[str, Any]:
        return {"video_id": video_id, "lyrics": "La la la", "source": "Test", "available": True}

    monkeypatch.setattr(app_main.ytmusic_service, "get_lyrics", fake_lyrics)

    try:
        with TestClient(app_main.app) as client:
            # PWA manifest, SW, and icon
            assert client.get("/manifest.json").status_code == 200
            assert client.get("/sw.js").status_code == 200
            assert client.get("/icons/icon.svg").status_code == 200

            # Guest auth returns session_token for decoupled Bearer usage
            r_auth = client.post("/api/auth/guest", json={"name": "Tilak"})
            assert r_auth.status_code == 200
            token = r_auth.json()["session_token"]
            assert token

            # Clear cookies and authenticate purely via Bearer header
            client.cookies.clear()
            headers = {"Authorization": f"Bearer {token}"}
            me = client.get("/api/auth/me", headers=headers).json()["user"]
            assert me["name"] == "Tilak"

            # Save and fetch speaker preset
            r_put = client.put(
                "/api/me/preset/dev_123",
                headers=headers,
                json={"name": "Desk", "role": "left", "offset_ms": -25, "eq_preset": "party_bass", "pos_x": -0.5, "pos_y": -0.4},
            )
            assert r_put.status_code == 200
            r_get = client.get("/api/me/preset/dev_123", headers=headers)
            assert r_get.json()["preset"]["eq_preset"] == "party_bass"
            assert r_get.json()["preset"]["offset_ms"] == -25

            # Create room and upload local MP3
            code = client.post("/api/rooms", headers=headers, json={"name": "Upload Room"}).json()["room"]["code"]
            r_up = client.post(
                f"/api/rooms/{code}/upload",
                headers=headers,
                files={"file": ("beat.mp3", b"ID3FAKEAUDIOBYTES123456", "audio/mpeg")},
            )
            assert r_up.status_code == 200
            uploaded = r_up.json()["track"]
            assert uploaded["video_id"].startswith("local_")
            assert uploaded["source"] == "local"

            # Serve uploaded file via byte-range endpoint
            r_range = client.get(f"/api/audio/{uploaded['video_id']}", headers={"Range": "bytes=0-4"})
            assert r_range.status_code == 206
            assert r_range.content == b"ID3FA"

            # Lyrics endpoint
            r_lyr = client.get("/api/lyrics/dQw4w9WgXcQ")
            assert r_lyr.status_code == 200
            assert r_lyr.json()["available"] is True
            assert r_lyr.json()["lyrics"] == "La la la"
    finally:
        db.close()


@pytest.mark.anyio
async def test_upvote_jukebox_spatial_eq_and_reactions(tmp_path: Path) -> None:
    cache = AudioCache(cache_dir=tmp_path / "cache")
    room = SyncRoom(audio_cache=cache, room_code="JUKE", owner_user_id="u1", owner_name="Tilak")
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    await room.register_speaker("sp1", ws1, "Host", "desktop", user_id="u1", user_name="Tilak")  # type: ignore[arg-type]
    await room.register_speaker("sp2", ws2, "Phone", "mobile", user_id="u2", user_name="Friend")  # type: ignore[arg-type]

    # Set queue: index 0 playing, index 1 and 2 upcoming
    t0 = Track(video_id="vid00000000", title="Now Playing")
    t1 = Track(video_id="vid00000001", title="Song A")
    t2 = Track(video_id="vid00000002", title="Song B")
    room.queue = [t0, t1, t2]
    room.playback.queue_index = 0

    # Upvote Song B (index 2) -> moves ahead of Song A (index 1)
    assert await room.upvote_queue_item("sp2", 2) is True
    assert room.queue[1].video_id == "vid00000002"
    assert "u2" in room.queue[1].votes

    # Cannot upvote currently playing song (index 0)
    assert await room.upvote_queue_item("sp2", 0) is False

    # Spatial & EQ update clamping
    await room.update_speaker(
        "sp2",
        "sp2",
        {"pos_x": -1.5, "pos_y": 0.4, "spatial_enabled": True, "eq_preset": "hostel_boost", "eq_bands": [20, 3, 0, -2, 5]},
    )
    sp2 = room.speakers["sp2"].state
    assert sp2.pos_x == -1.0
    assert sp2.spatial_enabled is True
    assert sp2.eq_preset == "hostel_boost"
    assert sp2.eq_bands == [12.0, 3.0, 0.0, -2.0, 5.0]

    # Emoji reactions whitelist
    assert await room.send_reaction("sp2", "🔥") is True
    assert await room.send_reaction("sp2", "BAD_EMOJI") is False
    assert any(m.get("type") == "reaction" and m.get("emoji") == "🔥" for m in ws1.sent)

    # Acoustic chirp trigger
    assert await room.trigger_acoustic_chirp("sp1", "sp2") is True
    assert any(m.get("type") == "acoustic_chirp" and m.get("emitter_id") == "sp2" for m in ws2.sent)


def test_cross_origin_oauth_redirect_with_frontend_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "FRONTEND_URL", "https://soundmesh.vercel.app")

    async def fake_callback(code: str, state: str):
        u = db.upsert_user("g_77", "google", "Tilak")
        tok = db.create_session("g_77")
        return u, tok, "/room/NOVA42"

    monkeypatch.setattr(app_main.auth_service, "handle_google_callback", fake_callback)
    try:
        with TestClient(app_main.app) as client:
            r = client.get("/api/auth/google/callback?code=abc&state=xyz", follow_redirects=False)
            assert r.status_code == 302
            loc = r.headers["location"]
            assert loc.startswith("https://soundmesh.vercel.app/room/NOVA42#auth_token=")
    finally:
        db.close()

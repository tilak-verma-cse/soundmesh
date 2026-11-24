"""Tests for multi-room lifecycle, Leader delegation, requests, and auto-failover."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import main as app_main
from backend.audio_cache import AudioCache
from backend.auth_service import AuthService
from backend.db import Database
from backend.models import Track
from backend.room_manager import RoomManager
from backend.sync_room import SyncRoom


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_dynamic_leader_assign_request_and_failover(tmp_path: Path) -> None:
    cache = AudioCache(cache_dir=tmp_path)
    room = SyncRoom(
        audio_cache=cache,
        room_code="PARTY1",
        room_name="Party Room",
        owner_user_id="owner_u1",
        owner_name="Alex",
    )

    ws_owner = FakeWebSocket()
    ws_dj = FakeWebSocket()
    ws_guest = FakeWebSocket()

    # Owner joins first -> becomes Owner + initial Leader
    await room.register_speaker(
        "dev_owner", ws_owner, "Alex Laptop", "desktop", user_id="owner_u1", user_name="Alex"  # type: ignore[arg-type]
    )
    assert room.speakers["dev_owner"].state.is_leader is True
    assert room.speakers["dev_owner"].state.is_owner is True

    # Guest & DJ join -> NOT Leader by default, cannot pause/play
    await room.register_speaker(
        "dev_dj", ws_dj, "Maya Phone", "mobile", user_id="u_maya", user_name="Maya"  # type: ignore[arg-type]
    )
    await room.register_speaker(
        "dev_guest", ws_guest, "Sam Phone", "mobile", user_id="u_sam", user_name="Sam"  # type: ignore[arg-type]
    )
    assert room.can_control_room("dev_dj") is False
    assert room.can_control_room("dev_guest") is False

    # Owner passes Leader to Maya's phone while playback state stays intact
    room.playback.status = "playing"
    ok = await room.assign_leader("dev_owner", "dev_dj")
    assert ok is True
    assert room.leader_speaker_id == "dev_dj"
    assert room.speakers["dev_dj"].state.is_leader is True
    assert room.speakers["dev_owner"].state.is_leader is False
    assert room.playback.status == "playing"
    assert room.can_control_room("dev_dj") is True
    assert room.can_control_room("dev_owner") is False
    # Room Owner can still reassign leadership even when not current Leader
    assert room.can_assign_leader("dev_owner") is True

    # Sam requests leadership -> Maya approves
    assert await room.request_leader("dev_guest") is True
    assert "dev_guest" in room.pending_leader_requests
    assert await room.respond_leader_request("dev_dj", "dev_guest", approve=True) is True
    assert room.leader_speaker_id == "dev_guest"
    assert "dev_guest" not in room.pending_leader_requests

    # Sam disconnects -> auto-failover prefers Room Owner (Alex)
    await room.unregister_speaker("dev_guest")
    assert room.leader_speaker_id == "dev_owner"
    assert any(m.get("type") == "leader_changed" and m.get("reason") == "auto_handoff" for m in ws_owner.sent)


@pytest.mark.anyio
async def test_collab_queue_permission(tmp_path: Path) -> None:
    cache = AudioCache(cache_dir=tmp_path)
    room = SyncRoom(audio_cache=cache, room_code="COLLAB", owner_user_id="u1", owner_name="Alex")
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    await room.register_speaker("sp1", ws1, "Host", "desktop", user_id="u1", user_name="Alex")  # type: ignore[arg-type]
    await room.register_speaker("sp2", ws2, "Guest", "mobile", user_id="u2", user_name="Guest")  # type: ignore[arg-type]

    t = Track(video_id="vid_collab01", title="Collab Song", artist="Artist")
    await room.add_to_queue("sp2", [t])
    assert len(room.queue) == 0  # Blocked when allow_collab_queue is False

    await room.set_allow_collab_queue("sp1", True)
    await room.add_to_queue("sp2", [t])
    assert len(room.queue) == 1


def test_rooms_rest_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(db_path=tmp_path / "rooms.db")
    auth = AuthService(db=db)
    cache = AudioCache(cache_dir=tmp_path / "cache")
    rm = RoomManager(db=db, audio_cache=cache)
    monkeypatch.setattr(app_main, "db", db)
    monkeypatch.setattr(app_main, "auth_service", auth)
    monkeypatch.setattr(app_main, "audio_cache", cache)
    monkeypatch.setattr(app_main, "room_manager", rm)

    try:
        with TestClient(app_main.app) as client:
            # Must sign in before creating room
            assert client.post("/api/rooms", json={"name": "NoAuth"}).status_code == 401

            client.post("/api/auth/guest", json={"name": "OwnerUser"})
            r_create = client.post(
                "/api/rooms",
                json={"name": "Lounge", "is_public": True, "passcode": "1234"},
            )
            assert r_create.status_code == 200
            room_obj = r_create.json()["room"]
            code = room_obj["code"]
            assert room_obj["has_passcode"] is True

            assert client.post(f"/api/rooms/{code}/verify", json={"passcode": "bad"}).json()["ok"] is False
            assert client.post(f"/api/rooms/{code}/verify", json={"passcode": "1234"}).json()["ok"] is True

            r_list = client.get("/api/rooms").json()
            assert any(r["code"] == code for r in r_list["public_rooms"])
            assert any(r["code"] == code for r in r_list["my_rooms"])

            r_info = client.get(f"/api/rooms/{code}/info").json()
            assert f"/room/{code}" in r_info["join_url"]
            assert r_info["qr_data_uri"].startswith("data:image/png;base64,")

            assert client.delete(f"/api/rooms/{code}").status_code == 200
            assert client.get(f"/api/rooms/{code}").status_code == 404
    finally:
        db.close()

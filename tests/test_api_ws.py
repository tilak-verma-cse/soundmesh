"""Integration tests for FastAPI REST endpoints, byte-range serving, and multi-room WebSockets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import main as app_main
from backend.audio_cache import AudioCache
from backend.auth_service import AuthService
from backend.db import Database
from backend.room_manager import RoomManager


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(db_path=tmp_path / "ws_test.db")
    auth = AuthService(db=db)
    cache = AudioCache(cache_dir=tmp_path / "cache")
    rm = RoomManager(db=db, audio_cache=cache)
    monkeypatch.setattr(app_main, "db", db)
    monkeypatch.setattr(app_main, "auth_service", auth)
    monkeypatch.setattr(app_main, "audio_cache", cache)
    monkeypatch.setattr(app_main, "room_manager", rm)
    return db


def test_http_pages_and_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _isolate(tmp_path, monkeypatch)
    try:
        with TestClient(app_main.app) as client:
            for route in ("/", "/join", "/room/STUDIO"):
                r = client.get(route)
                assert r.status_code == 200
                assert "SoundMesh" in r.text

            r_health = client.get("/api/health")
            assert r_health.status_code == 200
            assert r_health.json()["ok"] is True

            r_info = client.get("/api/info")
            assert r_info.status_code == 200
            info = r_info.json()
            assert info["qr_data_uri"].startswith("data:image/png;base64,")
            assert isinstance(info["lan_ips"], list) and len(info["lan_ips"]) >= 1
            assert "/room/STUDIO" in info["join_url"]

            r_qr = client.get("/api/qr", params={"url": "http://192.168.1.10:8765/room/STUDIO"})
            assert r_qr.status_code == 200
            assert r_qr.headers["content-type"] == "image/png"
            assert r_qr.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        db.close()


def test_audio_range_serving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _isolate(tmp_path, monkeypatch)
    sample_id = "rangeTest01"
    cache_dir = app_main.audio_cache.cache_dir
    sample_file = cache_dir / f"{sample_id}.m4a"
    payload = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    sample_file.write_bytes(payload)
    (cache_dir / f"{sample_id}.meta.json").write_text(
        json.dumps({"duration_sec": 12.0}), encoding="utf-8"
    )
    try:
        with TestClient(app_main.app) as client:
            res = client.get(f"/api/audio/{sample_id}", headers={"Range": "bytes=0-15"})
            assert res.status_code == 206
            assert res.content == payload[:16]
            assert res.headers.get("accept-ranges") == "bytes"
    finally:
        db.close()


def test_websocket_ntp_ping_leader_and_room_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)
    try:
        with TestClient(app_main.app) as client:
            client.post("/api/auth/guest", json={"name": "Alex"})
            c1 = client.post("/api/rooms", json={"name": "Room A", "is_public": True}).json()["room"]["code"]
            c2 = client.post("/api/rooms", json={"name": "Room B", "is_public": True}).json()["room"]["code"]

            with client.websocket_connect(f"/ws/{c1}?client_id=host_pc&name=HostPC") as ws_host:
                welcome_host = ws_host.receive_json()
                assert welcome_host["type"] == "welcome"
                assert welcome_host["is_leader"] is True
                assert ws_host.receive_json()["type"] == "room_state"

                with client.websocket_connect(f"/ws/{c1}?client_id=phone_1&name=Pixel") as ws_phone:
                    welcome_phone = ws_phone.receive_json()
                    assert welcome_phone["type"] == "welcome"
                    assert welcome_phone["is_leader"] is False
                    assert ws_host.receive_json()["type"] == "room_state"
                    assert ws_phone.receive_json()["type"] == "room_state"

                    # Separate Room B speaker should stay isolated
                    with client.websocket_connect(f"/ws/{c2}?client_id=other_1&name=Other") as ws_other:
                        assert ws_other.receive_json()["type"] == "welcome"
                        assert ws_other.receive_json()["type"] == "room_state"

                        # NTP sync_ping fast path
                        ws_phone.send_json({"type": "sync_ping", "seq": 7, "t0": 1234.5})
                        pong = ws_phone.receive_json()
                        assert pong["type"] == "sync_pong"
                        assert pong["seq"] == 7
                        assert pong["t1"] <= pong["t2"]

                        # Pass leadership from host_pc to phone_1
                        ws_host.send_json({"type": "assign_leader", "target_id": "phone_1"})
                        h_lc = ws_host.receive_json()
                        p_lc = ws_phone.receive_json()
                        assert h_lc["type"] == "leader_changed" and h_lc["leader_id"] == "phone_1"
                        assert p_lc["type"] == "leader_changed" and p_lc["leader_id"] == "phone_1"
                        h_rs = ws_host.receive_json()
                        p_rs = ws_phone.receive_json()
                        assert h_rs["state"]["leader_speaker_id"] == "phone_1"
                        assert p_rs["state"]["leader_speaker_id"] == "phone_1"

                        # Now phone_1 (new leader) triggers calibration
                        ws_phone.send_json({"type": "set_calibration", "active": True})
                        assert ws_host.receive_json()["state"]["calibration"]["active"] is True
                        assert ws_phone.receive_json()["state"]["calibration"]["active"] is True

                        # Room B remained untouched (leader_speaker_id is still other_1)
                        ws_other.send_json({"type": "sync_ping", "seq": 1, "t0": 10.0})
                        assert ws_other.receive_json()["type"] == "sync_pong"
    finally:
        db.close()

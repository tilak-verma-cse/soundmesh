"""Tests for free hosting helpers: cookies decoding, runtime public URL QR, and tunnel security."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend import main as app_main
from backend.audio_cache import AudioCache
from backend.auth_service import AuthService
from backend.db import Database
from backend.room_manager import RoomManager


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(db_path=tmp_path / "hosting_test.db")
    auth = AuthService(db=db)
    cache = AudioCache(cache_dir=tmp_path / "cache")
    rm = RoomManager(db=db, audio_cache=cache)
    monkeypatch.setattr(app_main, "db", db)
    monkeypatch.setattr(app_main, "auth_service", auth)
    monkeypatch.setattr(app_main, "audio_cache", cache)
    monkeypatch.setattr(app_main, "room_manager", rm)
    return db


def test_ytdlp_cookies_b64_decoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sample = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tval\n"
    encoded = base64.b64encode(sample.encode("utf-8")).decode("ascii")
    monkeypatch.setenv("YTDLP_COOKIES_B64", encoded)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")

    cookie_path = config.get_ytdlp_cookiefile()
    assert cookie_path is not None
    assert Path(cookie_path).read_text(encoding="utf-8") == sample


def test_runtime_public_url_updates_room_qr_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    config.set_runtime_public_url("https://demo-sound.trycloudflare.com")
    try:
        with TestClient(app_main.app) as client:
            client.post("/api/auth/guest", json={"name": "Host"})
            code = client.post("/api/rooms", json={"name": "Live Room"}).json()["room"]["code"]
            info = client.get(f"/api/rooms/{code}/info").json()
            assert info["public_url"] == "https://demo-sound.trycloudflare.com"
            assert info["join_url"] == f"https://demo-sound.trycloudflare.com/room/{code}"
    finally:
        config.set_runtime_public_url("")
        db.close()


def test_tunnel_api_blocks_remote_proxies_and_allows_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)

    async def fake_start(port: int) -> dict:
        config.set_runtime_public_url("https://live-test.trycloudflare.com")
        return {
            "active": True,
            "public_url": "https://live-test.trycloudflare.com",
            "installed": True,
            "starting": False,
            "error": "",
        }

    async def fake_stop() -> dict:
        config.set_runtime_public_url("")
        return {
            "active": False,
            "public_url": "",
            "installed": True,
            "starting": False,
            "error": "",
        }

    monkeypatch.setattr(app_main.tunnel_manager, "start", fake_start)
    monkeypatch.setattr(app_main.tunnel_manager, "stop", fake_stop)

    try:
        with TestClient(app_main.app) as client:
            # Remote forwarded request is blocked from controlling local tunnel
            r_remote = client.post(
                "/api/tunnel/start",
                headers={"X-Forwarded-For": "203.0.113.99"},
            )
            assert r_remote.status_code == 403

            r_cf = client.post(
                "/api/tunnel/stop",
                headers={"CF-Connecting-IP": "203.0.113.99"},
            )
            assert r_cf.status_code == 403

            # Direct local request succeeds
            st0 = client.get("/api/tunnel/status").json()
            assert st0["can_control"] is True

            st1 = client.post("/api/tunnel/start").json()
            assert st1["active"] is True
            assert st1["public_url"] == "https://live-test.trycloudflare.com"

            st2 = client.post("/api/tunnel/stop").json()
            assert st2["active"] is False
    finally:
        config.set_runtime_public_url("")
        db.close()

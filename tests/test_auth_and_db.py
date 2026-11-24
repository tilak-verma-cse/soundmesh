"""Tests for SQLite Database, Guest auth, Google OAuth PKCE callback, and Personal YT Library."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import config
from backend import main as app_main
from backend.audio_cache import AudioCache
from backend.auth_service import AuthService, parse_iso8601_duration
from backend.db import Database
from backend.room_manager import RoomManager


def _use_isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    db = Database(db_path=tmp_path / "test.db")
    auth = AuthService(db=db)
    cache = AudioCache(cache_dir=tmp_path / "cache")
    rm = RoomManager(db=db, audio_cache=cache)
    monkeypatch.setattr(app_main, "db", db)
    monkeypatch.setattr(app_main, "auth_service", auth)
    monkeypatch.setattr(app_main, "audio_cache", cache)
    monkeypatch.setattr(app_main, "room_manager", rm)
    return db


def test_parse_iso8601_duration() -> None:
    assert parse_iso8601_duration("PT3M45S") == 225.0
    assert parse_iso8601_duration("PT1H2M3S") == 3723.0
    assert parse_iso8601_duration("PT58S") == 58.0
    assert parse_iso8601_duration("INVALID") == 0.0


def test_sanitize_next_url() -> None:
    assert AuthService.sanitize_next_url("/room/NOVA42") == "/room/NOVA42"
    assert AuthService.sanitize_next_url("/") == "/"
    assert AuthService.sanitize_next_url("//evil.com/phish") == "/"
    assert AuthService.sanitize_next_url("https://evil.com") == "/"
    assert AuthService.sanitize_next_url("/\\evil") == "/"


def test_db_users_sessions_and_room_passcode(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "db.sqlite")
    user = db.upsert_user(
        user_id="g_123",
        provider="google",
        name="Alex",
        email="alex@example.com",
        google_access_token="secret_access",
        google_refresh_token="secret_refresh",
        has_youtube_scope=True,
    )
    assert "google_access_token" not in user
    assert "google_refresh_token" not in user
    assert user["has_youtube_scope"] is True

    token = db.create_session("g_123")
    resolved = db.get_user_by_session_token(token)
    assert resolved is not None and resolved["id"] == "g_123"
    assert "google_access_token" not in resolved

    db.delete_session(token)
    assert db.get_user_by_session_token(token) is None

    db.create_room_record("NOVA42", "Chill Room", "g_123", "Alex", is_public=False, passcode="jazz99")
    assert db.verify_room_passcode("NOVA42", "jazz99") is True
    assert db.verify_room_passcode("NOVA42", "wrong") is False
    db.close()


def test_guest_login_me_and_logout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _use_isolated_state(tmp_path, monkeypatch)
    try:
        with TestClient(app_main.app) as client:
            r0 = client.get("/api/auth/me")
            assert r0.status_code == 200
            assert r0.json()["user"] is None

            r_login = client.post("/api/auth/guest", json={"name": "Maya"})
            assert r_login.status_code == 200
            assert r_login.json()["user"]["name"] == "Maya"
            assert r_login.json()["user"]["provider"] == "guest"

            r1 = client.get("/api/auth/me")
            assert r1.json()["user"]["name"] == "Maya"

            r_out = client.post("/api/auth/logout")
            assert r_out.status_code == 200
            assert client.get("/api/auth/me").json()["user"] is None
    finally:
        db.close()


def test_google_oauth_pkce_flow_and_personal_youtube_playlists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _use_isolated_state(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(config, "GOOGLE_ENABLE_YOUTUBE_SCOPE", True)

    class MockAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "MockAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, url: str, data: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
            assert "oauth2.googleapis.com/token" in url
            return httpx.Response(
                200,
                json={
                    "access_token": "mock_access_tok",
                    "refresh_token": "mock_refresh_tok",
                    "expires_in": 3600,
                    "scope": "openid email profile https://www.googleapis.com/auth/youtube.readonly",
                },
            )

        async def get(
            self,
            url: str,
            params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            **kwargs: Any,
        ) -> httpx.Response:
            if "userinfo" in url:
                return httpx.Response(
                    200,
                    json={
                        "sub": "998877",
                        "email": "maya@gmail.com",
                        "name": "Maya Lin",
                        "picture": "https://example.com/avatar.jpg",
                    },
                )
            if url.endswith("/playlists"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "PL_MY_JAMS",
                                "snippet": {"title": "Late Night Synth", "channelTitle": "Maya Lin"},
                                "contentDetails": {"itemCount": 12},
                            }
                        ]
                    },
                )
            if url.endswith("/videos") and (params or {}).get("myRating") == "like":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "dQw4w9WgXcQ",
                                "snippet": {"title": "Liked Synth Track", "channelTitle": "Synth Artist"},
                                "contentDetails": {"duration": "PT3M32S"},
                            }
                        ]
                    },
                )
            return httpx.Response(404, json={})

    monkeypatch.setattr("backend.auth_service.httpx.AsyncClient", MockAsyncClient)

    try:
        with TestClient(app_main.app) as client:
            # Unauthenticated -> 401 on personal library
            assert client.get("/api/me/playlists").status_code == 401

            # Start OAuth login with next=/room/NOVA42
            r_login = client.get(
                "/api/auth/google/login?next=/room/NOVA42&include_youtube=1",
                follow_redirects=False,
            )
            assert r_login.status_code == 302
            loc = r_login.headers["location"]
            qs = parse_qs(urlparse(loc).query)
            assert qs["code_challenge_method"] == ["S256"]
            assert "youtube.readonly" in qs["scope"][0]
            state = qs["state"][0]

            # Complete OAuth callback
            r_cb = client.get(
                f"/api/auth/google/callback?code=authcode123&state={state}",
                follow_redirects=False,
            )
            assert r_cb.status_code == 302
            assert r_cb.headers["location"] == "/room/NOVA42"

            # Session authenticated and tokens NOT leaked
            me = client.get("/api/auth/me").json()["user"]
            assert me["provider"] == "google"
            assert me["name"] == "Maya Lin"
            assert me["has_youtube_scope"] is True
            assert "google_access_token" not in me

            # Personal YouTube playlists + Liked Music
            r_pl = client.get("/api/me/playlists")
            assert r_pl.status_code == 200
            pls = r_pl.json()["playlists"]
            assert any(p["id"] == "LL" for p in pls)
            assert any(p["id"] == "PL_MY_JAMS" for p in pls)

            r_liked = client.get("/api/me/playlists/LL/tracks")
            assert r_liked.status_code == 200
            tracks = r_liked.json()["tracks"]
            assert tracks[0]["video_id"] == "dQw4w9WgXcQ"
            assert tracks[0]["duration_sec"] == 212.0
    finally:
        db.close()

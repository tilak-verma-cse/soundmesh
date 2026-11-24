"""Security regression tests covering C-1..C-3, H-1..H-5, M-1..M-5, and L-1..L-3."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import config
from backend import main as app_main
from backend.audio_cache import AudioCache
from backend.auth_service import AuthService
from backend.db import Database, _verify_passcode_hash
from backend.models import Track
from backend.room_manager import RoomManager
from backend.security import decrypt_secret, encrypt_secret
from backend.tunnel_manager import TunnelManager


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    monkeypatch.setenv("SOUNDMESH_SECRET_KEY", "a" * 64)
    db = Database(db_path=tmp_path / "sec.db", database_url="")
    auth = AuthService(db=db)
    cache = AudioCache(cache_dir=tmp_path / "cache")
    rm = RoomManager(db=db, audio_cache=cache)
    app_main.rate_limiter.clear()
    monkeypatch.setattr(app_main, "db", db)
    monkeypatch.setattr(app_main, "auth_service", auth)
    monkeypatch.setattr(app_main, "audio_cache", cache)
    monkeypatch.setattr(app_main, "room_manager", rm)
    return db


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_c1_speaker_id_hijack_and_leader_takeover_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)
    try:
        with TestClient(app_main.app) as client:
            owner_tok = client.post("/api/auth/guest", json={"name": "OwnerAlice"}).json()["session_token"]
            client.cookies.clear()
            attacker_tok = client.post("/api/auth/guest", json={"name": "Mallory"}).json()["session_token"]
            client.cookies.clear()

            code = client.post(
                "/api/rooms",
                headers={"Authorization": f"Bearer {owner_tok}"},
                json={"name": "Protected Stage", "is_public": True},
            ).json()["room"]["code"]

            with client.websocket_connect(
                f"/ws/{code}?client_id=leader_box&name=AlicePC&token={owner_tok}"
            ) as ws_owner:
                w_owner = ws_owner.receive_json()
                assert w_owner["speaker_id"] == "leader_box"
                assert w_owner["is_leader"] is True
                ws_owner.receive_json()  # initial room_state

                # Attacker attempts to connect using Alice's exact client_id='leader_box'
                with client.websocket_connect(
                    f"/ws/{code}?client_id=leader_box&name=FakeAlice&token={attacker_tok}"
                ) as ws_attacker:
                    w_att = ws_attacker.receive_json()
                    assert w_att["speaker_id"] != "leader_box"
                    assert w_att["speaker_id"].startswith("sp_")
                    assert w_att["is_leader"] is False
                    ws_attacker.receive_json()  # attacker room_state
                    owner_update = ws_owner.receive_json()
                    assert owner_update["state"]["leader_speaker_id"] == "leader_box"

                # After attacker disconnects, Alice is still connected and still Leader
                after_disc = ws_owner.receive_json()
                assert after_disc["state"]["leader_speaker_id"] == "leader_box"
                ws_owner.send_json({"type": "sync_ping", "seq": 1, "t0": 100.0})
                assert ws_owner.receive_json()["type"] == "sync_pong"
    finally:
        db.close()


def test_c3_oauth_tokens_encrypted_at_rest_and_tamper_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)
    try:
        db.upsert_user(
            user_id="g_sec_1",
            provider="google",
            name="Encrypted User",
            email="enc@example.com",
            google_access_token="ya29.super_secret_access",
            google_refresh_token="1//0g_super_secret_refresh",
            google_token_expires_at=9999999999.0,
            has_youtube_scope=True,
        )
        # Raw row in SQLite must hold ciphertext, NOT plaintext tokens
        raw_row = db._fetchone("SELECT google_access_token, google_refresh_token FROM users WHERE id=?", ("g_sec_1",))
        assert raw_row is not None
        assert raw_row["google_access_token"].startswith("enc:v1:")
        assert "ya29.super_secret_access" not in raw_row["google_access_token"]
        assert raw_row["google_refresh_token"].startswith("enc:v1:")
        assert "1//0g_super_secret_refresh" not in raw_row["google_refresh_token"]

        # Decrypted view returns exact original tokens
        full = db.get_user_by_id("g_sec_1", include_tokens=True)
        assert full is not None
        assert full["google_access_token"] == "ya29.super_secret_access"
        assert full["google_refresh_token"] == "1//0g_super_secret_refresh"

        # Tampered ciphertext must fail constant-time MAC check cleanly
        ct = encrypt_secret("top_secret")
        tampered = ct[:-2] + ("AA" if not ct.endswith("AA") else "BB")
        assert decrypt_secret(tampered) == ""
    finally:
        db.close()


def test_h1_upload_magic_bytes_size_cap_and_passcode_authz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)  # 1 KB cap for fast test
    try:
        with TestClient(app_main.app) as client:
            owner_tok = client.post("/api/auth/guest", json={"name": "Owner"}).json()["session_token"]
            client.cookies.clear()
            outsider_tok = client.post("/api/auth/guest", json={"name": "Outsider"}).json()["session_token"]
            client.cookies.clear()
            owner_hdr = {"Authorization": f"Bearer {owner_tok}"}
            outsider_hdr = {"Authorization": f"Bearer {outsider_tok}"}

            code = client.post(
                "/api/rooms",
                headers=owner_hdr,
                json={"name": "VIP Room", "is_public": False, "passcode": "9876"},
            ).json()["room"]["code"]

            # 1. Outsider without room passcode / join_ticket is blocked with 403
            r_unauth_room = client.post(
                f"/api/rooms/{code}/upload",
                headers=outsider_hdr,
                files={"file": ("song.mp3", b"ID3" + b"\x00" * 32, "audio/mpeg")},
            )
            assert r_unauth_room.status_code == 403

            # 2. Disguised non-audio payload (.mp3 containing HTML/JS) rejected with 400
            r_bad_magic = client.post(
                f"/api/rooms/{code}/upload",
                headers=owner_hdr,
                files={"file": ("fake.mp3", b"<html><script>alert(1)</script></html>", "audio/mpeg")},
            )
            assert r_bad_magic.status_code == 400
            assert "header" in r_bad_magic.json()["detail"].lower()

            # 3. Oversized payload (> MAX_UPLOAD_BYTES) rejected with 413 during chunked stream read
            r_too_big = client.post(
                f"/api/rooms/{code}/upload",
                headers=owner_hdr,
                files={"file": ("huge.mp3", b"ID3" + (b"\x00" * 2048), "audio/mpeg")},
            )
            assert r_too_big.status_code == 413

            # 4. Valid MP3 by owner within limit succeeds
            r_ok = client.post(
                f"/api/rooms/{code}/upload",
                headers=owner_hdr,
                files={"file": ("real.mp3", b"ID3" + (b"\x00" * 64), "audio/mpeg")},
            )
            assert r_ok.status_code == 200
    finally:
        db.close()


def test_h2_h3_h4_ws_origin_unqueued_audio_and_track_sanitization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)
    try:
        # H-4: Malicious CSS/tracking thumbnail URLs stripped; YouTube thumbnails preserved
        t_evil = Track(
            video_id="dQw4w9WgXcQ",
            title="Test",
            thumbnail='https://evil.example/pixel.png"); background:red',
        )
        assert t_evil.thumbnail == ""

        t_good = Track(
            video_id="dQw4w9WgXcQ",
            title="Test",
            thumbnail="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        )
        assert t_good.thumbnail == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"

        with TestClient(app_main.app) as client:
            # H-3: Arbitrary uncached & unqueued video_id download blocked with 403
            r_audio = client.get("/api/audio/randomVid99")
            assert r_audio.status_code == 403

            # H-2: Cross-site WebSocket Origin rejected with close code 4403
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(
                    "/ws/STUDIO?client_id=cswsh_test",
                    headers={"Origin": "https://evil-phishing.example"},
                ) as ws:
                    ws.receive_json()
            assert exc_info.value.code == 4403

            # H-4: Vote stuffing in add_to_queue is stripped server-side
            client.post("/api/auth/guest", json={"name": "Leader"})
            code = client.post("/api/rooms", json={"name": "VoteTest"}).json()["room"]["code"]
            with client.websocket_connect(f"/ws/{code}?client_id=ldr1") as ws:
                ws.receive_json()  # welcome
                ws.receive_json()  # room_state
                ws.send_json(
                    {
                        "type": "add_to_queue",
                        "tracks": [
                            {
                                "video_id": "dQw4w9WgXcQ",
                                "title": "Stuffed Song",
                                "votes": ["fake1", "fake2", "fake3"],
                            }
                        ],
                    }
                )
                st = ws.receive_json()["state"]
                assert len(st["queue"]) == 1
                assert st["queue"][0]["votes"] == []
    finally:
        db.close()


def test_h5_m1_m2_m4_rate_limit_scrypt_tickets_redaction_and_qr_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "FRONTEND_URL", "https://soundmesh.vercel.app")

    async def fake_callback(code: str, state: str):
        u = db.upsert_user("g_99", "google", "OneTimeUser")
        tok = db.create_session("g_99")
        return u, tok, "/room/SAFE01"

    monkeypatch.setattr(app_main.auth_service, "handle_google_callback", fake_callback)

    try:
        # Legacy SHA-256 passcode backward compatibility
        legacy_hash = "deadbeef:" + hashlib.sha256(b"deadbeef:4321").hexdigest()
        assert _verify_passcode_hash("4321", legacy_hash) is True

        with TestClient(app_main.app) as client:
            # Security headers present
            r_root = client.get("/")
            assert r_root.headers.get("x-frame-options") == "DENY"
            assert r_root.headers.get("x-content-type-options") == "nosniff"
            assert "frame-ancestors 'none'" in r_root.headers.get("content-security-policy", "")

            # M-4: Open QR phishing blocked
            assert client.get("/api/qr", params={"url": "https://evil.example/login"}).status_code == 400
            assert client.get("/api/qr", params={"url": "http://127.0.0.1:8765/room/STUDIO"}).status_code == 200

            # M-1: One-time OAuth ticket exchange (raw session token NOT in redirect URL)
            r_cb = client.get("/api/auth/google/callback?code=c1&state=s1", follow_redirects=False)
            loc = r_cb.headers["location"]
            otk = loc.split("#auth_token=", 1)[1]
            assert otk.startswith("otk_")
            r_ex1 = client.post("/api/auth/exchange", json={"ticket": otk})
            assert r_ex1.status_code == 200
            real_session = r_ex1.json()["session_token"]
            assert real_session and real_session != otk
            # Replay of single-use ticket must fail
            assert client.post("/api/auth/exchange", json={"ticket": otk}).status_code == 401

            # M-1: Single-use WebSocket ticket
            r_wst = client.post(
                "/api/auth/ws-ticket", headers={"Authorization": f"Bearer {real_session}"}
            )
            wst = r_wst.json()["ws_ticket"]
            assert wst.startswith("wst_")
            client.cookies.clear()
            with client.websocket_connect(f"/ws/STUDIO?ticket={wst}&client_id=tdev") as ws:
                w = ws.receive_json()
                assert w["state"]["speakers"][0]["user_name"] == "OneTimeUser"

            # M-2 & H-5: Scrypt room passcode + metadata redaction + brute-force 429
            owner_tok = client.post("/api/auth/guest", json={"name": "SecretHost"}).json()["session_token"]
            client.cookies.clear()
            owner_hdr = {"Authorization": f"Bearer {owner_tok}"}

            code = client.post(
                "/api/rooms",
                headers=owner_hdr,
                json={"name": "Hidden Hostel", "is_public": False, "passcode": "5566"},
            ).json()["room"]["code"]

            raw_room = db._fetchone("SELECT passcode_hash FROM rooms WHERE code=?", (code,))
            assert raw_room is not None and raw_room["passcode_hash"].startswith("scrypt$")

            # Unverified caller sees redacted room summary
            redacted = client.get(f"/api/rooms/{code}").json()["room"]
            assert redacted["has_passcode"] is True
            assert redacted["owner_user_id"] == ""
            assert redacted["leader_name"] == "Protected Room"

            # Owner sees full room summary
            full_sum = client.get(f"/api/rooms/{code}", headers=owner_hdr).json()["room"]
            assert full_sum["owner_name"] == "SecretHost"

            # Brute-force >8 passcode guesses triggers HTTP 429
            statuses = [
                client.post(f"/api/rooms/{code}/verify", json={"passcode": f"000{i}"}).status_code
                for i in range(9)
            ]
            assert 429 in statuses
    finally:
        db.close()


@pytest.mark.anyio
async def test_m5_cloudflared_sha256_mismatch_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm = TunnelManager(bin_dir=tmp_path / "bin")
    monkeypatch.setattr(tm, "find_cloudflared", lambda: None)

    fake_binary = b"MZ" + (b"\x90" * 1_100_000)
    wrong_sha = "0" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".sha256"):
            return httpx.Response(200, text=f"{wrong_sha}  cloudflared\n")
        return httpx.Response(200, content=fake_binary)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def patched_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    with pytest.raises(RuntimeError, match="SHA-256"):
        await tm.ensure_cloudflared()
    # Partial/unverified binary must be deleted
    assert list((tmp_path / "bin").glob("*")) == []

"""FastAPI application: Auth, Personal YT Library, Multi-Room Lobby, QR, & WebSocket Sync."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import qrcode
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import config
from backend.audio_cache import AudioCache, validate_video_id
from backend.auth_service import AuthService
from backend.db import Database
from backend.models import AuthProvider, DeviceType, Track
from backend.room_manager import RoomManager
from backend.tunnel_manager import TunnelManager
from backend.ytmusic_service import YTMusicService

logger = logging.getLogger(__name__)

db = Database()
auth_service = AuthService(db=db)
audio_cache = AudioCache()
ytmusic_service = YTMusicService()
room_manager = RoomManager(db=db, audio_cache=audio_cache)
tunnel_manager = TunnelManager()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    room_manager.get_or_create_default_room()
    auto_task: asyncio.Task[Any] | None = None
    if config.AUTO_TUNNEL:
        async def _auto_start() -> None:
            st = await tunnel_manager.start(config.PORT)
            if st.get("public_url"):
                print(
                    f"\n============================================================\n"
                    f"  SOUNDMESH LIVE PUBLIC WEBSITE: {st['public_url']}\n"
                    f"============================================================\n",
                    flush=True,
                )

        auto_task = asyncio.create_task(_auto_start())
    try:
        yield
    finally:
        if auto_task and not auto_task.done():
            auto_task.cancel()
        await tunnel_manager.stop()
        await room_manager.stop_all()


app = FastAPI(title="SoundMesh Multi-Room Music Sync", lifespan=lifespan)

if (config.FRONTEND_DIR / "css").is_dir():
    app.mount("/css", StaticFiles(directory=str(config.FRONTEND_DIR / "css")), name="css")
if (config.FRONTEND_DIR / "js").is_dir():
    app.mount("/js", StaticFiles(directory=str(config.FRONTEND_DIR / "js")), name="js")


# ------------------------------------------------------------------
# Request Models & Helpers
# ------------------------------------------------------------------


class GuestAuthPayload(BaseModel):
    name: str


class CreateRoomPayload(BaseModel):
    name: str = ""
    is_public: bool = True
    passcode: str = ""


class VerifyRoomPayload(BaseModel):
    passcode: str = ""


def _generate_qr_png_bytes(url: str) -> bytes:
    qr = qrcode.QRCode(version=None, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _annotate_cached(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in tracks:
        vid = item.get("video_id")
        if isinstance(vid, str):
            item["cached"] = audio_cache.is_cached(vid)
    return tracks


def _get_current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if not token:
        return None
    return db.get_user_by_session_token(token)


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    base_url = config.resolve_base_url(request)
    is_https = base_url.startswith("https://")
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=token,
        max_age=config.SESSION_TTL_SEC,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "index.html")


@app.get("/room/{room_code}")
async def serve_room_page(room_code: str) -> FileResponse:
    _ = room_code
    return FileResponse(config.FRONTEND_DIR / "index.html")


@app.get("/join")
async def serve_join() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "index.html")


# ------------------------------------------------------------------
# Auth Endpoints (Google OAuth PKCE + Guest)
# ------------------------------------------------------------------


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    base_url = config.resolve_base_url(request)
    host_header = request.headers.get("host", "")
    return {
        "user": user,
        "auth_config": {
            "google_enabled": config.google_oauth_configured(),
            "youtube_scope_enabled": config.GOOGLE_ENABLE_YOUTUBE_SCOPE,
            "callback_url": f"{base_url}/api/auth/google/callback",
            "base_url": base_url,
            "is_private_ip": config.is_private_lan_host(host_header),
        },
    }


@app.post("/api/auth/guest")
async def auth_guest(payload: GuestAuthPayload, request: Request) -> JSONResponse:
    existing = _get_current_user(request)
    user, token = auth_service.create_or_update_guest(payload.name, existing_user=existing)
    resp = JSONResponse({"user": user})
    _set_session_cookie(resp, token, request)
    return resp


@app.get("/api/auth/google/login")
async def auth_google_login(
    request: Request,
    next: str = Query("/"),
    include_youtube: bool = Query(True),
) -> Response:
    if not config.google_oauth_configured():
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env.",
        )
    base_url = config.resolve_base_url(request)
    redirect_uri = f"{base_url}/api/auth/google/callback"
    url = auth_service.build_google_auth_url(
        redirect_uri=redirect_uri,
        next_url=next,
        include_youtube=include_youtube,
    )
    return RedirectResponse(url=url, status_code=302)


@app.get("/api/auth/google/callback")
async def auth_google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error:
        return RedirectResponse(url=f"/?{urlencode({'auth_error': error})}", status_code=302)
    if not code or not state:
        return RedirectResponse(
            url=f"/?{urlencode({'auth_error': 'missing_oauth_params'})}", status_code=302
        )
    try:
        _user, session_token, next_url = await auth_service.handle_google_callback(
            code=code, state=state
        )
    except Exception as exc:
        logger.warning("Google OAuth callback error: %s", exc)
        return RedirectResponse(
            url=f"/?{urlencode({'auth_error': str(exc)})}", status_code=302
        )

    safe_next = auth_service.sanitize_next_url(next_url)
    resp = RedirectResponse(url=safe_next, status_code=302)
    _set_session_cookie(resp, session_token, request)
    return resp


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if token:
        db.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE_NAME, path="/")
    return resp


# ------------------------------------------------------------------
# Personal YouTube Music / Library Endpoints
# ------------------------------------------------------------------


@app.get("/api/me/playlists")
async def my_playlists(request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in with Google first.")
    if user.get("provider") != "google" or not user.get("has_youtube_scope"):
        raise HTTPException(
            status_code=403,
            detail="Google account with YouTube Library permission required.",
        )
    try:
        playlists = await auth_service.fetch_user_playlists(user["id"])
        return {"playlists": playlists}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed loading YouTube playlists: {exc}")


@app.get("/api/me/playlists/{playlist_id}/tracks")
async def my_playlist_tracks(playlist_id: str, request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in with Google first.")
    if user.get("provider") != "google" or not user.get("has_youtube_scope"):
        raise HTTPException(
            status_code=403,
            detail="Google account with YouTube Library permission required.",
        )
    try:
        result = await auth_service.fetch_user_playlist_tracks(user["id"], playlist_id)
        _annotate_cached(result.get("tracks", []))
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed loading playlist tracks: {exc}")


# ------------------------------------------------------------------
# Multi-Room Management Endpoints
# ------------------------------------------------------------------


@app.get("/api/rooms")
async def list_rooms(request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    public_rooms = room_manager.list_public_rooms()
    my_rooms = room_manager.list_user_rooms(user["id"]) if user else []
    return {"public_rooms": public_rooms, "my_rooms": my_rooms}


@app.post("/api/rooms")
async def create_room(payload: CreateRoomPayload, request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in with Google or Guest first.")
    room = room_manager.create_room(
        name=payload.name,
        owner_user=user,
        is_public=payload.is_public,
        passcode=payload.passcode,
    )
    return {"room": room.get_summary()}


@app.get("/api/rooms/{room_code}")
async def get_room(room_code: str) -> dict[str, Any]:
    room = room_manager.get_or_load_room(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found.")
    return {"room": room.get_summary()}


@app.post("/api/rooms/{room_code}/verify")
async def verify_room(room_code: str, payload: VerifyRoomPayload) -> dict[str, Any]:
    room = room_manager.get_or_load_room(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found.")
    ok = room_manager.verify_passcode(room.room_code, payload.passcode)
    return {"ok": ok}


@app.delete("/api/rooms/{room_code}")
async def delete_room(room_code: str, request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    rec = db.get_room_record(room_code)
    if not rec:
        raise HTTPException(status_code=404, detail="Room not found.")
    if rec["owner_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the room owner can delete this room.")
    await room_manager.close_room(room_code, user["id"])
    return {"ok": True}


def _build_room_info_payload(room_code: str, request: Request) -> dict[str, Any]:
    clean_code = room_code.strip().upper()
    lan_ips = config.get_lan_ips()
    join_urls: list[str] = []
    public_url = config.get_public_url()
    if public_url:
        join_urls.append(f"{public_url}/room/{clean_code}")
    else:
        base_url = config.resolve_base_url(request)
        if base_url.startswith("https://"):
            join_urls.append(f"{base_url}/room/{clean_code}")

    for ip in lan_ips:
        candidate = f"http://{ip}:{config.PORT}/room/{clean_code}"
        if candidate not in join_urls:
            join_urls.append(candidate)

    primary_url = join_urls[0]
    png_bytes = _generate_qr_png_bytes(primary_url)
    qr_data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        "room_code": clean_code,
        "port": config.PORT,
        "lan_ips": lan_ips,
        "primary_ip": lan_ips[0],
        "public_url": public_url,
        "join_url": primary_url,
        "join_urls": join_urls,
        "qr_data_uri": qr_data_uri,
        "deno_available": audio_cache.deno_path is not None,
    }


def _is_local_direct_request(request: Request) -> bool:
    """Allow tunnel start/stop only from the host machine directly (never remote proxies)."""
    if request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for"):
        return False
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient") or host.startswith("127.")


@app.get("/api/tunnel/status")
async def tunnel_status(request: Request) -> dict[str, Any]:
    return {
        **tunnel_manager.status(),
        "can_control": _is_local_direct_request(request),
    }


@app.post("/api/tunnel/start")
async def tunnel_start(request: Request) -> dict[str, Any]:
    if not _is_local_direct_request(request):
        raise HTTPException(
            status_code=403, detail="Only the local host machine can start the public tunnel."
        )
    st = await tunnel_manager.start(config.PORT)
    return {**st, "can_control": True}


@app.post("/api/tunnel/stop")
async def tunnel_stop(request: Request) -> dict[str, Any]:
    if not _is_local_direct_request(request):
        raise HTTPException(
            status_code=403, detail="Only the local host machine can stop the public tunnel."
        )
    st = await tunnel_manager.stop()
    return {**st, "can_control": True}


@app.get("/api/rooms/{room_code}/info")
async def room_info(room_code: str, request: Request) -> dict[str, Any]:
    room = room_manager.get_or_load_room(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found.")
    return _build_room_info_payload(room.room_code, request)


# ------------------------------------------------------------------
# Core Health, QR, Search & Audio Streaming Endpoints
# ------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "server_now_ms": config.server_now_ms()}


@app.get("/api/info")
async def server_info(request: Request) -> dict[str, Any]:
    return _build_room_info_payload("STUDIO", request)


@app.get("/api/qr")
async def qr_image(url: str = Query(..., min_length=4, max_length=512)) -> Response:
    png_bytes = _generate_qr_png_bytes(url)
    return Response(content=png_bytes, media_type="image/png")


@app.get("/api/search")
async def search_music(
    q: str = Query("", max_length=400),
    filter: str = Query("songs"),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    try:
        result = await ytmusic_service.search(q, filter_type=filter, limit=limit)
        if isinstance(result.get("tracks"), list):
            _annotate_cached(result["tracks"])
        return result
    except Exception as exc:
        logger.exception("Search error: %s", exc)
        raise HTTPException(status_code=502, detail=f"YouTube Music search failed: {exc}")


@app.get("/api/playlist/{playlist_id}")
async def get_playlist(
    playlist_id: str, limit: int = Query(50, ge=1, le=100)
) -> dict[str, Any]:
    try:
        result = await ytmusic_service.get_playlist_tracks(playlist_id, limit=limit)
        _annotate_cached(result.get("tracks", []))
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to load playlist: {exc}")


@app.get("/api/album/{browse_id}")
async def get_album(browse_id: str) -> dict[str, Any]:
    try:
        result = await ytmusic_service.get_album_tracks(browse_id)
        _annotate_cached(result.get("tracks", []))
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to load album: {exc}")


@app.get("/api/related/{video_id}")
async def get_related(
    video_id: str, limit: int = Query(15, ge=1, le=30)
) -> dict[str, Any]:
    try:
        validate_video_id(video_id)
        tracks = await ytmusic_service.get_related_tracks(video_id, limit=limit)
        _annotate_cached(tracks)
        return {"tracks": tracks}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to load radio: {exc}")


@app.get("/api/audio/{video_id}")
async def stream_audio(video_id: str) -> FileResponse:
    try:
        validate_video_id(video_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        info = await audio_cache.ensure_track(video_id)
    except Exception as exc:
        logger.exception("Audio fetch error for %s: %s", video_id, exc)
        raise HTTPException(status_code=502, detail=f"Audio unavailable: {exc}")

    return FileResponse(
        path=info["path"],
        media_type=info["mime_type"],
        headers={
            "Cache-Control": "public, max-age=3600",
            "Accept-Ranges": "bytes",
        },
    )


# ------------------------------------------------------------------
# WebSocket Sync & Leader Control Endpoint
# ------------------------------------------------------------------


def _parse_device_type(raw: str | None) -> DeviceType:
    if raw in ("desktop", "mobile", "tablet"):
        return raw  # type: ignore[return-value]
    return "desktop"


def _parse_tracks(raw_list: Any) -> list[Track]:
    tracks: list[Track] = []
    if not isinstance(raw_list, list):
        return tracks
    for item in raw_list:
        if isinstance(item, dict) and item.get("video_id"):
            try:
                tracks.append(Track(**item))
            except Exception:
                continue
    return tracks


async def _handle_room_ws(ws: WebSocket, room_code: str) -> None:
    room = room_manager.get_or_load_room(room_code)
    if not room:
        await ws.accept()
        await ws.close(code=4404, reason="Room not found")
        return

    qp = ws.query_params
    session_token = ws.cookies.get(config.SESSION_COOKIE_NAME)
    user = db.get_user_by_session_token(session_token) if session_token else None

    passcode = (qp.get("passcode") or "").strip()
    is_owner = bool(user and room.owner_user_id and user["id"] == room.owner_user_id)
    if room.has_passcode and not is_owner:
        if not room_manager.verify_passcode(room.room_code, passcode):
            await ws.accept()
            await ws.close(code=4403, reason="Invalid room passcode")
            return

    await ws.accept()

    speaker_id = (qp.get("client_id") or "").strip() or uuid.uuid4().hex[:12]
    raw_name = (qp.get("name") or "").strip()
    device_type = _parse_device_type(qp.get("device_type"))
    role = (qp.get("role") or "stereo").strip().lower()
    try:
        offset_ms = float(qp.get("offset_ms") or 0.0)
    except ValueError:
        offset_ms = 0.0

    if user:
        user_id = str(user["id"])
        user_name = str(user["name"])
        user_avatar = str(user.get("avatar_url") or "")
        auth_provider: AuthProvider = (
            "google" if user.get("provider") == "google" else "guest"
        )
    else:
        user_id = f"anon_{speaker_id}"
        user_name = raw_name or "Guest Speaker"
        user_avatar = ""
        auth_provider = "guest"

    device_name = raw_name or user_name
    db.touch_room(room.room_code)

    await room.register_speaker(
        speaker_id=speaker_id,
        ws=ws,
        name=device_name,
        device_type=device_type,
        user_id=user_id,
        user_name=user_name,
        user_avatar=user_avatar,
        auth_provider=auth_provider,
        role=role,
        offset_ms=offset_ms,
    )

    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type")

            # CRITICAL FAST PATH for NTP clock synchronization
            if msg_type == "sync_ping":
                t1 = config.server_now_ms()
                await ws.send_json(
                    {
                        "type": "sync_pong",
                        "seq": msg.get("seq", 0),
                        "t0": msg.get("t0", 0),
                        "t1": t1,
                        "t2": config.server_now_ms(),
                    }
                )
                continue

            if msg_type == "telemetry":
                await room.update_telemetry(
                    speaker_id=speaker_id,
                    rtt_ms=float(msg.get("rtt_ms") or 0.0),
                    drift_ms=float(msg.get("drift_ms") or 0.0),
                    sync_status=str(msg.get("sync_status") or "idle"),
                )
            elif msg_type == "track_ready":
                room.mark_track_ready(speaker_id, int(msg.get("generation") or -1))
            elif msg_type == "update_speaker":
                target_id = str(msg.get("target_id") or speaker_id)
                fields = msg.get("fields") if isinstance(msg.get("fields"), dict) else msg
                await room.update_speaker(speaker_id, target_id, fields)
            elif msg_type == "assign_leader":
                target_id = str(msg.get("target_id") or "")
                if target_id:
                    await room.assign_leader(speaker_id, target_id)
            elif msg_type == "request_leader":
                await room.request_leader(speaker_id)
            elif msg_type == "respond_leader_request":
                req_id = str(msg.get("requester_id") or "")
                if req_id:
                    await room.respond_leader_request(
                        speaker_id, req_id, bool(msg.get("approve", False))
                    )
            elif msg_type == "set_allow_collab_queue":
                await room.set_allow_collab_queue(
                    speaker_id, bool(msg.get("allowed", False))
                )
            elif msg_type == "play_now":
                track_data = msg.get("track")
                if isinstance(track_data, dict) and track_data.get("video_id"):
                    await room.play_now(speaker_id, Track(**track_data))
            elif msg_type == "add_to_queue":
                tracks = _parse_tracks(msg.get("tracks"))
                if not tracks and isinstance(msg.get("track"), dict):
                    tracks = _parse_tracks([msg.get("track")])
                await room.add_to_queue(
                    speaker_id,
                    tracks,
                    play_next=bool(msg.get("play_next", False)),
                    play_if_idle=bool(msg.get("play_if_idle", False)),
                )
            elif msg_type == "play_index":
                if room.can_control_room(speaker_id):
                    await room.play_queue_index(int(msg.get("index", -1)))
            elif msg_type == "remove_queue":
                await room.remove_queue_item(speaker_id, int(msg.get("index", -1)))
            elif msg_type == "move_queue":
                await room.move_queue_item(
                    speaker_id,
                    int(msg.get("from_index", -1)),
                    int(msg.get("to_index", -1)),
                )
            elif msg_type == "clear_queue":
                await room.clear_queue(speaker_id)
            elif msg_type == "pause":
                await room.pause(speaker_id)
            elif msg_type == "resume":
                await room.resume(speaker_id)
            elif msg_type == "seek":
                await room.seek(speaker_id, float(msg.get("position_sec") or 0.0))
            elif msg_type == "next":
                await room.next_track(speaker_id)
            elif msg_type == "prev":
                await room.prev_track(speaker_id)
            elif msg_type == "force_resync":
                await room.force_resync(speaker_id)
            elif msg_type == "set_shuffle":
                await room.set_shuffle(speaker_id, bool(msg.get("enabled", False)))
            elif msg_type == "set_repeat":
                await room.set_repeat(speaker_id, str(msg.get("mode") or "off"))
            elif msg_type == "set_master_volume":
                vol = msg.get("volume")
                muted = msg.get("muted")
                await room.set_master_volume(
                    speaker_id,
                    volume=float(vol) if vol is not None else None,
                    muted=bool(muted) if muted is not None else None,
                )
            elif msg_type == "set_allow_guest_control":
                await room.set_allow_guest_control(
                    speaker_id, bool(msg.get("allowed", False))
                )
            elif msg_type == "auto_assign_stereo":
                await room.auto_assign_stereo(speaker_id)
            elif msg_type == "identify_speaker":
                target = str(msg.get("target_id") or speaker_id)
                await room.identify_speaker(speaker_id, target)
            elif msg_type == "set_calibration":
                await room.set_calibration(speaker_id, bool(msg.get("active", False)))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket error for %s in room %s: %s", speaker_id, room.room_code, exc)
    finally:
        await room.unregister_speaker(speaker_id)


@app.websocket("/ws")
async def websocket_default_endpoint(ws: WebSocket) -> None:
    room_manager.get_or_create_default_room()
    await _handle_room_ws(ws, "STUDIO")


@app.websocket("/ws/{room_code}")
async def websocket_room_endpoint(ws: WebSocket, room_code: str) -> None:
    await _handle_room_ws(ws, room_code)

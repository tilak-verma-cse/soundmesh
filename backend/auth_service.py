"""Google OAuth 2.0 (PKCE), Guest authentication, and personal YouTube library access."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from backend import config
from backend.db import Database
from backend.models import Track
from backend.ytmusic_service import pick_best_thumbnail

logger = logging.getLogger(__name__)

_ISO_DUR_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

_YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
_STATE_TTL_SEC = 600
_MAX_PENDING_STATES = 500
_MAX_ONE_TIME_TICKETS = 1000


def parse_iso8601_duration(iso: str) -> float:
    """Convert YouTube ISO-8601 duration strings (e.g. PT3M45S) into seconds."""
    if not isinstance(iso, str) or not iso:
        return 0.0
    m = _ISO_DUR_RE.match(iso.strip())
    if not m:
        return 0.0
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class AuthService:
    """Manages Google OAuth PKCE sign-in, guest users, and personal YouTube Data API calls."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._pending_states: dict[str, dict[str, Any]] = {}
        self._one_time_tickets: dict[str, dict[str, Any]] = {}

    def _prune_tickets(self) -> None:
        now = time.time()
        expired = [
            k for k, v in self._one_time_tickets.items() if float(v["expires_at"]) <= now
        ]
        for k in expired:
            self._one_time_tickets.pop(k, None)

    def issue_one_time_ticket(
        self, payload: dict[str, Any], prefix: str = "wst", ttl_sec: int = 45
    ) -> str:
        """Mint a single-use short-lived opaque ticket (`wst_...` or `otk_...`)."""
        self._prune_tickets()
        while len(self._one_time_tickets) >= _MAX_ONE_TIME_TICKETS:
            self._one_time_tickets.pop(next(iter(self._one_time_tickets)), None)
        ticket = f"{prefix}_{secrets.token_urlsafe(24)}"
        self._one_time_tickets[ticket] = {
            **payload,
            "prefix": prefix,
            "expires_at": time.time() + ttl_sec,
        }
        return ticket

    def consume_one_time_ticket(
        self, ticket: str, expected_prefix: str
    ) -> dict[str, Any] | None:
        """Atomically consume and validate a single-use short-lived ticket."""
        if not ticket or not ticket.startswith(f"{expected_prefix}_"):
            return None
        self._prune_tickets()
        entry = self._one_time_tickets.pop(ticket, None)
        if not entry:
            return None
        if entry.get("prefix") != expected_prefix or float(entry["expires_at"]) <= time.time():
            return None
        return entry

    @staticmethod
    def sanitize_next_url(next_url: str | None) -> str:
        """Allow only local relative paths starting with '/' to prevent open redirects."""
        if not next_url:
            return "/"
        cleaned = next_url.strip()
        if not cleaned.startswith("/") or cleaned.startswith("//") or "\\" in cleaned:
            return "/"
        return cleaned

    def _prune_expired_states(self) -> None:
        now = time.time()
        expired = [
            st
            for st, meta in self._pending_states.items()
            if now - float(meta["created_at"]) > _STATE_TTL_SEC
        ]
        for st in expired:
            self._pending_states.pop(st, None)
        while len(self._pending_states) >= _MAX_PENDING_STATES:
            self._pending_states.pop(next(iter(self._pending_states)), None)

    def build_google_auth_url(
        self,
        redirect_uri: str,
        next_url: str = "/",
        include_youtube: bool = True,
    ) -> str:
        """Create a Google OAuth 2.0 PKCE authorization URL."""
        if not config.google_oauth_configured():
            raise RuntimeError("Google OAuth is not configured on the server.")

        self._prune_expired_states()
        state = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        safe_next = self.sanitize_next_url(next_url)

        scopes = ["openid", "email", "profile"]
        use_yt = bool(include_youtube and config.GOOGLE_ENABLE_YOUTUBE_SCOPE)
        if use_yt:
            scopes.append(_YOUTUBE_SCOPE)

        self._pending_states[state] = {
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "next_url": safe_next,
            "created_at": time.time(),
        }

        params = {
            "client_id": config.GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": _pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "select_account consent" if use_yt else "select_account",
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def handle_google_callback(
        self, code: str, state: str
    ) -> tuple[dict[str, Any], str, str]:
        """Exchange OAuth code for tokens, upsert user, and create session."""
        self._prune_expired_states()
        pending = self._pending_states.pop(state, None)
        if not pending:
            raise ValueError("Invalid or expired OAuth state.")

        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": config.GOOGLE_CLIENT_ID,
                    "client_secret": config.GOOGLE_CLIENT_SECRET,
                    "code": code,
                    "code_verifier": pending["code_verifier"],
                    "grant_type": "authorization_code",
                    "redirect_uri": pending["redirect_uri"],
                },
            )
            if token_resp.status_code != 200:
                raise ValueError(f"Google token exchange failed ({token_resp.status_code})")
            token_data = token_resp.json()

            access_token = str(token_data.get("access_token") or "")
            if not access_token:
                raise ValueError("Google returned no access token.")

            refresh_token = token_data.get("refresh_token")
            expires_in = float(token_data.get("expires_in") or 3600)
            granted_scope = str(token_data.get("scope") or "")
            has_yt = _YOUTUBE_SCOPE in granted_scope

            user_resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code != 200:
                raise ValueError(f"Google userinfo failed ({user_resp.status_code})")
            profile = user_resp.json()

        sub = str(profile.get("sub") or "").strip()
        if not sub:
            raise ValueError("Google userinfo missing subject ID.")

        user = self.db.upsert_user(
            user_id=f"g_{sub}",
            provider="google",
            email=str(profile.get("email") or ""),
            name=str(profile.get("name") or profile.get("email") or "Google User")[:48],
            avatar_url=str(profile.get("picture") or ""),
            google_access_token=access_token,
            google_refresh_token=str(refresh_token) if refresh_token else None,
            google_token_expires_at=time.time() + expires_in,
            has_youtube_scope=has_yt,
        )
        session_token = self.db.create_session(user["id"])
        return user, session_token, pending["next_url"]

    async def get_valid_google_access_token(self, user_id: str) -> str | None:
        """Return a valid Google OAuth access token, refreshing if needed."""
        user = self.db.get_user_by_id(user_id, include_tokens=True)
        if not user or user.get("provider") != "google":
            return None

        access_token = str(user.get("google_access_token") or "")
        expires_at = float(user.get("google_token_expires_at") or 0.0)
        if access_token and expires_at > time.time() + 60.0:
            return access_token

        refresh_token = str(user.get("google_refresh_token") or "")
        if not refresh_token or not config.google_oauth_configured():
            return access_token or None

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": config.GOOGLE_CLIENT_ID,
                    "client_secret": config.GOOGLE_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if resp.status_code != 200:
                logger.warning("Google token refresh failed (%s)", resp.status_code)
                return None
            data = resp.json()

        new_access = str(data.get("access_token") or "")
        if not new_access:
            return None
        expires_in = float(data.get("expires_in") or 3600)
        self.db.upsert_user(
            user_id=user_id,
            provider="google",
            name=user["name"],
            google_access_token=new_access,
            google_token_expires_at=time.time() + expires_in,
        )
        return new_access

    def create_or_update_guest(
        self, name: str, existing_user: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str]:
        """Create a new Guest identity or update an existing guest's display name."""
        clean_name = (name or "Guest Listener").strip()[:36] or "Guest Listener"
        if existing_user and existing_user.get("provider") == "guest":
            user_id = existing_user["id"]
        else:
            user_id = f"guest_{secrets.token_hex(6)}"

        user = self.db.upsert_user(
            user_id=user_id,
            provider="guest",
            name=clean_name,
        )
        session_token = self.db.create_session(user["id"])
        return user, session_token

    # ------------------------------------------------------------------
    # Personal YouTube / YouTube Music Playlists
    # ------------------------------------------------------------------

    async def fetch_user_playlists(self, user_id: str) -> list[dict[str, Any]]:
        """Fetch the signed-in Google user's YouTube playlists + Liked Music."""
        token = await self.get_valid_google_access_token(user_id)
        if not token:
            raise PermissionError("Google YouTube authorization missing or expired.")

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{YOUTUBE_API_BASE}/playlists",
                params={
                    "part": "snippet,contentDetails",
                    "mine": "true",
                    "maxResults": 50,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401 or resp.status_code == 403:
                raise PermissionError("YouTube Music/Library permission required.")
            if resp.status_code != 200:
                raise RuntimeError(f"YouTube API error ({resp.status_code})")
            data = resp.json()

        playlists: list[dict[str, Any]] = [
            {
                "id": "LL",
                "kind": "youtube_liked",
                "title": "Liked Music & Videos",
                "subtitle": "Your personal YouTube Likes",
                "item_count": "",
                "thumbnail": "",
            }
        ]
        for item in data.get("items") or []:
            pid = item.get("id")
            snip = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            thumbs = list((snip.get("thumbnails") or {}).values())
            if pid:
                playlists.append(
                    {
                        "id": str(pid),
                        "kind": "youtube_playlist",
                        "title": str(snip.get("title") or "Untitled Playlist"),
                        "subtitle": str(snip.get("channelTitle") or "Your Playlist"),
                        "item_count": str(details.get("itemCount") or ""),
                        "thumbnail": pick_best_thumbnail(thumbs),
                    }
                )
        return playlists

    async def fetch_user_playlist_tracks(
        self, user_id: str, playlist_id: str
    ) -> dict[str, Any]:
        """Fetch tracks from a personal YouTube playlist or 'LL' (Liked Videos)."""
        token = await self.get_valid_google_access_token(user_id)
        if not token:
            raise PermissionError("Google YouTube authorization missing or expired.")

        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            if playlist_id == "LL":
                resp = await client.get(
                    f"{YOUTUBE_API_BASE}/videos",
                    params={
                        "part": "snippet,contentDetails",
                        "myRating": "like",
                        "maxResults": 50,
                    },
                    headers=headers,
                )
                if resp.status_code in (401, 403):
                    raise PermissionError("YouTube permission denied.")
                if resp.status_code != 200:
                    raise RuntimeError(f"YouTube API error ({resp.status_code})")
                data = resp.json()
                tracks: list[dict[str, Any]] = []
                for item in data.get("items") or []:
                    vid = item.get("id")
                    snip = item.get("snippet") or {}
                    details = item.get("contentDetails") or {}
                    thumbs = list((snip.get("thumbnails") or {}).values())
                    if not vid:
                        continue
                    tracks.append(
                        Track(
                            video_id=str(vid),
                            title=str(snip.get("title") or "Untitled"),
                            artist=str(snip.get("channelTitle") or "YouTube"),
                            duration_sec=parse_iso8601_duration(
                                str(details.get("duration") or "")
                            ),
                            thumbnail=pick_best_thumbnail(thumbs, str(vid)),
                        ).model_dump()
                    )
                return {
                    "id": "LL",
                    "title": "Liked Music & Videos",
                    "subtitle": "Your YouTube Likes",
                    "tracks": tracks,
                }

            pl_resp = await client.get(
                f"{YOUTUBE_API_BASE}/playlistItems",
                params={
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": 50,
                },
                headers=headers,
            )
            if pl_resp.status_code in (401, 403):
                raise PermissionError("YouTube permission denied.")
            if pl_resp.status_code != 200:
                raise RuntimeError(f"YouTube API error ({pl_resp.status_code})")
            pl_data = pl_resp.json()

            items = pl_data.get("items") or []
            video_ids: list[str] = []
            raw_entries: list[dict[str, Any]] = []
            for item in items:
                snip = item.get("snippet") or {}
                details = item.get("contentDetails") or {}
                vid = details.get("videoId") or (snip.get("resourceId") or {}).get("videoId")
                if not vid:
                    continue
                vid_str = str(vid)
                video_ids.append(vid_str)
                raw_entries.append({"video_id": vid_str, "snippet": snip})

            durations: dict[str, float] = {}
            if video_ids:
                dur_resp = await client.get(
                    f"{YOUTUBE_API_BASE}/videos",
                    params={
                        "part": "contentDetails",
                        "id": ",".join(video_ids[:50]),
                    },
                    headers=headers,
                )
                if dur_resp.status_code == 200:
                    for v in dur_resp.json().get("items") or []:
                        vid = str(v.get("id") or "")
                        iso = str((v.get("contentDetails") or {}).get("duration") or "")
                        durations[vid] = parse_iso8601_duration(iso)

            tracks = []
            for entry in raw_entries:
                vid = entry["video_id"]
                snip = entry["snippet"]
                thumbs = list((snip.get("thumbnails") or {}).values())
                artist = str(
                    snip.get("videoOwnerChannelTitle")
                    or snip.get("channelTitle")
                    or "YouTube"
                )
                tracks.append(
                    Track(
                        video_id=vid,
                        title=str(snip.get("title") or "Untitled"),
                        artist=artist,
                        duration_sec=durations.get(vid, 0.0),
                        thumbnail=pick_best_thumbnail(thumbs, vid),
                    ).model_dump()
                )
            return {
                "id": playlist_id,
                "title": "Personal Playlist",
                "subtitle": f"{len(tracks)} tracks",
                "tracks": tracks,
            }

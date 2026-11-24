"""YouTube Music search, URL resolution, and playlist/album/radio extraction."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from ytmusicapi import YTMusic

from backend.models import Track

logger = logging.getLogger(__name__)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SAFE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


def parse_duration_text(text: Any) -> float:
    """Convert duration strings like '3:45' or '1:02:10' into seconds."""
    if isinstance(text, (int, float)):
        return max(0.0, float(text))
    if not isinstance(text, str):
        return 0.0
    cleaned = text.strip()
    if not cleaned:
        return 0.0
    if cleaned.isdigit():
        return float(cleaned)
    parts = cleaned.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 2:
        return float(nums[0] * 60 + nums[1])
    if len(nums) == 3:
        return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    return 0.0


def pick_best_thumbnail(thumbnails: Any, video_id: str = "") -> str:
    """Select the highest-resolution thumbnail URL available."""
    if isinstance(thumbnails, list) and thumbnails:
        best = max(
            (t for t in thumbnails if isinstance(t, dict) and t.get("url")),
            key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0),
            default=None,
        )
        if best and best.get("url"):
            return str(best["url"])
    if video_id and _SAFE_VIDEO_ID_RE.match(video_id):
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return ""


def format_artists(artists: Any) -> str:
    """Join ytmusicapi artist objects into a clean comma-separated string."""
    if isinstance(artists, str):
        return artists.strip() or "Unknown Artist"
    if isinstance(artists, list):
        names = [
            str(a["name"]).strip()
            for a in artists
            if isinstance(a, dict) and a.get("name")
        ]
        if names:
            return ", ".join(names)
    return "Unknown Artist"


def parse_youtube_url(text: str) -> dict[str, str | None] | None:
    """Parse YouTube / YouTube Music URLs or raw 11-char video IDs."""
    raw = text.strip()
    if not raw:
        return None

    if _VIDEO_ID_RE.match(raw):
        return {"type": "video", "video_id": raw, "playlist_id": None}

    if not re.match(r"^https?://", raw, re.IGNORECASE):
        if any(host in raw.lower() for host in ("youtube.com/", "youtu.be/", "music.youtube.com/")):
            raw = "https://" + raw
        else:
            return None

    try:
        parsed = urlparse(raw)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if not any(
        host == domain or host.endswith("." + domain)
        for domain in ("youtube.com", "youtu.be")
    ):
        return None

    qs = parse_qs(parsed.query)
    path_parts = [p for p in parsed.path.split("/") if p]
    playlist_id = qs.get("list", [None])[0]
    video_id = qs.get("v", [None])[0]

    if host == "youtu.be" or host.endswith(".youtu.be"):
        if path_parts and _SAFE_VIDEO_ID_RE.match(path_parts[0]):
            video_id = path_parts[0]
    elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "embed", "live", "v"):
        if _SAFE_VIDEO_ID_RE.match(path_parts[1]):
            video_id = path_parts[1]

    if video_id and _SAFE_VIDEO_ID_RE.match(video_id):
        return {"type": "video", "video_id": video_id, "playlist_id": playlist_id}

    if path_parts and path_parts[0] == "playlist" and playlist_id:
        return {"type": "playlist", "video_id": None, "playlist_id": playlist_id}

    if path_parts and path_parts[0] == "browse" and len(path_parts) >= 2:
        browse_id = path_parts[1]
        if browse_id.startswith("VL"):
            return {"type": "playlist", "video_id": None, "playlist_id": browse_id[2:]}
        return {"type": "album", "browse_id": browse_id, "video_id": None, "playlist_id": None}

    if playlist_id:
        return {"type": "playlist", "video_id": None, "playlist_id": playlist_id}

    return None


class YTMusicService:
    """Async wrapper around ytmusicapi with yt-dlp fallbacks."""

    def __init__(self) -> None:
        self._ytmusic: YTMusic | None = None

    def _get_client(self) -> YTMusic:
        if self._ytmusic is None:
            self._ytmusic = YTMusic()
        return self._ytmusic

    def _normalize_track(self, item: dict[str, Any]) -> Track | None:
        video_id = item.get("videoId")
        if not video_id or not _SAFE_VIDEO_ID_RE.match(str(video_id)):
            return None
        title = str(item.get("title") or "Untitled Track").strip()
        artist = format_artists(item.get("artists") or item.get("author"))
        album_obj = item.get("album")
        album = str(album_obj.get("name", "")) if isinstance(album_obj, dict) else str(album_obj or "")
        duration_sec = parse_duration_text(
            item.get("duration_seconds")
            or item.get("lengthSeconds")
            or item.get("duration")
            or item.get("length")
        )
        thumbnail = pick_best_thumbnail(item.get("thumbnails"), str(video_id))
        return Track(
            video_id=str(video_id),
            title=title,
            artist=artist,
            album=album,
            duration_sec=duration_sec,
            thumbnail=thumbnail,
        )

    def _search_sync(self, query: str, filter_type: str, limit: int) -> dict[str, Any]:
        yt = self._get_client()
        allowed = {"songs", "videos", "albums", "playlists"}
        yt_filter = filter_type if filter_type in allowed else "songs"
        raw_results = yt.search(query, filter=yt_filter, limit=limit)

        tracks: list[dict[str, Any]] = []
        collections: list[dict[str, Any]] = []

        for item in raw_results:
            if not isinstance(item, dict):
                continue
            if yt_filter in ("songs", "videos"):
                track = self._normalize_track(item)
                if track:
                    tracks.append(track.model_dump())
            elif yt_filter == "albums":
                browse_id = item.get("browseId")
                if browse_id:
                    collections.append(
                        {
                            "kind": "album",
                            "id": str(browse_id),
                            "title": str(item.get("title") or "Untitled Album"),
                            "subtitle": format_artists(item.get("artists")),
                            "year": str(item.get("year") or ""),
                            "thumbnail": pick_best_thumbnail(item.get("thumbnails")),
                        }
                    )
            elif yt_filter == "playlists":
                browse_id = item.get("browseId") or ""
                playlist_id = browse_id[2:] if browse_id.startswith("VL") else browse_id
                if playlist_id:
                    collections.append(
                        {
                            "kind": "playlist",
                            "id": str(playlist_id),
                            "title": str(item.get("title") or "Untitled Playlist"),
                            "subtitle": str(item.get("author") or "YouTube Music"),
                            "item_count": str(item.get("itemCount") or ""),
                            "thumbnail": pick_best_thumbnail(item.get("thumbnails")),
                        }
                    )

        return {"filter": yt_filter, "tracks": tracks, "collections": collections}

    def _ytdlp_search_fallback(self, query: str, limit: int) -> dict[str, Any]:
        import yt_dlp  # type: ignore[import-untyped]

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        tracks: list[dict[str, Any]] = []
        for entry in (info or {}).get("entries") or []:
            if not isinstance(entry, dict):
                continue
            vid = entry.get("id")
            if not vid or not _SAFE_VIDEO_ID_RE.match(str(vid)):
                continue
            tracks.append(
                Track(
                    video_id=str(vid),
                    title=str(entry.get("title") or "Untitled Track"),
                    artist=str(entry.get("uploader") or entry.get("channel") or "YouTube"),
                    duration_sec=float(entry.get("duration") or 0.0),
                    thumbnail=pick_best_thumbnail(entry.get("thumbnails"), str(vid)),
                ).model_dump()
            )
        return {"filter": "songs", "tracks": tracks, "collections": []}

    def _get_track_info_sync(self, video_id: str) -> Track:
        try:
            yt = self._get_client()
            song = yt.get_song(video_id)
            details = song.get("videoDetails") if isinstance(song, dict) else None
            if isinstance(details, dict) and details.get("videoId"):
                thumbs = (details.get("thumbnail") or {}).get("thumbnails")
                return Track(
                    video_id=str(details["videoId"]),
                    title=str(details.get("title") or "Untitled Track"),
                    artist=str(details.get("author") or "Unknown Artist"),
                    duration_sec=parse_duration_text(details.get("lengthSeconds")),
                    thumbnail=pick_best_thumbnail(thumbs, video_id),
                )
        except Exception as exc:
            logger.debug("ytmusic get_song fallback for %s: %s", video_id, exc)

        import yt_dlp  # type: ignore[import-untyped]

        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        return Track(
            video_id=video_id,
            title=str((info or {}).get("title") or "Untitled Track"),
            artist=str((info or {}).get("artist") or (info or {}).get("uploader") or "Unknown Artist"),
            duration_sec=float((info or {}).get("duration") or 0.0),
            thumbnail=str((info or {}).get("thumbnail") or pick_best_thumbnail([], video_id)),
        )

    def _get_playlist_sync(self, playlist_id: str, limit: int) -> dict[str, Any]:
        try:
            yt = self._get_client()
            pl = yt.get_playlist(playlist_id, limit=limit)
            tracks: list[dict[str, Any]] = []
            for item in pl.get("tracks") or []:
                track = self._normalize_track(item)
                if track:
                    tracks.append(track.model_dump())
            if tracks:
                return {
                    "id": playlist_id,
                    "title": str(pl.get("title") or "Playlist"),
                    "subtitle": format_artists(pl.get("author")),
                    "thumbnail": pick_best_thumbnail(pl.get("thumbnails")),
                    "tracks": tracks,
                }
        except Exception as exc:
            logger.debug("ytmusic get_playlist fallback for %s: %s", playlist_id, exc)

        import yt_dlp  # type: ignore[import-untyped]

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "playlistend": limit,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/playlist?list={playlist_id}",
                download=False,
            )
        tracks = []
        for entry in (info or {}).get("entries") or []:
            if not isinstance(entry, dict):
                continue
            vid = entry.get("id")
            if not vid or not _SAFE_VIDEO_ID_RE.match(str(vid)):
                continue
            tracks.append(
                Track(
                    video_id=str(vid),
                    title=str(entry.get("title") or "Untitled Track"),
                    artist=str(entry.get("uploader") or "YouTube"),
                    duration_sec=float(entry.get("duration") or 0.0),
                    thumbnail=pick_best_thumbnail(entry.get("thumbnails"), str(vid)),
                ).model_dump()
            )
        return {
            "id": playlist_id,
            "title": str((info or {}).get("title") or "Playlist"),
            "subtitle": str((info or {}).get("uploader") or "YouTube"),
            "thumbnail": tracks[0]["thumbnail"] if tracks else "",
            "tracks": tracks,
        }

    def _get_album_sync(self, browse_id: str) -> dict[str, Any]:
        yt = self._get_client()
        alb = yt.get_album(browse_id)
        album_title = str(alb.get("title") or "Album")
        album_artist = format_artists(alb.get("artists"))
        album_thumb = pick_best_thumbnail(alb.get("thumbnails"))
        tracks: list[dict[str, Any]] = []
        for item in alb.get("tracks") or []:
            track = self._normalize_track(item)
            if track:
                if not track.album:
                    track.album = album_title
                if track.artist == "Unknown Artist":
                    track.artist = album_artist
                if not track.thumbnail:
                    track.thumbnail = album_thumb
                tracks.append(track.model_dump())
        return {
            "id": browse_id,
            "title": album_title,
            "subtitle": album_artist,
            "thumbnail": album_thumb,
            "tracks": tracks,
        }

    def _get_related_sync(self, video_id: str, limit: int) -> list[dict[str, Any]]:
        yt = self._get_client()
        watch = yt.get_watch_playlist(videoId=video_id, limit=limit)
        tracks: list[dict[str, Any]] = []
        for item in watch.get("tracks") or []:
            track = self._normalize_track(item)
            if track and track.video_id != video_id:
                tracks.append(track.model_dump())
        return tracks

    async def search(
        self, query: str, filter_type: str = "songs", limit: int = 20
    ) -> dict[str, Any]:
        """Search YouTube Music or directly resolve a pasted YouTube / YT Music URL."""
        cleaned = query.strip()
        if not cleaned:
            return {"filter": filter_type, "tracks": [], "collections": []}

        parsed = parse_youtube_url(cleaned)
        if parsed:
            kind = parsed.get("type")
            if kind == "video" and parsed.get("video_id"):
                track = await self.get_track_info(str(parsed["video_id"]))
                return {
                    "filter": "songs",
                    "resolved_url": True,
                    "tracks": [track.model_dump()],
                    "collections": [],
                }
            if kind == "playlist" and parsed.get("playlist_id"):
                pl = await self.get_playlist_tracks(str(parsed["playlist_id"]), limit=50)
                return {
                    "filter": "songs",
                    "resolved_url": True,
                    "playlist_title": pl["title"],
                    "tracks": pl["tracks"],
                    "collections": [],
                }
            if kind == "album" and parsed.get("browse_id"):
                alb = await self.get_album_tracks(str(parsed["browse_id"]))
                return {
                    "filter": "songs",
                    "resolved_url": True,
                    "playlist_title": alb["title"],
                    "tracks": alb["tracks"],
                    "collections": [],
                }

        try:
            return await asyncio.to_thread(self._search_sync, cleaned, filter_type, limit)
        except Exception as exc:
            logger.warning("ytmusicapi search failed (%s), falling back to yt-dlp", exc)
            return await asyncio.to_thread(self._ytdlp_search_fallback, cleaned, limit)

    async def get_track_info(self, video_id: str) -> Track:
        """Fetch metadata for a single video ID."""
        return await asyncio.to_thread(self._get_track_info_sync, video_id)

    async def get_playlist_tracks(self, playlist_id: str, limit: int = 50) -> dict[str, Any]:
        """Fetch playlist metadata and tracks."""
        return await asyncio.to_thread(self._get_playlist_sync, playlist_id, limit)

    async def get_album_tracks(self, browse_id: str) -> dict[str, Any]:
        """Fetch album metadata and tracks."""
        return await asyncio.to_thread(self._get_album_sync, browse_id)

    async def get_related_tracks(self, video_id: str, limit: int = 15) -> list[dict[str, Any]]:
        """Fetch YouTube Music Radio / Up Next recommendations for a track."""
        return await asyncio.to_thread(self._get_related_sync, video_id, limit)

    def _get_lyrics_sync(self, video_id: str) -> dict[str, Any]:
        try:
            yt = self._get_client()
            watch = yt.get_watch_playlist(videoId=video_id, limit=1)
            lyrics_id = (watch or {}).get("lyrics")
            if not lyrics_id:
                return {"video_id": video_id, "lyrics": "", "source": "", "available": False}
            data = yt.get_lyrics(lyrics_id)
            text = str((data or {}).get("lyrics") or "").strip()
            source = str((data or {}).get("source") or "YouTube Music")
            return {
                "video_id": video_id,
                "lyrics": text,
                "source": source,
                "available": bool(text),
            }
        except Exception as exc:
            logger.debug("Lyrics unavailable for %s: %s", video_id, exc)
            return {"video_id": video_id, "lyrics": "", "source": "", "available": False}

    async def get_lyrics(self, video_id: str) -> dict[str, Any]:
        """Fetch YouTube Music lyrics for a track."""
        return await asyncio.to_thread(self._get_lyrics_sync, video_id)

"""Disk-backed audio cache with concurrent download deduplication and LRU eviction."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from backend import config
from backend.config import (
    CACHE_DIR,
    MAX_CACHE_BYTES,
    MAX_CACHE_FILES,
    find_deno_executable,
)

logger = logging.getLogger(__name__)

_SAFE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

MIME_BY_EXT: dict[str, str] = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".webm": "audio/webm",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
}

AUDIO_EXTS: tuple[str, ...] = tuple(MIME_BY_EXT.keys())


def validate_video_id(video_id: str) -> str:
    """Validate a YouTube video ID to prevent path traversal."""
    if not video_id or not _SAFE_VIDEO_ID_RE.match(video_id):
        raise ValueError(f"Invalid video ID: {video_id!r}")
    return video_id


class AudioCache:
    """Manages cached audio tracks downloaded via yt-dlp + Deno."""

    def __init__(
        self,
        cache_dir: Path = CACHE_DIR,
        max_files: int = MAX_CACHE_FILES,
        max_bytes: int = MAX_CACHE_BYTES,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.deno_path = find_deno_executable()
        self._in_flight: dict[str, asyncio.Task[dict[str, Any]]] = {}

        if self.deno_path:
            deno_dir = str(Path(self.deno_path).parent)
            current_path = os.environ.get("PATH", "")
            if deno_dir not in current_path.split(os.pathsep):
                os.environ["PATH"] = f"{deno_dir}{os.pathsep}{current_path}"

    def _meta_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.meta.json"

    def get_cached_path(self, video_id: str) -> tuple[Path, str, float] | None:
        """Return (audio_path, mime_type, duration_sec) if cached on disk."""
        validate_video_id(video_id)
        for ext in AUDIO_EXTS:
            candidate = self.cache_dir / f"{video_id}{ext}"
            if candidate.is_file() and candidate.stat().st_size > 0:
                duration = 0.0
                meta_file = self._meta_path(video_id)
                if meta_file.is_file():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        duration = float(meta.get("duration_sec") or 0.0)
                    except Exception:
                        pass
                return candidate, MIME_BY_EXT[ext], duration
        return None

    def is_cached(self, video_id: str) -> bool:
        try:
            return self.get_cached_path(video_id) is not None
        except ValueError:
            return False

    def _touch_file(self, path: Path) -> None:
        try:
            os.utime(path, None)
        except OSError:
            pass

    def _download_sync(self, video_id: str) -> dict[str, Any]:
        import yt_dlp  # type: ignore[import-untyped]

        url = f"https://www.youtube.com/watch?v={video_id}"
        outtmpl = str(self.cache_dir / f"{video_id}.%(ext)s")

        ydl_opts: dict[str, Any] = {
            "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio[ext=webm]/bestaudio/best[ext=mp4]/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "socket_timeout": 15,
        }
        if self.deno_path:
            ydl_opts["js_runtimes"] = {"deno": {"path": self.deno_path}}
        cookiefile = config.get_ytdlp_cookiefile()
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception:
            clients = ["web", "mweb", "tv"] if cookiefile else ["android_vr", "ios", "tv", "mweb"]
            fallback_opts = {
                **ydl_opts,
                "format": "bestaudio/best[ext=mp4]/best",
                "extractor_args": {
                    "youtube": {"player_client": clients}
                },
            }
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                info = ydl.extract_info(url, download=True)

        cached = self.get_cached_path(video_id)
        if not cached:
            # Handle rare cases where yt-dlp saved another audio container extension
            matches = [
                p
                for p in self.cache_dir.glob(f"{video_id}.*")
                if not p.name.endswith(".meta.json") and not p.name.endswith(".part")
            ]
            if not matches:
                raise RuntimeError(f"Audio download produced no file for {video_id}")
            audio_path = matches[0]
            mime = MIME_BY_EXT.get(audio_path.suffix.lower(), "audio/mp4")
        else:
            audio_path, mime, _ = cached

        duration_sec = float((info or {}).get("duration") or 0.0)
        title = str((info or {}).get("title") or "")
        artist = str((info or {}).get("artist") or (info or {}).get("uploader") or "")

        meta = {
            "video_id": video_id,
            "title": title,
            "artist": artist,
            "duration_sec": duration_sec,
            "ext": audio_path.suffix.lower(),
            "mime_type": mime,
        }
        self._meta_path(video_id).write_text(json.dumps(meta), encoding="utf-8")
        self._touch_file(audio_path)
        self._prune_cache(protected_id=video_id)

        return {
            "video_id": video_id,
            "path": audio_path,
            "mime_type": mime,
            "duration_sec": duration_sec,
        }

    def _prune_cache(self, protected_id: str | None = None) -> None:
        audio_files: list[Path] = [
            p
            for p in self.cache_dir.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS
        ]
        if not audio_files:
            return

        total_bytes = sum(p.stat().st_size for p in audio_files)
        if len(audio_files) <= self.max_files and total_bytes <= self.max_bytes:
            return

        # Oldest mtime first (LRU)
        audio_files.sort(key=lambda p: p.stat().st_mtime)
        remaining_count = len(audio_files)

        for path in audio_files:
            if remaining_count <= self.max_files and total_bytes <= self.max_bytes:
                break
            vid = path.stem
            if protected_id and vid == protected_id:
                continue
            size = path.stat().st_size
            try:
                path.unlink(missing_ok=True)
                self._meta_path(vid).unlink(missing_ok=True)
                total_bytes -= size
                remaining_count -= 1
            except OSError as exc:
                logger.debug("Cache prune skip %s: %s", path.name, exc)

    async def ensure_track(self, video_id: str) -> dict[str, Any]:
        """Return cached audio metadata or download once (deduplicated across callers)."""
        validate_video_id(video_id)
        existing = self.get_cached_path(video_id)
        if existing:
            path, mime, duration = existing
            self._touch_file(path)
            return {
                "video_id": video_id,
                "path": path,
                "mime_type": mime,
                "duration_sec": duration,
            }

        task = self._in_flight.get(video_id)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(self._download_sync, video_id))
            self._in_flight[video_id] = task

        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._in_flight.get(video_id) is task:
                self._in_flight.pop(video_id, None)

    def prefetch(self, video_id: str) -> None:
        """Fire-and-forget background prefetch for upcoming queue tracks."""
        try:
            validate_video_id(video_id)
        except ValueError:
            return
        if self.is_cached(video_id) or video_id in self._in_flight:
            return

        async def _bg() -> None:
            try:
                await self.ensure_track(video_id)
            except Exception as exc:
                logger.debug("Prefetch failed for %s: %s", video_id, exc)

        asyncio.create_task(_bg())

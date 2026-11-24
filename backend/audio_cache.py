"""Disk-backed audio cache with concurrent download deduplication and LRU eviction."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

from backend import config
from backend.config import (
    CACHE_DIR,
    MAX_CACHE_BYTES,
    MAX_CACHE_FILES,
    find_deno_executable,
)
from backend.security import validate_audio_magic_bytes

logger = logging.getLogger(__name__)

_SAFE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

try:
    import yt_dlp_ejs as _yt_dlp_ejs  # type: ignore[import-untyped]  # noqa: F401

    _HAS_BUNDLED_EJS = True
except ImportError:
    _HAS_BUNDLED_EJS = False

MIME_BY_EXT: dict[str, str] = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".webm": "audio/webm",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}

UPLOAD_EXTS: set[str] = {".mp3", ".m4a", ".wav", ".ogg", ".webm", ".mp4", ".flac"}

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
        self.ffmpeg_path = shutil.which("ffmpeg")
        self._in_flight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._fallback_queries: dict[str, str] = {}
        self._download_sem = asyncio.Semaphore(2)

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

    @staticmethod
    def _reject_live_or_huge(
        info_dict: dict[str, Any], *, incomplete: bool = False
    ) -> str | None:
        _ = incomplete
        if info_dict.get("is_live") or info_dict.get("live_status") == "is_live":
            return "Live streams are not supported for cached sync playback."
        dur = float(info_dict.get("duration") or 0.0)
        if dur > 14400:  # 4 hours max
            return "Track exceeds maximum allowed duration (4 hours)."
        return None

    def _build_base_ydl_opts(self, outtmpl: str, use_cookies: bool = True) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "format": "bestaudio[ext=m4a][abr<=140]/bestaudio[ext=webm][abr<=140]/bestaudio[abr<=160]/18/bestaudio*/best*",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 1,
            "fragment_retries": 1,
            "socket_timeout": 6,
            "max_filesize": config.MAX_AUDIO_DOWNLOAD_BYTES,
            "match_filter": self._reject_live_or_huge,
        }
        if self.deno_path:
            opts["js_runtimes"] = {"deno": {"path": self.deno_path}}
            if not _HAS_BUNDLED_EJS:
                opts["remote_components"] = ["ejs:github"]
        if use_cookies:
            cookiefile = config.get_ytdlp_cookiefile()
            if cookiefile:
                opts["cookiefile"] = cookiefile
        return opts

    def _pick_best_format_id(self, formats: list[dict[str, Any]]) -> str | None:
        """Select the best playable audio (or muxed fallback) format_id directly from metadata."""
        playable = [
            f
            for f in formats
            if f.get("format_id")
            and not str(f.get("format_id", "")).startswith("sb")
            and f.get("ext") != "mhtml"
            and f.get("protocol") != "mhtml"
        ]
        if not playable:
            return None

        def _score(fmt: dict[str, Any]) -> tuple[int, int, float]:
            vcodec = str(fmt.get("vcodec") or "none")
            acodec = str(fmt.get("acodec") or "none")
            ext = str(fmt.get("ext") or "")
            proto = str(fmt.get("protocol") or "")
            is_audio_only = 1 if vcodec == "none" else 0
            has_audio = 1 if acodec != "none" else 0
            direct_https = 1 if proto in ("https", "http") else 0
            good_ext = 2 if ext in ("m4a", "webm", "opus") else (1 if ext == "mp4" else 0)
            abr = float(fmt.get("abr") or fmt.get("tbr") or 0.0)
            return (is_audio_only * 2 + has_audio, good_ext + direct_https, abr)

        playable.sort(key=_score, reverse=True)
        return str(playable[0]["format_id"])

    def _download_single_id(self, source_id: str, target_id: str) -> dict[str, Any]:
        """Run fast 2-client yt-dlp extraction for `source_id`, saving under `target_id`."""
        import yt_dlp  # type: ignore[import-untyped]

        url = f"https://www.youtube.com/watch?v={source_id}"
        outtmpl = str(self.cache_dir / f"{target_id}.%(ext)s")
        has_cookies = bool(config.get_ytdlp_cookiefile())

        def _cleanup_partials() -> None:
            for p in self.cache_dir.glob(f"{target_id}.*.part"):
                p.unlink(missing_ok=True)

        strategies: list[dict[str, Any]] = []
        if has_cookies:
            s1 = self._build_base_ydl_opts(outtmpl, use_cookies=True)
            s1["extractor_args"] = {"youtube": {"player_client": ["web"]}}
            strategies.append(s1)

        # Cookie-free android_vr typically resolves in ~1.6s when web cookies are throttled
        s2 = self._build_base_ydl_opts(outtmpl, use_cookies=False)
        s2["extractor_args"] = {"youtube": {"player_client": ["android_vr"]}}
        strategies.append(s2)

        last_exc: Exception | None = None
        for opts in strategies:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                if info is not None:
                    return dict(info)
            except Exception as exc:
                last_exc = exc
                _cleanup_partials()

        assert last_exc is not None
        raise last_exc

    def _download_sync(
        self, video_id: str, fallback_query: str = ""
    ) -> dict[str, Any]:
        import yt_dlp  # type: ignore[import-untyped]

        info: dict[str, Any] | None = None
        last_exc: Exception | None = None
        try:
            info = self._download_single_id(video_id, video_id)
        except Exception as exc:
            last_exc = exc

        # Stage 5: Alternate YouTube upload search fallback when original ID is blocked
        if info is None and fallback_query.strip():
            search_opts = self._build_base_ydl_opts(
                str(self.cache_dir / f"{video_id}.%(ext)s"), use_cookies=False
            )
            search_opts["extract_flat"] = "in_playlist"
            try:
                with yt_dlp.YoutubeDL(search_opts) as ydl:
                    sr = ydl.extract_info(
                        f"ytsearch3:{fallback_query.strip()} audio", download=False
                    )
                for entry in (sr or {}).get("entries") or []:
                    alt_id = str((entry or {}).get("id") or "").strip()
                    if not alt_id or alt_id == video_id:
                        continue
                    try:
                        validate_video_id(alt_id)
                    except ValueError:
                        continue
                    try:
                        info = self._download_single_id(alt_id, video_id)
                        logger.info(
                            "Recovered blocked track %s via alternate upload %s",
                            video_id,
                            alt_id,
                        )
                        break
                    except Exception as alt_exc:
                        last_exc = alt_exc
                        continue
            except Exception as sr_exc:
                logger.debug("Fallback search failed for %s: %s", video_id, sr_exc)

        if info is None:
            assert last_exc is not None
            raise last_exc

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

    def set_fallback_query(self, video_id: str, query: str) -> None:
        """Register optional title/artist fallback search for `video_id`."""
        cleaned = (query or "").strip()
        if cleaned:
            if len(self._fallback_queries) >= 200:
                self._fallback_queries.pop(next(iter(self._fallback_queries)), None)
            self._fallback_queries[video_id] = cleaned

    async def ensure_track(
        self, video_id: str, fallback_query: str = ""
    ) -> dict[str, Any]:
        """Return cached audio metadata or download once (deduplicated across callers)."""
        validate_video_id(video_id)
        resolved_query = fallback_query or self._fallback_queries.pop(video_id, "")
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

        if video_id.startswith("local_"):
            raise FileNotFoundError("Uploaded local track expired from cache.")

        task = self._in_flight.get(video_id)
        if task is None:
            async def _run_bounded() -> dict[str, Any]:
                async with self._download_sem:
                    if resolved_query:
                        return await asyncio.to_thread(
                            self._download_sync, video_id, resolved_query
                        )
                    return await asyncio.to_thread(self._download_sync, video_id)

            task = asyncio.create_task(_run_bounded())
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

    async def save_uploaded_audio(
        self, filename: str, content: bytes, title: str = ""
    ) -> dict[str, Any]:
        """Persist a locally uploaded MP3/WAV/M4A/OGG/FLAC track into the cache."""
        if not content or len(content) > config.MAX_UPLOAD_BYTES:
            raise ValueError("Uploaded file is empty or exceeds 30 MB limit.")
        ext = Path(filename or "track.mp3").suffix.lower()
        if ext not in UPLOAD_EXTS:
            raise ValueError(f"Unsupported audio format: {ext}")
        if not validate_audio_magic_bytes(ext, content):
            raise ValueError("Uploaded file failed audio header validation.")

        video_id = f"local_{secrets.token_hex(6)}"
        dest = self.cache_dir / f"{video_id}{ext}"
        await asyncio.to_thread(dest.write_bytes, content)

        clean_title = (title or Path(filename or "Uploaded Track").stem)[:80]
        mime = MIME_BY_EXT[ext]
        meta = {
            "video_id": video_id,
            "title": clean_title,
            "artist": "Local Upload",
            "duration_sec": 0.0,
            "ext": ext,
            "mime_type": mime,
        }
        self._meta_path(video_id).write_text(json.dumps(meta), encoding="utf-8")
        self._touch_file(dest)
        self._prune_cache(protected_id=video_id)
        return {
            "video_id": video_id,
            "title": clean_title,
            "artist": "Local Upload",
            "path": dest,
            "mime_type": mime,
        }

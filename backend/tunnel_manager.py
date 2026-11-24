"""Free instant HTTPS public link via official Cloudflare Quick Tunnel (cloudflared)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import platform
import re
import shutil
import stat
from pathlib import Path
from typing import Any

import httpx

from backend import config

logger = logging.getLogger(__name__)

_TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


class TunnelManager:
    """Manages a zero-signup Cloudflare Quick Tunnel pointing to the local SoundMesh server."""

    def __init__(self, bin_dir: Path | None = None) -> None:
        self.bin_dir = Path(bin_dir) if bin_dir else config.BIN_DIR
        self._process: asyncio.subprocess.Process | None = None
        self._drain_tasks: list[asyncio.Task[None]] = []
        self._start_lock = asyncio.Lock()

        self.public_url: str = ""
        self.starting: bool = False
        self.error: str = ""

    def find_cloudflared(self) -> Path | None:
        """Locate cloudflared on PATH or inside .bin/."""
        on_path = shutil.which("cloudflared")
        if on_path:
            return Path(on_path)
        for name in ("cloudflared.exe", "cloudflared"):
            candidate = self.bin_dir / name
            if candidate.is_file() and candidate.stat().st_size > 1_000_000:
                return candidate
        return None

    async def ensure_cloudflared(self) -> Path:
        """Return existing cloudflared binary or download official release into .bin/."""
        existing = self.find_cloudflared()
        if existing:
            return existing

        self.bin_dir.mkdir(parents=True, exist_ok=True)
        system = platform.system().lower()
        machine = platform.machine().lower()

        if system == "windows":
            asset = "cloudflared-windows-amd64.exe"
            dest = self.bin_dir / "cloudflared.exe"
        elif system == "darwin":
            asset = "cloudflared-darwin-arm64.tgz" if "arm" in machine else "cloudflared-darwin-amd64.tgz"
            dest = self.bin_dir / "cloudflared"
        else:
            asset = (
                "cloudflared-linux-arm64"
                if ("arm64" in machine or "aarch64" in machine)
                else "cloudflared-linux-amd64"
            )
            dest = self.bin_dir / "cloudflared"

        if asset.endswith(".tgz"):
            raise RuntimeError("Install cloudflared via 'brew install cloudflared' on macOS.")

        download_url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
        checksum_url = f"{download_url}.sha256"
        tmp_dest = dest.with_suffix(".download")

        logger.info("Downloading official cloudflared from %s...", download_url)
        hasher = hashlib.sha256()
        async with httpx.AsyncClient(follow_redirects=True, timeout=90.0) as client:
            sha_resp = await client.get(checksum_url)
            sha_resp.raise_for_status()
            sha_tokens = sha_resp.text.strip().split()
            expected_sha = sha_tokens[0].lower() if sha_tokens else ""
            if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
                raise RuntimeError("Invalid SHA-256 manifest returned for cloudflared release.")

            async with client.stream("GET", download_url) as resp:
                resp.raise_for_status()
                with tmp_dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=131072):
                        hasher.update(chunk)
                        f.write(chunk)

        if not tmp_dest.is_file() or tmp_dest.stat().st_size < 1_000_000:
            tmp_dest.unlink(missing_ok=True)
            raise RuntimeError("Downloaded cloudflared binary was incomplete.")

        actual_sha = hasher.hexdigest().lower()
        if not hmac.compare_digest(actual_sha, expected_sha):
            tmp_dest.unlink(missing_ok=True)
            raise RuntimeError("SHA-256 checksum verification failed for downloaded cloudflared binary.")

        tmp_dest.replace(dest)
        if os.name != "nt":
            st = dest.stat()
            dest.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return dest

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def status(self) -> dict[str, Any]:
        return {
            "active": self.is_running() and bool(self.public_url),
            "public_url": self.public_url,
            "installed": self.find_cloudflared() is not None,
            "starting": self.starting,
            "error": self.error,
        }

    async def _drain_stream(
        self,
        stream: asyncio.StreamReader | None,
        url_future: asyncio.Future[str] | None,
    ) -> None:
        if stream is None:
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if url_future is not None and not url_future.done():
                    m = _TRYCLOUDFLARE_RE.search(line)
                    if m:
                        url_future.set_result(m.group(0).rstrip("/"))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def start(self, port: int) -> dict[str, Any]:
        """Start cloudflared Quick Tunnel and resolve its public https://*.trycloudflare.com URL."""
        async with self._start_lock:
            if self.is_running() and self.public_url:
                return self.status()

            await self._cleanup_process()
            self.starting = True
            self.error = ""
            self.public_url = ""
            config.set_runtime_public_url("")

            try:
                bin_path = await self.ensure_cloudflared()
                proc = await asyncio.create_subprocess_exec(
                    str(bin_path),
                    "tunnel",
                    "--url",
                    f"http://127.0.0.1:{port}",
                    "--no-autoupdate",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._process = proc

                loop = asyncio.get_running_loop()
                url_future: asyncio.Future[str] = loop.create_future()

                self._drain_tasks = [
                    asyncio.create_task(self._drain_stream(proc.stdout, url_future)),
                    asyncio.create_task(self._drain_stream(proc.stderr, url_future)),
                ]

                url = await asyncio.wait_for(url_future, timeout=25.0)
                self.public_url = url
                config.set_runtime_public_url(url)
                logger.info("Cloudflare Quick Tunnel LIVE: %s", url)
            except Exception as exc:
                self.error = str(exc) or "Failed to establish Cloudflare tunnel."
                logger.warning("Tunnel start failed: %s", self.error)
                await self._cleanup_process()
            finally:
                self.starting = False

            return self.status()

    async def _cleanup_process(self) -> None:
        for t in self._drain_tasks:
            if not t.done():
                t.cancel()
        self._drain_tasks.clear()

        if self._process is not None:
            if self._process.returncode is None:
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=3.0)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
            self._process = None

    async def stop(self) -> dict[str, Any]:
        """Stop the running Cloudflare Quick Tunnel."""
        async with self._start_lock:
            await self._cleanup_process()
            self.public_url = ""
            self.starting = False
            self.error = ""
            config.set_runtime_public_url("")
            return self.status()

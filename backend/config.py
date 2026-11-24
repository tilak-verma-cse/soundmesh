"""Configuration, paths, .env loading, network discovery, and timing constants for SoundMesh."""

from __future__ import annotations

import base64
import ipaddress
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
FRONTEND_DIR: Path = PROJECT_ROOT / "frontend"
CACHE_DIR: Path = PROJECT_ROOT / ".cache" / "audio"
DATA_DIR: Path = PROJECT_ROOT / ".data"
BIN_DIR: Path = PROJECT_ROOT / ".bin"


def _load_dotenv(env_path: Path) -> None:
    """Load simple KEY=VALUE lines from .env without overriding existing environment vars."""
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_dotenv(PROJECT_ROOT / ".env")

HOST: str = "0.0.0.0"
PORT: int = int(os.environ.get("PORT") or os.environ.get("SOUNDMESH_PORT") or "8765")
DB_PATH: Path = Path(os.environ.get("SOUNDMESH_DB_PATH", str(DATA_DIR / "soundmesh.db")))
DATABASE_URL: str = os.environ.get("DATABASE_URL", "").strip()
PUBLIC_URL: str = os.environ.get("SOUNDMESH_PUBLIC_URL", "").strip().rstrip("/")
FRONTEND_URL: str = os.environ.get("SOUNDMESH_FRONTEND_URL", "").strip().rstrip("/")
AUTO_TUNNEL: bool = os.environ.get("SOUNDMESH_AUTO_TUNNEL", "").strip().lower() in (
    "1",
    "true",
    "yes",
)


def get_allowed_origins() -> list[str]:
    """Return configured CORS origins plus local dev origins and FRONTEND_URL."""
    raw = os.environ.get("SOUNDMESH_ALLOWED_ORIGINS", "").strip()
    origins: list[str] = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    if FRONTEND_URL and FRONTEND_URL not in origins:
        origins.append(FRONTEND_URL)
    for local in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
        if local not in origins:
            origins.append(local)
    return origins


GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET: str = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_ENABLE_YOUTUBE_SCOPE: bool = os.environ.get(
    "GOOGLE_ENABLE_YOUTUBE_SCOPE", "true"
).strip().lower() in ("1", "true", "yes")

SESSION_COOKIE_NAME: str = "soundmesh_session"
SESSION_TTL_SEC: int = 30 * 24 * 3600  # 30 days

# Audio cache & upload limits
MAX_CACHE_FILES: int = 50
MAX_CACHE_BYTES: int = int(os.environ.get("SOUNDMESH_MAX_CACHE_MB", "500")) * 1024 * 1024
MAX_UPLOAD_BYTES: int = 30 * 1024 * 1024  # 30 MB
MAX_AUDIO_DOWNLOAD_BYTES: int = int(
    os.environ.get("SOUNDMESH_MAX_DOWNLOAD_MB", "80")
) * 1024 * 1024

_runtime_public_url: str = ""
_cached_cookie_digest: str = ""
_cached_cookie_path: str = ""


def set_runtime_public_url(url: str) -> None:
    """Store the active Cloudflare Quick Tunnel public HTTPS URL."""
    global _runtime_public_url
    _runtime_public_url = url.strip().rstrip("/")


def get_public_url() -> str:
    """Return configured SOUNDMESH_PUBLIC_URL or active runtime tunnel URL."""
    return PUBLIC_URL or _runtime_public_url


def get_ytdlp_cookiefile() -> str | None:
    """Return a clean, uncorrupted path to YouTube cookies from YTDLP_COOKIES_FILE or YTDLP_COOKIES_B64."""
    direct_path = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    if direct_path and Path(direct_path).is_file():
        return direct_path

    b64 = os.environ.get("YTDLP_COOKIES_B64", "").strip()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None

    for target_dir in (DATA_DIR, Path(tempfile.gettempdir())):
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            cookie_path = target_dir / "yt_cookies.txt"
            # Always restore fresh cookie bytes so yt-dlp cannot mutate/corrupt them across runs
            cookie_path.write_bytes(raw)
            try:
                os.chmod(cookie_path, 0o600)
            except OSError:
                pass
            return str(cookie_path)
        except OSError:
            continue
    return None

# Synchronization timing constants
PLAY_LEAD_MS: float = 1000.0
READY_BARRIER_TIMEOUT_SEC: float = 6.0
SPEAKER_STALE_TIMEOUT_MS: float = 22000.0
STREAMING_THRESHOLD_SEC: int = 720  # 12 minutes
CALIBRATION_INTERVAL_MS: float = 1000.0


def get_secret_key() -> str:
    """Return configured secret key or persist an auto-generated 32-byte hex secret."""
    env_key = os.environ.get("SOUNDMESH_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    secret_file = DATA_DIR / ".secret_key"
    if secret_file.is_file():
        existing = secret_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    generated = secrets.token_hex(32)
    secret_file.write_text(generated, encoding="utf-8")
    return generated


def google_oauth_configured() -> bool:
    """Return True when both Google OAuth client ID and secret are configured."""
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def resolve_base_url(request: Request) -> str:
    """Resolve external base URL with strict host/scheme validation."""
    pub = get_public_url()
    if pub:
        return pub
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    raw_scheme = forwarded_proto.split(",")[0].strip().lower() if forwarded_proto else request.url.scheme.lower()
    scheme = "https" if raw_scheme == "https" else "http"

    forwarded_host = request.headers.get("x-forwarded-host", "")
    candidate_host = (
        forwarded_host.split(",")[0].strip()
        if forwarded_host
        else (request.headers.get("host") or f"localhost:{PORT}")
    )
    # Reject malformed/injected host headers (CR/LF, paths, credentials, spaces)
    import re

    if not re.match(r"^[A-Za-z0-9._:\[\]-]{1,253}$", candidate_host):
        candidate_host = f"localhost:{PORT}"
        scheme = "http"
    return f"{scheme}://{candidate_host}"


def is_private_lan_host(host_header: str) -> bool:
    """Return True if host is a private LAN IPv4 address (Google OAuth rejects bare LAN IPs)."""
    hostname = host_header.split(":")[0].strip("[]").lower()
    if not hostname or hostname in ("localhost", "127.0.0.1", "::1", "testserver"):
        return False
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private and not ip.is_loopback
    except ValueError:
        return False


def server_now_ms() -> float:
    """Return monotonic server timestamp in milliseconds."""
    return time.perf_counter() * 1000.0


def get_lan_ips() -> list[str]:
    """Return deduplicated local IPv4 addresses, prioritizing the primary outbound LAN IP."""
    ips: list[str] = []

    def _add(ip: str) -> None:
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip not in ips:
            ips.append(ip)

    # 1. Primary outbound interface (UDP connect never sends packets)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            _add(sock.getsockname()[0])
    except OSError:
        pass

    # 2. Additional local IPv4 interfaces
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if isinstance(addr, str):
                _add(addr)
    except OSError:
        pass

    return ips if ips else ["127.0.0.1"]


def find_deno_executable() -> str | None:
    """Locate the Deno binary from the pip deno package, PATH, or venv Scripts."""
    try:
        import deno  # type: ignore[import-untyped]

        deno_bin = deno.find_deno_bin()
        if deno_bin and Path(deno_bin).exists():
            return str(deno_bin)
    except Exception:
        pass

    which_deno = shutil.which("deno")
    if which_deno:
        return which_deno

    exe_dir = Path(sys.executable).parent
    for candidate_name in ("deno.exe", "deno"):
        candidate = exe_dir / candidate_name
        if candidate.exists():
            return str(candidate)

    return None

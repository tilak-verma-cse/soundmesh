"""Cryptographic helpers, rate limiting, audio header checks, and origin/URL guards."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlparse

from backend import config

_ENC_PREFIX = "enc:v1:"
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,253}$")
_SAFE_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,36}$")

TRUSTED_THUMB_HOSTS: tuple[str, ...] = (
    "ytimg.com",
    "ggpht.com",
    "googleusercontent.com",
    "youtube.com",
)


# ------------------------------------------------------------------
# Authenticated Encryption at Rest (HKDF + SHAKE256-CTR + HMAC-SHA256)
# ------------------------------------------------------------------


def _derive_subkeys() -> tuple[bytes, bytes]:
    raw_secret = config.get_secret_key().encode("utf-8")
    prk = hashlib.sha256(raw_secret).digest()
    enc_key = hmac.new(prk, b"soundmesh-oauth-enc-v1", hashlib.sha256).digest()
    mac_key = hmac.new(prk, b"soundmesh-oauth-mac-v1", hashlib.sha256).digest()
    return enc_key, mac_key


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a sensitive token string using Encrypt-then-MAC."""
    if not plaintext:
        return ""
    if plaintext.startswith(_ENC_PREFIX):
        return plaintext
    enc_key, mac_key = _derive_subkeys()
    pt_bytes = plaintext.encode("utf-8")
    nonce = secrets.token_bytes(16)
    keystream = hashlib.shake_256(enc_key + nonce).digest(len(pt_bytes))
    ciphertext = bytes(a ^ b for a, b in zip(pt_bytes, keystream))
    tag = hmac.new(mac_key, _ENC_PREFIX.encode("ascii") + nonce + ciphertext, hashlib.sha256).digest()
    blob = base64.urlsafe_b64encode(nonce + ciphertext + tag).decode("ascii")
    return f"{_ENC_PREFIX}{blob}"


def decrypt_secret(stored: str) -> str:
    """Verify MAC in constant time and decrypt a stored token (supports legacy plaintext)."""
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored
    b64_part = stored[len(_ENC_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(b64_part.encode("ascii"))
    except Exception:
        return ""
    if len(raw) < 48:  # 16-byte nonce + >=0 ciphertext + 32-byte tag
        return ""
    nonce = raw[:16]
    ciphertext = raw[16:-32]
    tag = raw[-32:]
    enc_key, mac_key = _derive_subkeys()
    expected_tag = hmac.new(
        mac_key, _ENC_PREFIX.encode("ascii") + nonce + ciphertext, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected_tag, tag):
        return ""
    keystream = hashlib.shake_256(enc_key + nonce).digest(len(ciphertext))
    pt_bytes = bytes(a ^ b for a, b in zip(ciphertext, keystream))
    try:
        return pt_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ""


# ------------------------------------------------------------------
# Short-Lived Room Join Tickets (avoids passcodes in WS query URLs)
# ------------------------------------------------------------------


def sign_room_join_ticket(room_code: str, ttl_sec: int = 180) -> str:
    """Mint an HMAC-signed short-lived ticket proving passcode verification for `room_code`."""
    clean_code = room_code.strip().upper()
    exp = int(time.time()) + ttl_sec
    nonce = secrets.token_hex(6)
    payload = f"{clean_code}:{exp}:{nonce}"
    key = hashlib.sha256(config.get_secret_key().encode("utf-8")).digest()
    sig = hmac.new(key, f"room-join:{payload}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    token = base64.urlsafe_b64encode(f"{payload}:{sig}".encode("ascii")).decode("ascii").rstrip("=")
    return f"rjt_{token}"


def verify_room_join_ticket(ticket: str, room_code: str) -> bool:
    """Verify an `rjt_...` ticket matches `room_code` and has not expired."""
    if not ticket or not ticket.startswith("rjt_"):
        return False
    b64 = ticket[4:]
    pad = "=" * ((4 - len(b64) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode((b64 + pad).encode("ascii")).decode("ascii")
        code, exp_str, nonce, sig = decoded.split(":", 3)
        exp = int(exp_str)
    except Exception:
        return False
    if code != room_code.strip().upper() or exp < int(time.time()):
        return False
    payload = f"{code}:{exp}:{nonce}"
    key = hashlib.sha256(config.get_secret_key().encode("utf-8")).digest()
    expected = hmac.new(key, f"room-join:{payload}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


# ------------------------------------------------------------------
# Bounded Sliding-Window Rate Limiter
# ------------------------------------------------------------------


class SlidingWindowRateLimiter:
    """Thread-safe bounded sliding-window rate limiter keyed by endpoint + client key."""

    def __init__(self, max_buckets: int = 4096) -> None:
        self.max_buckets = max_buckets
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, bucket: str, limit: int, window_sec: float) -> bool:
        now = time.monotonic()
        cutoff = now - window_sec
        with self._lock:
            dq = self._hits.get(bucket)
            if dq is None:
                if len(self._hits) >= self.max_buckets:
                    # Evict oldest bucket to bound memory
                    oldest_key = next(iter(self._hits))
                    self._hits.pop(oldest_key, None)
                dq = deque()
                self._hits[bucket] = dq
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


# ------------------------------------------------------------------
# Input, URL, Origin & Audio Header Validators
# ------------------------------------------------------------------


def sanitize_client_id(raw: str | None) -> str:
    cleaned = (raw or "").strip()
    if _SAFE_CLIENT_ID_RE.match(cleaned):
        return cleaned
    return ""


def sanitize_thumbnail_url(url: str | None) -> str:
    """Allow empty string or safe HTTPS thumbnail URLs on trusted media domains."""
    if not url:
        return ""
    cleaned = url.strip()
    if len(cleaned) > 512 or any(ch in cleaned for ch in ('"', "'", "(", ")", "\\", " ", "\n", "\r")):
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if any(host == dom or host.endswith(f".{dom}") for dom in TRUSTED_THUMB_HOSTS):
        return cleaned
    return ""


def validate_audio_magic_bytes(ext: str, content: bytes) -> bool:
    """Verify uploaded file header matches a real audio/media container."""
    if len(content) < 12:
        return False
    head = content[:16]
    lower_ext = ext.lower()
    if lower_ext == ".mp3":
        # ID3v2 tag or MPEG audio frame sync (0xFFE0..0xFFFF)
        return head.startswith(b"ID3") or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if lower_ext == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if lower_ext == ".flac":
        return head.startswith(b"fLaC")
    if lower_ext in (".ogg", ".opus"):
        return head.startswith(b"OggS")
    if lower_ext in (".m4a", ".mp4"):
        return head[4:8] == b"ftyp"
    if lower_ext == ".webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def is_safe_host_header(host: str) -> bool:
    return bool(host and _HOST_RE.match(host))


def is_allowed_ws_origin(origin: str | None, host_header: str) -> bool:
    """Validate WebSocket Origin header to prevent Cross-Site WebSocket Hijacking."""
    if not origin:
        # Non-browser clients / local test clients omit Origin
        return True
    cleaned = origin.strip().rstrip("/")
    if cleaned == "null":
        return False
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    # Same-host match
    if host_header and parsed.netloc.lower() == host_header.strip().lower():
        return True

    allowed = {o.lower() for o in config.get_allowed_origins()}
    pub = config.get_public_url().lower()
    if pub:
        allowed.add(pub)
    if config.FRONTEND_URL:
        allowed.add(config.FRONTEND_URL.lower())
    if cleaned.lower() in allowed:
        return True

    # Allow active Cloudflare Quick Tunnel or local LAN origin matching port
    host_only = (parsed.hostname or "").lower()
    if host_only in ("localhost", "127.0.0.1", "::1", "testserver"):
        return True
    try:
        ip = ipaddress.ip_address(host_only)
        if ip.is_private or ip.is_loopback:
            return True
    except ValueError:
        pass
    return False


def is_trusted_qr_url(url: str, request_host: str = "") -> bool:
    """Ensure `/api/qr` only encodes SoundMesh room/join links, never arbitrary phishing URLs."""
    cleaned = (url or "").strip()
    if not cleaned or len(cleaned) > 512:
        return False
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    path = parsed.path or "/"
    if not (path == "/" or path == "/join" or path.startswith("/room/")):
        return False

    host_lower = parsed.hostname.lower()
    netloc_lower = parsed.netloc.lower()

    trusted_netlocs: set[str] = set()
    if request_host:
        trusted_netlocs.add(request_host.strip().lower())
    for candidate in [config.get_public_url(), config.FRONTEND_URL, *config.get_allowed_origins()]:
        if candidate:
            p = urlparse(candidate)
            if p.netloc:
                trusted_netlocs.add(p.netloc.lower())

    if netloc_lower in trusted_netlocs:
        return True
    if host_lower in ("localhost", "127.0.0.1", "::1", "testserver"):
        return True
    if host_lower.endswith(".trycloudflare.com") and parsed.scheme == "https":
        return True
    try:
        ip = ipaddress.ip_address(host_lower)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False

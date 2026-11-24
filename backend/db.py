"""Dual-backend persistence: Supabase PostgreSQL (DATABASE_URL) or local SQLite."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from backend import config
from backend.security import decrypt_secret, encrypt_secret

_SESSION_CACHE_TTL_SEC = 45.0


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _hash_passcode(passcode: str, salt: str | None = None) -> str:
    use_salt = salt or secrets.token_hex(16)
    dk = hashlib.scrypt(
        passcode.encode("utf-8"),
        salt=use_salt.encode("ascii"),
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt${use_salt}${dk.hex()}"


def _verify_passcode_hash(passcode: str, stored: str) -> bool:
    if stored.startswith("scrypt$"):
        parts = stored.split("$")
        if len(parts) != 3:
            return False
        _, salt, expected = parts
        try:
            dk = hashlib.scrypt(
                passcode.encode("utf-8"),
                salt=salt.encode("ascii"),
                n=16384,
                r=8,
                p=1,
                dklen=32,
            )
        except ValueError:
            return False
        return hmac.compare_digest(dk.hex(), expected)
    # Backward-compatible verification for legacy SHA-256 room passcodes
    if ":" not in stored:
        return False
    salt, expected = stored.split(":", 1)
    candidate = hashlib.sha256(f"{salt}:{passcode}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate, expected)


def normalize_postgres_url(raw_url: str) -> str:
    """Normalize postgres:// scheme and enforce sslmode=require for Supabase Pooler."""
    url = raw_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    if "sslmode" not in qs:
        qs["sslmode"] = ["require"]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


class Database:
    """Thread-safe persistence supporting Supabase PostgreSQL and local SQLite."""

    def __init__(
        self,
        db_path: Path | None = None,
        database_url: str | None = None,
    ) -> None:
        raw_url = config.DATABASE_URL if database_url is None else database_url.strip()
        self.is_postgres = raw_url.startswith(("postgres://", "postgresql://"))
        self.database_url = normalize_postgres_url(raw_url) if self.is_postgres else ""
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self._lock = threading.RLock()
        self._conn: Any = None
        self._session_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._session_writes: int = 0
        self._connect()
        self._init_schema()

    def _connect(self) -> None:
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            self._conn = psycopg.connect(
                self.database_url,
                row_factory=dict_row,
                autocommit=False,
                prepare_threshold=None,
            )
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    def _ensure_conn(self) -> None:
        if self._conn is None:
            self._connect()
            return
        if self.is_postgres and getattr(self._conn, "closed", False):
            self._connect()

    def _sql(self, query: str) -> str:
        """Translate SQLite '?' placeholders to PostgreSQL '%s' when using Postgres."""
        if self.is_postgres:
            return query.replace("?", "%s")
        return query

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        with self._lock:
            self._ensure_conn()
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                if self.is_postgres:
                    # Reconnect cleanly if pooler dropped an idle socket
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

    def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            for attempt in range(2):
                try:
                    self._ensure_conn()
                    cur = self._conn.cursor()
                    try:
                        cur.execute(self._sql(query), params)
                        row = cur.fetchone()
                        if self.is_postgres:
                            self._conn.commit()
                        return dict(row) if row else None
                    finally:
                        cur.close()
                except Exception:
                    if self.is_postgres and attempt == 0:
                        try:
                            self._conn.close()
                        except Exception:
                            pass
                        self._conn = None
                        continue
                    raise
        return None

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            for attempt in range(2):
                try:
                    self._ensure_conn()
                    cur = self._conn.cursor()
                    try:
                        cur.execute(self._sql(query), params)
                        rows = cur.fetchall()
                        if self.is_postgres:
                            self._conn.commit()
                        return [dict(r) for r in rows]
                    finally:
                        cur.close()
                except Exception:
                    if self.is_postgres and attempt == 0:
                        try:
                            self._conn.close()
                        except Exception:
                            pass
                        self._conn = None
                        continue
                    raise
        return []

    def _init_schema(self) -> None:
        with self._tx() as cur:
            if not self.is_postgres:
                cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    email TEXT DEFAULT '',
                    name TEXT NOT NULL,
                    avatar_url TEXT DEFAULT '',
                    google_access_token TEXT DEFAULT '',
                    google_refresh_token TEXT DEFAULT '',
                    google_token_expires_at DOUBLE PRECISION DEFAULT 0,
                    has_youtube_scope INTEGER DEFAULT 0,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL,
                    expires_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS rooms (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    is_public INTEGER NOT NULL DEFAULT 1,
                    passcode_hash TEXT DEFAULT '',
                    created_at DOUBLE PRECISION NOT NULL,
                    last_active_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS speaker_presets (
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    role TEXT DEFAULT 'stereo',
                    offset_ms DOUBLE PRECISION DEFAULT 0,
                    eq_preset TEXT DEFAULT 'flat',
                    pos_x DOUBLE PRECISION DEFAULT 0,
                    pos_y DOUBLE PRECISION DEFAULT 0,
                    updated_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (user_id, device_id)
                )
                """
            )
            if self.is_postgres:
                # Lock down Supabase PostgREST / Data API access via Row Level Security
                for tbl in ("users", "sessions", "rooms", "speaker_presets"):
                    cur.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;")
                    cur.execute(
                        f"""
                        DO $$
                        BEGIN
                            EXECUTE 'REVOKE ALL ON TABLE {tbl} FROM anon, authenticated';
                        EXCEPTION WHEN undefined_object THEN
                            NULL;
                        END $$;
                        """
                    )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    @staticmethod
    def _public_user(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "provider": row["provider"],
            "email": row.get("email") or "",
            "name": row["name"],
            "avatar_url": row.get("avatar_url") or "",
            "has_youtube_scope": bool(row.get("has_youtube_scope")),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Users & Sessions
    # ------------------------------------------------------------------

    def upsert_user(
        self,
        user_id: str,
        provider: str,
        name: str,
        email: str = "",
        avatar_url: str = "",
        google_access_token: str | None = None,
        google_refresh_token: str | None = None,
        google_token_expires_at: float | None = None,
        has_youtube_scope: bool | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        existing = self.get_user_by_id(user_id, include_tokens=True)
        with self._tx() as cur:
            if existing:
                raw_access = (
                    google_access_token
                    if google_access_token is not None
                    else existing.get("google_access_token", "")
                )
                raw_refresh = (
                    google_refresh_token
                    if google_refresh_token
                    else existing.get("google_refresh_token", "")
                )
                access = encrypt_secret(raw_access) if raw_access else ""
                refresh = encrypt_secret(raw_refresh) if raw_refresh else ""
                expires = (
                    google_token_expires_at
                    if google_token_expires_at is not None
                    else float(existing.get("google_token_expires_at") or 0.0)
                )
                yt_scope = (
                    int(bool(has_youtube_scope))
                    if has_youtube_scope is not None
                    else int(bool(existing.get("has_youtube_scope")))
                )
                cur.execute(
                    self._sql(
                        """
                        UPDATE users
                        SET provider=?, email=?, name=?, avatar_url=?,
                            google_access_token=?, google_refresh_token=?,
                            google_token_expires_at=?, has_youtube_scope=?, updated_at=?
                        WHERE id=?
                        """
                    ),
                    (
                        provider,
                        email or existing.get("email", ""),
                        name,
                        avatar_url if avatar_url != "" else existing.get("avatar_url", ""),
                        access,
                        refresh,
                        expires,
                        yt_scope,
                        now,
                        user_id,
                    ),
                )
            else:
                enc_access = encrypt_secret(google_access_token) if google_access_token else ""
                enc_refresh = encrypt_secret(google_refresh_token) if google_refresh_token else ""
                cur.execute(
                    self._sql(
                        """
                        INSERT INTO users (
                            id, provider, email, name, avatar_url,
                            google_access_token, google_refresh_token,
                            google_token_expires_at, has_youtube_scope,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        user_id,
                        provider,
                        email,
                        name,
                        avatar_url,
                        enc_access,
                        enc_refresh,
                        float(google_token_expires_at or 0.0),
                        int(bool(has_youtube_scope)),
                        now,
                        now,
                    ),
                )
        with self._lock:
            self._session_cache.clear()
        user = self.get_user_by_id(user_id, include_tokens=False)
        assert user is not None
        return user

    def get_user_by_id(
        self, user_id: str, include_tokens: bool = False
    ) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM users WHERE id=?", (user_id,))
        if not row:
            return None
        if include_tokens:
            row["has_youtube_scope"] = bool(row.get("has_youtube_scope"))
            row["google_access_token"] = decrypt_secret(str(row.get("google_access_token") or ""))
            row["google_refresh_token"] = decrypt_secret(str(row.get("google_refresh_token") or ""))
            return row
        return self._public_user(row)

    def create_session(self, user_id: str, ttl_sec: int = config.SESSION_TTL_SEC) -> str:
        raw_token = secrets.token_urlsafe(32)
        now = time.time()
        with self._tx() as cur:
            cur.execute(
                self._sql(
                    "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)"
                ),
                (_hash_token(raw_token), user_id, now, now + ttl_sec),
            )
            self._session_writes += 1
            if self._session_writes % 50 == 0:
                cur.execute(self._sql("DELETE FROM sessions WHERE expires_at < ?"), (now,))
        return raw_token

    def purge_expired_sessions(self) -> None:
        now = time.time()
        with self._tx() as cur:
            cur.execute(self._sql("DELETE FROM sessions WHERE expires_at < ?"), (now,))
        with self._lock:
            self._session_cache.clear()

    def get_user_by_session_token(self, raw_token: str) -> dict[str, Any] | None:
        if not raw_token or len(raw_token) > 256:
            return None
        token_hash = _hash_token(raw_token)
        now_mono = time.monotonic()
        with self._lock:
            cached = self._session_cache.get(token_hash)
            if cached and cached[0] > now_mono:
                return dict(cached[1])

        row = self._fetchone(
            """
            SELECT u.*, s.expires_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        )
        if not row:
            return None
        if float(row["expires_at"]) < time.time():
            self.delete_session(raw_token)
            return None
        pub = self._public_user(row)
        with self._lock:
            if len(self._session_cache) >= 1024:
                self._session_cache.pop(next(iter(self._session_cache)), None)
            self._session_cache[token_hash] = (now_mono + _SESSION_CACHE_TTL_SEC, pub)
        return dict(pub)

    def delete_session(self, raw_token: str) -> None:
        if not raw_token:
            return
        token_hash = _hash_token(raw_token)
        with self._lock:
            self._session_cache.pop(token_hash, None)
        with self._tx() as cur:
            cur.execute(
                self._sql("DELETE FROM sessions WHERE token_hash=?"),
                (token_hash,),
            )

    # ------------------------------------------------------------------
    # Speaker Presets
    # ------------------------------------------------------------------

    def save_speaker_preset(
        self,
        user_id: str,
        device_id: str,
        name: str = "",
        role: str = "stereo",
        offset_ms: float = 0.0,
        eq_preset: str = "flat",
        pos_x: float = 0.0,
        pos_y: float = 0.0,
    ) -> dict[str, Any]:
        now = time.time()
        existing = self.get_speaker_preset(user_id, device_id)
        with self._tx() as cur:
            if existing:
                cur.execute(
                    self._sql(
                        """
                        UPDATE speaker_presets
                        SET name=?, role=?, offset_ms=?, eq_preset=?, pos_x=?, pos_y=?, updated_at=?
                        WHERE user_id=? AND device_id=?
                        """
                    ),
                    (name, role, float(offset_ms), eq_preset, float(pos_x), float(pos_y), now, user_id, device_id),
                )
            else:
                cur.execute(
                    self._sql(
                        """
                        INSERT INTO speaker_presets (
                            user_id, device_id, name, role, offset_ms, eq_preset, pos_x, pos_y, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (user_id, device_id, name, role, float(offset_ms), eq_preset, float(pos_x), float(pos_y), now),
                )
        saved = self.get_speaker_preset(user_id, device_id)
        assert saved is not None
        return saved

    def get_speaker_preset(self, user_id: str, device_id: str) -> dict[str, Any] | None:
        return self._fetchone(
            "SELECT * FROM speaker_presets WHERE user_id=? AND device_id=?",
            (user_id, device_id),
        )

    # ------------------------------------------------------------------
    # Rooms
    # ------------------------------------------------------------------

    @staticmethod
    def _room_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": row["code"],
            "name": row["name"],
            "owner_user_id": row["owner_user_id"],
            "owner_name": row["owner_name"],
            "is_public": bool(row["is_public"]),
            "has_passcode": bool(row.get("passcode_hash")),
            "created_at": row["created_at"],
            "last_active_at": row["last_active_at"],
        }

    def create_room_record(
        self,
        code: str,
        name: str,
        owner_user_id: str,
        owner_name: str,
        is_public: bool = True,
        passcode: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        clean_pass = passcode.strip()
        pass_hash = _hash_passcode(clean_pass) if clean_pass else ""
        with self._tx() as cur:
            cur.execute(
                self._sql(
                    """
                    INSERT INTO rooms (
                        code, name, owner_user_id, owner_name, is_public,
                        passcode_hash, created_at, last_active_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    code.upper(),
                    name.strip()[:48],
                    owner_user_id,
                    owner_name.strip()[:40],
                    int(bool(is_public)),
                    pass_hash,
                    now,
                    now,
                ),
            )
        rec = self.get_room_record(code)
        assert rec is not None
        return rec

    def get_room_record(self, code: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM rooms WHERE code=?", (code.strip().upper(),)
        )
        return self._room_dict(row) if row else None

    def list_public_room_records(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._fetchall(
            "SELECT * FROM rooms WHERE is_public=1 ORDER BY last_active_at DESC LIMIT ?",
            (limit,),
        )
        return [self._room_dict(r) for r in rows]

    def list_rooms_by_owner(self, owner_user_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall(
            "SELECT * FROM rooms WHERE owner_user_id=? ORDER BY last_active_at DESC",
            (owner_user_id,),
        )
        return [self._room_dict(r) for r in rows]

    def verify_room_passcode(self, code: str, passcode: str) -> bool:
        row = self._fetchone(
            "SELECT passcode_hash FROM rooms WHERE code=?",
            (code.strip().upper(),),
        )
        if not row:
            return False
        stored = row.get("passcode_hash") or ""
        if not stored:
            return True
        return _verify_passcode_hash(passcode.strip(), stored)

    def touch_room(self, code: str) -> None:
        with self._tx() as cur:
            cur.execute(
                self._sql("UPDATE rooms SET last_active_at=? WHERE code=?"),
                (time.time(), code.strip().upper()),
            )

    def delete_room_record(self, code: str) -> None:
        with self._tx() as cur:
            cur.execute(
                self._sql("DELETE FROM rooms WHERE code=?"),
                (code.strip().upper(),),
            )

"""SQLite persistence for users, sessions, and SoundMesh rooms."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from backend.config import DB_PATH, SESSION_TTL_SEC


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _hash_passcode(passcode: str, salt: str | None = None) -> str:
    use_salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{use_salt}:{passcode}".encode("utf-8")).hexdigest()
    return f"{use_salt}:{digest}"


def _verify_passcode_hash(passcode: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, expected = stored.split(":", 1)
    candidate = hashlib.sha256(f"{salt}:{passcode}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate, expected)


class Database:
    """Thread-safe SQLite store using WAL mode."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._tx() as cur:
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
                    google_token_expires_at REAL DEFAULT 0,
                    has_youtube_scope INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
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
                    created_at REAL NOT NULL,
                    last_active_at REAL NOT NULL
                )
                """
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "provider": row["provider"],
            "email": row["email"] or "",
            "name": row["name"],
            "avatar_url": row["avatar_url"] or "",
            "has_youtube_scope": bool(row["has_youtube_scope"]),
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
                access = (
                    google_access_token
                    if google_access_token is not None
                    else existing.get("google_access_token", "")
                )
                refresh = (
                    google_refresh_token
                    if google_refresh_token
                    else existing.get("google_refresh_token", "")
                )
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
                    """
                    UPDATE users
                    SET provider=?, email=?, name=?, avatar_url=?,
                        google_access_token=?, google_refresh_token=?,
                        google_token_expires_at=?, has_youtube_scope=?, updated_at=?
                    WHERE id=?
                    """,
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
                cur.execute(
                    """
                    INSERT INTO users (
                        id, provider, email, name, avatar_url,
                        google_access_token, google_refresh_token,
                        google_token_expires_at, has_youtube_scope,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        provider,
                        email,
                        name,
                        avatar_url,
                        google_access_token or "",
                        google_refresh_token or "",
                        float(google_token_expires_at or 0.0),
                        int(bool(has_youtube_scope)),
                        now,
                        now,
                    ),
                )
        user = self.get_user_by_id(user_id, include_tokens=False)
        assert user is not None
        return user

    def get_user_by_id(
        self, user_id: str, include_tokens: bool = False
    ) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        try:
            row = cur.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                return None
            if include_tokens:
                d = dict(row)
                d["has_youtube_scope"] = bool(d["has_youtube_scope"])
                return d
            return self._public_user(row)
        finally:
            cur.close()

    def create_session(self, user_id: str, ttl_sec: int = SESSION_TTL_SEC) -> str:
        raw_token = secrets.token_urlsafe(32)
        now = time.time()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_hash_token(raw_token), user_id, now, now + ttl_sec),
            )
        return raw_token

    def get_user_by_session_token(self, raw_token: str) -> dict[str, Any] | None:
        if not raw_token:
            return None
        token_hash = _hash_token(raw_token)
        now = time.time()
        cur = self._conn.cursor()
        try:
            row = cur.execute(
                """
                SELECT u.*, s.expires_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        finally:
            cur.close()

        if not row:
            return None
        if float(row["expires_at"]) < now:
            self.delete_session(raw_token)
            return None
        return self._public_user(row)

    def delete_session(self, raw_token: str) -> None:
        if not raw_token:
            return
        with self._tx() as cur:
            cur.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(raw_token),))

    # ------------------------------------------------------------------
    # Rooms
    # ------------------------------------------------------------------

    @staticmethod
    def _room_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "code": row["code"],
            "name": row["name"],
            "owner_user_id": row["owner_user_id"],
            "owner_name": row["owner_name"],
            "is_public": bool(row["is_public"]),
            "has_passcode": bool(row["passcode_hash"]),
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
                """
                INSERT INTO rooms (
                    code, name, owner_user_id, owner_name, is_public,
                    passcode_hash, created_at, last_active_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
        cur = self._conn.cursor()
        try:
            row = cur.execute(
                "SELECT * FROM rooms WHERE code=?", (code.strip().upper(),)
            ).fetchone()
            return self._room_dict(row) if row else None
        finally:
            cur.close()

    def list_public_room_records(self, limit: int = 30) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        try:
            rows = cur.execute(
                "SELECT * FROM rooms WHERE is_public=1 ORDER BY last_active_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._room_dict(r) for r in rows]
        finally:
            cur.close()

    def list_rooms_by_owner(self, owner_user_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        try:
            rows = cur.execute(
                "SELECT * FROM rooms WHERE owner_user_id=? ORDER BY last_active_at DESC",
                (owner_user_id,),
            ).fetchall()
            return [self._room_dict(r) for r in rows]
        finally:
            cur.close()

    def verify_room_passcode(self, code: str, passcode: str) -> bool:
        cur = self._conn.cursor()
        try:
            row = cur.execute(
                "SELECT passcode_hash FROM rooms WHERE code=?",
                (code.strip().upper(),),
            ).fetchone()
            if not row:
                return False
            stored = row["passcode_hash"] or ""
            if not stored:
                return True
            return _verify_passcode_hash(passcode.strip(), stored)
        finally:
            cur.close()

    def touch_room(self, code: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE rooms SET last_active_at=? WHERE code=?",
                (time.time(), code.strip().upper()),
            )

    def delete_room_record(self, code: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM rooms WHERE code=?", (code.strip().upper(),))

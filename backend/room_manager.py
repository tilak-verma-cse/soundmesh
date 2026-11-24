"""Multi-room registry and lifecycle manager backed by SQLite."""

from __future__ import annotations

import secrets
from typing import Any

from backend.audio_cache import AudioCache
from backend.db import Database
from backend.sync_room import SyncRoom

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class RoomManager:
    """Manages active SyncRoom instances and persists room metadata in SQLite."""

    def __init__(self, db: Database, audio_cache: AudioCache) -> None:
        self.db = db
        self.audio_cache = audio_cache
        self.rooms: dict[str, SyncRoom] = {}

    def generate_room_code(self, length: int = 6) -> str:
        """Generate a unique readable room code avoiding 0/O/1/I."""
        for _ in range(25):
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
            if code not in self.rooms and not self.db.get_room_record(code):
                return code
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length + 2))

    def _hydrate_room(
        self, rec: dict[str, Any], allow_collab_queue: bool = True
    ) -> SyncRoom:
        code = rec["code"].upper()
        if code in self.rooms:
            return self.rooms[code]
        room = SyncRoom(
            audio_cache=self.audio_cache,
            room_code=code,
            room_name=rec["name"],
            owner_user_id=rec["owner_user_id"],
            owner_name=rec["owner_name"],
            is_public=bool(rec["is_public"]),
            has_passcode=bool(rec["has_passcode"]),
            allow_collab_queue=allow_collab_queue,
        )
        room.start()
        self.rooms[code] = room
        return room

    def create_room(
        self,
        name: str,
        owner_user: dict[str, Any],
        is_public: bool = True,
        passcode: str = "",
        allow_collab_queue: bool = True,
    ) -> SyncRoom:
        """Create and persist a new synchronized room."""
        code = self.generate_room_code()
        clean_name = (name or f"{owner_user['name']}'s Room").strip()[:48]
        rec = self.db.create_room_record(
            code=code,
            name=clean_name,
            owner_user_id=owner_user["id"],
            owner_name=owner_user["name"],
            is_public=is_public,
            passcode=passcode,
        )
        return self._hydrate_room(rec, allow_collab_queue=allow_collab_queue)

    def get_or_load_room(self, code: str) -> SyncRoom | None:
        """Return an active room or hydrate it from SQLite if it exists."""
        clean = code.strip().upper()
        if not clean:
            return None
        if clean in self.rooms:
            return self.rooms[clean]
        rec = self.db.get_room_record(clean)
        if not rec:
            return None
        return self._hydrate_room(rec)

    def get_or_create_default_room(self) -> SyncRoom:
        """Return the default 'STUDIO' room for quick/legacy access."""
        existing = self.get_or_load_room("STUDIO")
        if existing:
            return existing
        rec = self.db.create_room_record(
            code="STUDIO",
            name="SoundMesh Studio",
            owner_user_id="",
            owner_name="Host",
            is_public=True,
            passcode="",
        )
        return self._hydrate_room(rec)

    def list_public_rooms(self) -> list[dict[str, Any]]:
        """List active/persisted public rooms with live speaker counts and now-playing."""
        records = self.db.list_public_room_records(limit=30)
        summaries: list[dict[str, Any]] = []
        for rec in records:
            room = self._hydrate_room(rec)
            summaries.append(room.get_summary())
        summaries.sort(key=lambda r: (r["speaker_count"] == 0, -r["speaker_count"]))
        return summaries

    def list_user_rooms(self, user_id: str) -> list[dict[str, Any]]:
        """List rooms owned by a specific user."""
        if not user_id:
            return []
        records = self.db.list_rooms_by_owner(user_id)
        return [self._hydrate_room(rec).get_summary() for rec in records]

    def verify_passcode(self, code: str, passcode: str) -> bool:
        return self.db.verify_room_passcode(code, passcode)

    def is_track_referenced(self, video_id: str) -> bool:
        """Return True if `video_id` is active or queued in any hydrated room."""
        for room in self.rooms.values():
            if room.playback.track and room.playback.track.video_id == video_id:
                return True
            if any(t.video_id == video_id for t in room.queue):
                return True
        return False

    async def evict_idle_rooms(self, max_idle_ms: float = 30 * 60 * 1000.0) -> int:
        """Stop and unload empty non-default rooms idle for longer than `max_idle_ms`."""
        from backend.config import server_now_ms

        now = server_now_ms()
        evicted = 0
        for code, room in list(self.rooms.items()):
            if code == "STUDIO" or room.speakers:
                continue
            if now - room.last_activity_ms >= max_idle_ms:
                self.rooms.pop(code, None)
                await room.stop()
                evicted += 1
        return evicted

    async def close_room(self, code: str, actor_user_id: str) -> bool:
        """Close and delete a room if actor is its owner."""
        clean = code.strip().upper()
        rec = self.db.get_room_record(clean)
        if not rec:
            return False
        if rec["owner_user_id"] != actor_user_id:
            return False

        room = self.rooms.pop(clean, None)
        if room:
            await room.broadcast({"type": "room_closed", "room_code": clean})
            await room.stop()
        self.db.delete_room_record(clean)
        return True

    async def stop_all(self) -> None:
        """Stop all active room watcher tasks on server shutdown."""
        for room in list(self.rooms.values()):
            await room.stop()
        self.rooms.clear()

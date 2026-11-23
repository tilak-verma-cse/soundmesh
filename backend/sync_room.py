"""Authoritative multi-speaker playback, dynamic leader delegation, queue, and calibration."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from backend.audio_cache import AudioCache
from backend.config import (
    CALIBRATION_INTERVAL_MS,
    PLAY_LEAD_MS,
    READY_BARRIER_TIMEOUT_SEC,
    STREAMING_THRESHOLD_SEC,
    server_now_ms,
)
from backend.models import (
    AuthProvider,
    CalibrationState,
    DeviceType,
    LeaderRequest,
    PlaybackState,
    RepeatMode,
    RoomSnapshot,
    SpeakerRole,
    SpeakerState,
    Track,
)

logger = logging.getLogger(__name__)

_VALID_ROLES: set[str] = {"stereo", "left", "right", "mono", "subwoofer"}
_VALID_REPEAT: set[str] = {"off", "all", "one"}


@dataclass
class SpeakerConnection:
    """Pairs a SpeakerState with its active WebSocket."""

    state: SpeakerState
    ws: WebSocket
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SyncRoom:
    """Authoritative multi-speaker synchronization room with dynamic Leader delegation."""

    def __init__(
        self,
        audio_cache: AudioCache,
        room_code: str = "STUDIO",
        room_name: str = "SoundMesh Studio",
        owner_user_id: str = "",
        owner_name: str = "Host",
        is_public: bool = True,
        has_passcode: bool = False,
    ) -> None:
        self.audio_cache = audio_cache
        self.room_code = room_code.strip().upper() or "STUDIO"
        self.room_name = room_name
        self.owner_user_id = owner_user_id
        self.owner_name = owner_name
        self.is_public = is_public
        self.has_passcode = has_passcode

        self.speakers: dict[str, SpeakerConnection] = {}
        self.queue: list[Track] = []
        self.shuffle: bool = False
        self.repeat: RepeatMode = "off"
        self.master_volume: float = 1.0
        self.master_muted: bool = False
        # Leader Mode by default: the elected Leader controls playback for all devices
        self.allow_guest_control: bool = False
        self.allow_collab_queue: bool = False

        self.leader_speaker_id: str | None = None
        self.leader_user_id: str | None = None
        self.pending_leader_requests: dict[str, LeaderRequest] = {}

        self.playback: PlaybackState = PlaybackState()
        self.calibration: CalibrationState = CalibrationState(
            interval_ms=CALIBRATION_INTERVAL_MS
        )

        self._load_task: asyncio.Task[None] | None = None
        self._barrier_event: asyncio.Event | None = None
        self._barrier_generation: int = -1
        self._watcher_task: asyncio.Task[None] | None = None
        self._last_telemetry_broadcast: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background auto-advance watcher."""
        if self._watcher_task is None or self._watcher_task.done():
            self._watcher_task = asyncio.create_task(self._auto_advance_loop())

    async def stop(self) -> None:
        """Cancel background tasks cleanly on shutdown."""
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        if self._watcher_task and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass

    async def _auto_advance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(0.5)
                pb = self.playback
                if pb.status == "playing" and pb.audio_duration_sec > 0:
                    pos = self.get_current_position_sec()
                    if pos >= pb.audio_duration_sec + 0.35:
                        await self.next_track(auto=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Auto-advance loop error: %s", exc)

    # ------------------------------------------------------------------
    # Permissions & Ordering
    # ------------------------------------------------------------------

    def can_control_room(self, speaker_id: str) -> bool:
        conn = self.speakers.get(speaker_id)
        if not conn:
            return False
        return conn.state.is_leader or self.allow_guest_control

    def can_add_to_queue(self, speaker_id: str) -> bool:
        conn = self.speakers.get(speaker_id)
        if not conn:
            return False
        return self.can_control_room(speaker_id) or self.allow_collab_queue

    def can_assign_leader(self, speaker_id: str) -> bool:
        conn = self.speakers.get(speaker_id)
        if not conn:
            return False
        return conn.state.is_leader or conn.state.is_owner

    def _sync_leader_flags(self) -> None:
        for sid, conn in self.speakers.items():
            is_ldr = sid == self.leader_speaker_id
            conn.state.is_leader = is_ldr
            conn.state.is_host = is_ldr

    def _ordered_speakers(self) -> list[SpeakerState]:
        return [
            conn.state
            for conn in sorted(
                self.speakers.values(),
                key=lambda c: (
                    not c.state.is_leader,
                    not c.state.is_owner,
                    c.state.connected_at,
                ),
            )
        ]

    # ------------------------------------------------------------------
    # Snapshot & Messaging
    # ------------------------------------------------------------------

    def get_current_position_sec(self, now_ms: float | None = None) -> float:
        """Compute authoritative playback position in seconds."""
        pb = self.playback
        if pb.status != "playing":
            return max(0.0, pb.anchor_position_sec)
        current_ms = server_now_ms() if now_ms is None else now_ms
        if current_ms < pb.anchor_server_ms:
            return max(0.0, pb.anchor_position_sec)
        elapsed_sec = ((current_ms - pb.anchor_server_ms) / 1000.0) * pb.playback_rate
        return max(0.0, pb.anchor_position_sec + elapsed_sec)

    def build_snapshot(self) -> dict[str, Any]:
        """Build full serialized RoomSnapshot dictionary."""
        snap = RoomSnapshot(
            room_code=self.room_code,
            room_name=self.room_name,
            owner_user_id=self.owner_user_id,
            owner_name=self.owner_name,
            is_public=self.is_public,
            has_passcode=self.has_passcode,
            leader_speaker_id=self.leader_speaker_id,
            leader_user_id=self.leader_user_id,
            allow_collab_queue=self.allow_collab_queue,
            pending_leader_requests=sorted(
                self.pending_leader_requests.values(),
                key=lambda r: r.requested_at,
            ),
            server_now_ms=server_now_ms(),
            playback=self.playback,
            queue=self.queue,
            shuffle=self.shuffle,
            repeat=self.repeat,
            master_volume=self.master_volume,
            master_muted=self.master_muted,
            allow_guest_control=self.allow_guest_control,
            calibration=self.calibration,
            speakers=self._ordered_speakers(),
        )
        return snap.model_dump()

    def get_summary(self) -> dict[str, Any]:
        """Return compact room summary for the public/my-rooms lobby directory."""
        leader_conn = (
            self.speakers.get(self.leader_speaker_id)
            if self.leader_speaker_id
            else None
        )
        leader_name = (
            (leader_conn.state.user_name or leader_conn.state.name)
            if leader_conn
            else self.owner_name
        )
        leader_avatar = leader_conn.state.user_avatar if leader_conn else ""
        track = self.playback.track
        return {
            "code": self.room_code,
            "name": self.room_name,
            "owner_user_id": self.owner_user_id,
            "owner_name": self.owner_name,
            "leader_name": leader_name,
            "leader_avatar": leader_avatar,
            "speaker_count": len(self.speakers),
            "status": self.playback.status,
            "now_playing_title": track.title if track else "",
            "now_playing_artist": track.artist if track else "",
            "now_playing_thumb": track.thumbnail if track else "",
            "is_public": self.is_public,
            "has_passcode": self.has_passcode,
        }

    async def send_to(self, speaker_id: str, payload: dict[str, Any]) -> bool:
        """Send JSON message to a single speaker; return False if disconnected."""
        conn = self.speakers.get(speaker_id)
        if not conn:
            return False
        try:
            async with conn.lock:
                await asyncio.wait_for(conn.ws.send_json(payload), timeout=3.0)
            return True
        except Exception:
            await self.unregister_speaker(speaker_id)
            return False

    async def broadcast(
        self, payload: dict[str, Any], exclude_id: str | None = None
    ) -> None:
        """Broadcast JSON payload concurrently to all connected speakers."""
        targets = [
            (sid, conn)
            for sid, conn in list(self.speakers.items())
            if sid != exclude_id
        ]
        if not targets:
            return

        async def _send(sid: str, conn: SpeakerConnection) -> str | None:
            try:
                async with conn.lock:
                    await asyncio.wait_for(conn.ws.send_json(payload), timeout=3.0)
                return None
            except Exception:
                return sid

        results = await asyncio.gather(
            *(_send(sid, conn) for sid, conn in targets),
            return_exceptions=True,
        )
        dropped = [r for r in results if isinstance(r, str)]
        if dropped:
            for sid in dropped:
                await self.unregister_speaker(sid)

    async def broadcast_room_state(self) -> None:
        await self.broadcast({"type": "room_state", "state": self.build_snapshot()})

    async def broadcast_playback(self, force_resync: bool = False) -> None:
        await self.broadcast(
            {
                "type": "playback_update",
                "server_now_ms": server_now_ms(),
                "playback": self.playback.model_dump(),
                "force_resync": force_resync,
            }
        )

    async def broadcast_speakers(self) -> None:
        await self.broadcast(
            {
                "type": "speakers_update",
                "speakers": [s.model_dump() for s in self._ordered_speakers()],
            }
        )

    # ------------------------------------------------------------------
    # Speaker Registration & Dynamic Leader Handoff
    # ------------------------------------------------------------------

    async def register_speaker(
        self,
        speaker_id: str,
        ws: WebSocket,
        name: str,
        device_type: DeviceType,
        user_id: str = "",
        user_name: str = "",
        user_avatar: str = "",
        auth_provider: AuthProvider = "guest",
        role: str = "stereo",
        offset_ms: float = 0.0,
        is_host: bool | None = None,
    ) -> SpeakerState:
        """Register or reconnect a speaker and elect a Leader if none is active."""
        clean_role: SpeakerRole = role if role in _VALID_ROLES else "stereo"  # type: ignore[assignment]
        clamped_offset = max(-500.0, min(500.0, float(offset_ms)))
        resolved_user_name = (user_name or name or "Speaker")[:40]
        is_owner = bool(user_id and self.owner_user_id and user_id == self.owner_user_id)

        # Elect first active speaker (or explicit host when empty) as Room Leader
        if (
            self.leader_speaker_id is None
            or self.leader_speaker_id not in self.speakers
            or is_host is True
            and self.leader_speaker_id is None
        ):
            self.leader_speaker_id = speaker_id
            self.leader_user_id = user_id

        state = SpeakerState(
            id=speaker_id,
            name=(name or resolved_user_name or "Speaker")[:36],
            device_type=device_type,
            user_id=user_id,
            user_name=resolved_user_name,
            user_avatar=user_avatar,
            auth_provider=auth_provider,
            is_owner=is_owner,
            is_leader=(speaker_id == self.leader_speaker_id),
            is_host=(speaker_id == self.leader_speaker_id),
            role=clean_role,
            offset_ms=clamped_offset,
            connected_at=server_now_ms(),
        )
        self.speakers[speaker_id] = SpeakerConnection(state=state, ws=ws)
        self._sync_leader_flags()

        await self.send_to(
            speaker_id,
            {
                "type": "welcome",
                "speaker_id": speaker_id,
                "is_leader": state.is_leader,
                "is_owner": state.is_owner,
                "is_host": state.is_leader,
                "state": self.build_snapshot(),
            },
        )
        await self.broadcast_room_state()
        return state

    async def unregister_speaker(self, speaker_id: str) -> None:
        removed = self.speakers.pop(speaker_id, None)
        if removed is None:
            return

        self.pending_leader_requests.pop(speaker_id, None)
        self._check_ready_barrier()

        if self.leader_speaker_id == speaker_id:
            next_leader = self._pick_failover_leader(previous_user_id=removed.state.user_id)
            if next_leader:
                self.leader_speaker_id = next_leader.id
                self.leader_user_id = next_leader.user_id
                self._sync_leader_flags()
                await self.broadcast(
                    {
                        "type": "leader_changed",
                        "leader_id": next_leader.id,
                        "leader_name": next_leader.user_name or next_leader.name,
                        "device_name": next_leader.name,
                        "assigned_by": "Auto Failover",
                        "reason": "auto_handoff",
                    }
                )
            else:
                self.leader_speaker_id = None
                self.leader_user_id = None
                if self.playback.status == "playing":
                    pos = self.get_current_position_sec()
                    self.playback.status = "paused"
                    self.playback.anchor_position_sec = pos
                    self.playback.anchor_server_ms = server_now_ms()

        await self.broadcast_room_state()

    def _pick_failover_leader(self, previous_user_id: str) -> SpeakerState | None:
        if not self.speakers:
            return None
        ordered_by_join = sorted(
            (c.state for c in self.speakers.values()), key=lambda s: s.connected_at
        )
        # 1. Prefer Room Owner if connected
        for sp in ordered_by_join:
            if sp.is_owner:
                return sp
        # 2. Prefer another device of the same user
        if previous_user_id:
            for sp in ordered_by_join:
                if sp.user_id == previous_user_id:
                    return sp
        # 3. Earliest connected remaining speaker
        return ordered_by_join[0]

    async def assign_leader(self, actor_id: str, target_id: str) -> bool:
        """Pass or assign Room Leadership to `target_id` without interrupting playback."""
        if not self.can_assign_leader(actor_id):
            return False
        target_conn = self.speakers.get(target_id)
        actor_conn = self.speakers.get(actor_id)
        if not target_conn or not actor_conn:
            return False

        self.leader_speaker_id = target_id
        self.leader_user_id = target_conn.state.user_id
        self.pending_leader_requests.pop(target_id, None)
        self._sync_leader_flags()

        await self.broadcast(
            {
                "type": "leader_changed",
                "leader_id": target_id,
                "leader_name": target_conn.state.user_name or target_conn.state.name,
                "device_name": target_conn.state.name,
                "assigned_by": actor_conn.state.user_name or actor_conn.state.name,
                "reason": "assigned",
            }
        )
        await self.broadcast_room_state()
        return True

    async def request_leader(self, speaker_id: str) -> bool:
        """Allow any non-leader member to ask the current Leader/Owner for leadership."""
        conn = self.speakers.get(speaker_id)
        if not conn or conn.state.is_leader:
            return False

        req = LeaderRequest(
            speaker_id=speaker_id,
            user_id=conn.state.user_id,
            name=conn.state.name,
            user_name=conn.state.user_name or conn.state.name,
            user_avatar=conn.state.user_avatar,
            requested_at=server_now_ms(),
        )
        self.pending_leader_requests[speaker_id] = req

        # Notify current Leader and any connected Room Owner devices
        notify_targets = [
            sid
            for sid, c in self.speakers.items()
            if c.state.is_leader or c.state.is_owner
        ]
        for tid in notify_targets:
            await self.send_to(
                tid,
                {"type": "leader_request", "request": req.model_dump()},
            )

        await self.broadcast_room_state()
        return True

    async def respond_leader_request(
        self, actor_id: str, requester_id: str, approve: bool
    ) -> bool:
        """Approve or decline a pending leadership request."""
        if not self.can_assign_leader(actor_id):
            return False

        self.pending_leader_requests.pop(requester_id, None)
        actor_conn = self.speakers.get(actor_id)
        actor_name = (
            (actor_conn.state.user_name or actor_conn.state.name)
            if actor_conn
            else "Leader"
        )

        if approve and requester_id in self.speakers:
            return await self.assign_leader(actor_id, requester_id)

        await self.send_to(
            requester_id,
            {"type": "leader_request_declined", "by": actor_name},
        )
        await self.broadcast_room_state()
        return True

    # ------------------------------------------------------------------
    # Speaker Settings & Telemetry
    # ------------------------------------------------------------------

    async def update_speaker(
        self, actor_id: str, target_id: str, fields: dict[str, Any]
    ) -> None:
        """Update a speaker's settings; enforces permissions for other speakers."""
        if actor_id != target_id and not self.can_control_room(actor_id):
            return
        conn = self.speakers.get(target_id)
        if not conn:
            return
        st = conn.state
        if "name" in fields and isinstance(fields["name"], str):
            trimmed = fields["name"].strip()
            if trimmed:
                st.name = trimmed[:36]
        if "role" in fields and fields["role"] in _VALID_ROLES:
            st.role = fields["role"]
        if "volume" in fields:
            try:
                st.volume = max(0.0, min(1.0, float(fields["volume"])))
            except (TypeError, ValueError):
                pass
        if "muted" in fields:
            st.muted = bool(fields["muted"])
        if "offset_ms" in fields:
            try:
                st.offset_ms = max(-500.0, min(500.0, float(fields["offset_ms"])))
            except (TypeError, ValueError):
                pass
        if "audio_unlocked" in fields:
            st.audio_unlocked = bool(fields["audio_unlocked"])
            if st.audio_unlocked and st.sync_status == "needs_unlock":
                st.sync_status = "idle"
            elif not st.audio_unlocked:
                st.sync_status = "needs_unlock"

        await self.broadcast_speakers()

    async def update_telemetry(
        self,
        speaker_id: str,
        rtt_ms: float,
        drift_ms: float,
        sync_status: str,
    ) -> None:
        conn = self.speakers.get(speaker_id)
        if not conn:
            return
        st = conn.state
        st.rtt_ms = round(max(0.0, float(rtt_ms)), 1)
        st.drift_ms = round(float(drift_ms), 1)
        if sync_status in {"locked", "syncing", "buffering", "idle", "needs_unlock"}:
            st.sync_status = sync_status  # type: ignore[assignment]

        now = server_now_ms()
        if now - self._last_telemetry_broadcast >= 500.0:
            self._last_telemetry_broadcast = now
            await self.broadcast_speakers()

    async def auto_assign_stereo(self, actor_id: str) -> None:
        if not self.can_control_room(actor_id):
            return
        ordered = self._ordered_speakers()
        if not ordered:
            return
        if len(ordered) == 1:
            ordered[0].role = "stereo"
        else:
            sequence: list[SpeakerRole] = ["left", "right", "mono", "subwoofer"]
            for idx, sp in enumerate(ordered):
                sp.role = sequence[idx] if idx < len(sequence) else "stereo"
        await self.broadcast_speakers()

    async def identify_speaker(self, actor_id: str, target_id: str) -> None:
        if not self.can_control_room(actor_id) and actor_id != target_id:
            return
        payload = {"type": "identify_speaker", "server_now_ms": server_now_ms()}
        if target_id == "all":
            await self.broadcast(payload)
        else:
            await self.send_to(target_id, payload)

    async def set_calibration(self, actor_id: str, active: bool) -> None:
        if not self.can_control_room(actor_id):
            return
        if active:
            if self.playback.status == "playing":
                pos = self.get_current_position_sec()
                self.playback.status = "paused"
                self.playback.anchor_position_sec = pos
                self.playback.anchor_server_ms = server_now_ms()
            self.calibration.active = True
            self.calibration.anchor_server_ms = server_now_ms() + 500.0
        else:
            self.calibration.active = False
        await self.broadcast_room_state()

    # ------------------------------------------------------------------
    # Ready-Barrier Coordination
    # ------------------------------------------------------------------

    def mark_track_ready(self, speaker_id: str, generation: int) -> None:
        conn = self.speakers.get(speaker_id)
        if not conn:
            return
        conn.state.ready_generation = generation
        if conn.state.audio_unlocked and conn.state.sync_status == "buffering":
            conn.state.sync_status = "idle"
        self._check_ready_barrier()

    def _check_ready_barrier(self) -> None:
        if self._barrier_event is None or self._barrier_event.is_set():
            return
        unlocked = [
            conn.state for conn in self.speakers.values() if conn.state.audio_unlocked
        ]
        if not unlocked:
            self._barrier_event.set()
            return
        if all(sp.ready_generation == self._barrier_generation for sp in unlocked):
            self._barrier_event.set()

    # ------------------------------------------------------------------
    # Queue & Playback Orchestration
    # ------------------------------------------------------------------

    def _prefetch_next(self) -> None:
        if not self.queue or self.playback.queue_index < 0:
            return
        next_idx = self.playback.queue_index + 1
        if next_idx < len(self.queue):
            self.audio_cache.prefetch(self.queue[next_idx].video_id)
        elif self.repeat == "all" and len(self.queue) > 1:
            self.audio_cache.prefetch(self.queue[0].video_id)

    async def play_queue_index(self, index: int) -> None:
        """Load and play the queue track at `index` across all connected speakers."""
        if not (0 <= index < len(self.queue)):
            return

        if self._load_task and not self._load_task.done():
            self._load_task.cancel()

        pb = self.playback
        pb.generation += 1
        gen = pb.generation
        track = self.queue[index]

        pb.status = "loading"
        pb.loading_phase = "downloading"
        pb.loading_error = ""
        pb.track = track
        pb.queue_index = index
        pb.audio_ready = False
        pb.audio_url = None
        pb.audio_duration_sec = track.duration_sec
        pb.anchor_position_sec = 0.0
        pb.anchor_server_ms = server_now_ms()
        pb.use_streaming = False

        for conn in self.speakers.values():
            conn.state.ready_generation = -1
            if conn.state.audio_unlocked:
                conn.state.sync_status = "buffering"

        await self.broadcast_room_state()
        self._load_task = asyncio.create_task(self._load_and_start(gen, track))

    async def _load_and_start(self, generation: int, track: Track) -> None:
        try:
            info = await self.audio_cache.ensure_track(track.video_id)
            if self.playback.generation != generation:
                return

            track.cached = True
            duration_sec = float(info.get("duration_sec") or track.duration_sec or 0.0)
            if duration_sec > 0 and track.duration_sec <= 0:
                track.duration_sec = duration_sec

            pb = self.playback
            pb.audio_ready = True
            pb.audio_url = f"/api/audio/{track.video_id}"
            pb.audio_duration_sec = duration_sec
            pb.use_streaming = duration_sec > STREAMING_THRESHOLD_SEC
            pb.loading_phase = "buffering"

            barrier_event = asyncio.Event()
            self._barrier_event = barrier_event
            self._barrier_generation = generation

            await self.broadcast_room_state()

            unlocked_count = sum(
                1 for conn in self.speakers.values() if conn.state.audio_unlocked
            )
            if unlocked_count == 0:
                await asyncio.sleep(0.1)
            else:
                self._check_ready_barrier()
                if not barrier_event.is_set():
                    try:
                        await asyncio.wait_for(
                            barrier_event.wait(), timeout=READY_BARRIER_TIMEOUT_SEC
                        )
                    except asyncio.TimeoutError:
                        logger.debug(
                            "Ready barrier timed out for generation %d", generation
                        )

            if self.playback.generation != generation:
                return

            pb.status = "playing"
            pb.loading_phase = ""
            pb.anchor_position_sec = 0.0
            pb.anchor_server_ms = server_now_ms() + PLAY_LEAD_MS
            await self.broadcast_room_state()
            self._prefetch_next()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Failed loading track %s: %s", track.video_id, exc)
            if self.playback.generation == generation:
                self.playback.status = "stopped"
                self.playback.loading_phase = ""
                self.playback.loading_error = f"Unable to load audio: {exc}"
                await self.broadcast_room_state()

    async def play_now(self, actor_id: str, track: Track) -> None:
        if not self.can_control_room(actor_id):
            return
        track.cached = self.audio_cache.is_cached(track.video_id)
        if not self.queue or self.playback.queue_index < 0:
            self.queue.append(track)
            target_idx = len(self.queue) - 1
        else:
            insert_idx = min(len(self.queue), self.playback.queue_index + 1)
            self.queue.insert(insert_idx, track)
            target_idx = insert_idx
        await self.play_queue_index(target_idx)

    async def add_to_queue(
        self,
        actor_id: str,
        tracks: list[Track],
        play_next: bool = False,
        play_if_idle: bool = False,
    ) -> None:
        if not self.can_add_to_queue(actor_id) or not tracks:
            return
        for t in tracks:
            t.cached = self.audio_cache.is_cached(t.video_id)

        was_empty = len(self.queue) == 0
        if play_next and self.playback.queue_index >= 0 and self.can_control_room(actor_id):
            insert_at = min(len(self.queue), self.playback.queue_index + 1)
            for offset, t in enumerate(tracks):
                self.queue.insert(insert_at + offset, t)
        else:
            self.queue.extend(tracks)

        if (
            play_if_idle
            and self.can_control_room(actor_id)
            and (was_empty or self.playback.status == "stopped")
        ):
            start_idx = 0 if was_empty else max(0, len(self.queue) - len(tracks))
            await self.play_queue_index(start_idx)
        else:
            await self.broadcast_room_state()

    async def remove_queue_item(self, actor_id: str, index: int) -> None:
        if not self.can_control_room(actor_id):
            return
        if not (0 <= index < len(self.queue)):
            return
        self.queue.pop(index)
        if index < self.playback.queue_index:
            self.playback.queue_index -= 1
        elif index == self.playback.queue_index:
            if not self.queue:
                await self._stop_playback()
                return
            next_idx = min(index, len(self.queue) - 1)
            await self.play_queue_index(next_idx)
            return
        await self.broadcast_room_state()

    async def move_queue_item(self, actor_id: str, from_idx: int, to_idx: int) -> None:
        if not self.can_control_room(actor_id):
            return
        n = len(self.queue)
        if not (0 <= from_idx < n and 0 <= to_idx < n) or from_idx == to_idx:
            return
        item = self.queue.pop(from_idx)
        self.queue.insert(to_idx, item)

        cur = self.playback.queue_index
        if cur == from_idx:
            self.playback.queue_index = to_idx
        elif from_idx < cur <= to_idx:
            self.playback.queue_index -= 1
        elif to_idx <= cur < from_idx:
            self.playback.queue_index += 1
        await self.broadcast_room_state()

    async def clear_queue(self, actor_id: str) -> None:
        if not self.can_control_room(actor_id):
            return
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self.queue.clear()
        await self._stop_playback()

    async def _stop_playback(self) -> None:
        self.playback.generation += 1
        self.playback.status = "stopped"
        self.playback.loading_phase = ""
        self.playback.loading_error = ""
        self.playback.track = None
        self.playback.queue_index = -1
        self.playback.audio_ready = False
        self.playback.audio_url = None
        self.playback.audio_duration_sec = 0.0
        self.playback.anchor_position_sec = 0.0
        self.playback.anchor_server_ms = server_now_ms()
        await self.broadcast_room_state()

    async def pause(self, actor_id: str) -> None:
        if not self.can_control_room(actor_id):
            return
        if self.playback.status != "playing":
            return
        pos = self.get_current_position_sec()
        self.playback.status = "paused"
        self.playback.anchor_position_sec = pos
        self.playback.anchor_server_ms = server_now_ms()
        await self.broadcast_playback()

    async def resume(self, actor_id: str) -> None:
        if not self.can_control_room(actor_id):
            return
        if self.playback.status == "paused" and self.playback.audio_ready:
            self.calibration.active = False
            self.playback.generation += 1
            self.playback.status = "playing"
            self.playback.anchor_server_ms = server_now_ms() + PLAY_LEAD_MS
            await self.broadcast_room_state()
        elif self.playback.status == "stopped" and self.queue:
            idx = (
                self.playback.queue_index
                if 0 <= self.playback.queue_index < len(self.queue)
                else 0
            )
            await self.play_queue_index(idx)

    async def seek(self, actor_id: str, position_sec: float) -> None:
        if not self.can_control_room(actor_id):
            return
        if not self.playback.audio_ready:
            return
        max_dur = self.playback.audio_duration_sec or 0.0
        clamped = max(0.0, float(position_sec))
        if max_dur > 0:
            clamped = min(clamped, max(0.0, max_dur - 0.1))

        self.playback.anchor_position_sec = clamped
        if self.playback.status == "playing":
            self.playback.generation += 1
            self.playback.anchor_server_ms = server_now_ms() + PLAY_LEAD_MS
            await self.broadcast_playback(force_resync=True)
        else:
            self.playback.anchor_server_ms = server_now_ms()
            await self.broadcast_playback()

    async def force_resync(self, actor_id: str) -> None:
        if not self.can_control_room(actor_id):
            return
        if self.playback.status != "playing":
            return
        current_pos = self.get_current_position_sec()
        lead_ms = 450.0
        self.playback.generation += 1
        self.playback.anchor_position_sec = current_pos + (lead_ms / 1000.0)
        self.playback.anchor_server_ms = server_now_ms() + lead_ms
        await self.broadcast_playback(force_resync=True)

    async def next_track(self, actor_id: str | None = None, auto: bool = False) -> None:
        if actor_id is not None and not self.can_control_room(actor_id):
            return
        if not self.queue:
            await self._stop_playback()
            return

        cur = self.playback.queue_index
        if auto and self.repeat == "one" and 0 <= cur < len(self.queue):
            await self.play_queue_index(cur)
            return

        if self.shuffle and len(self.queue) > 1:
            choices = [i for i in range(len(self.queue)) if i != cur]
            await self.play_queue_index(random.choice(choices))
            return

        nxt = cur + 1
        if nxt < len(self.queue):
            await self.play_queue_index(nxt)
        elif self.repeat == "all":
            await self.play_queue_index(0)
        elif auto:
            await self._stop_playback()

    async def prev_track(self, actor_id: str) -> None:
        if not self.can_control_room(actor_id):
            return
        if not self.queue:
            return
        if self.playback.status == "playing" and self.get_current_position_sec() > 3.5:
            await self.seek(actor_id, 0.0)
            return

        cur = self.playback.queue_index
        if cur > 0:
            await self.play_queue_index(cur - 1)
        elif self.repeat == "all":
            await self.play_queue_index(len(self.queue) - 1)
        else:
            await self.seek(actor_id, 0.0)

    async def set_shuffle(self, actor_id: str, enabled: bool) -> None:
        if not self.can_control_room(actor_id):
            return
        self.shuffle = bool(enabled)
        await self.broadcast_room_state()

    async def set_repeat(self, actor_id: str, mode: str) -> None:
        if not self.can_control_room(actor_id):
            return
        if mode in _VALID_REPEAT:
            self.repeat = mode  # type: ignore[assignment]
            await self.broadcast_room_state()

    async def set_master_volume(
        self, actor_id: str, volume: float | None = None, muted: bool | None = None
    ) -> None:
        if not self.can_control_room(actor_id):
            return
        if volume is not None:
            self.master_volume = max(0.0, min(1.0, float(volume)))
        if muted is not None:
            self.master_muted = bool(muted)
        await self.broadcast_room_state()

    async def set_allow_guest_control(self, actor_id: str, allowed: bool) -> None:
        if not self.can_assign_leader(actor_id):
            return
        self.allow_guest_control = bool(allowed)
        await self.broadcast_room_state()

    async def set_allow_collab_queue(self, actor_id: str, allowed: bool) -> None:
        if not self.can_assign_leader(actor_id):
            return
        self.allow_collab_queue = bool(allowed)
        await self.broadcast_room_state()

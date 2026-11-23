"""Pydantic data models representing SoundMesh tracks, speakers, leaders, and room state."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field

DeviceType = Literal["desktop", "mobile", "tablet"]
SpeakerRole = Literal["stereo", "left", "right", "mono", "subwoofer"]
SyncStatus = Literal["locked", "syncing", "buffering", "idle", "needs_unlock"]
PlaybackStatus = Literal["stopped", "loading", "playing", "paused"]
LoadingPhase = Literal["", "downloading", "buffering"]
RepeatMode = Literal["off", "all", "one"]
AuthProvider = Literal["google", "guest"]


class Track(BaseModel):
    """Normalized YouTube Music / YouTube track metadata."""

    video_id: str
    title: str
    artist: str = "Unknown Artist"
    album: str = ""
    duration_sec: float = 0.0
    thumbnail: str = ""
    cached: bool = False


class SpeakerState(BaseModel):
    """State and telemetry for a single connected speaker device."""

    id: str
    name: str
    device_type: DeviceType = "desktop"
    user_id: str = ""
    user_name: str = ""
    user_avatar: str = ""
    auth_provider: AuthProvider = "guest"
    is_owner: bool = False
    is_leader: bool = False
    is_host: bool = False
    role: SpeakerRole = "stereo"
    volume: float = Field(default=1.0, ge=0.0, le=1.0)
    muted: bool = False
    offset_ms: float = Field(default=0.0, ge=-500.0, le=500.0)
    audio_unlocked: bool = False
    sync_status: SyncStatus = "needs_unlock"
    rtt_ms: float = 0.0
    drift_ms: float = 0.0
    ready_generation: int = -1
    connected_at: float = 0.0


class LeaderRequest(BaseModel):
    """Pending request from a room member asking to become the Room Leader."""

    speaker_id: str
    user_id: str
    name: str
    user_name: str
    user_avatar: str = ""
    requested_at: float = 0.0


class PlaybackState(BaseModel):
    """Authoritative synchronized playback timeline state."""

    status: PlaybackStatus = "stopped"
    loading_phase: LoadingPhase = ""
    loading_error: str = ""
    track: Track | None = None
    queue_index: int = -1
    anchor_server_ms: float = 0.0
    anchor_position_sec: float = 0.0
    playback_rate: float = 1.0
    generation: int = 0
    audio_ready: bool = False
    audio_url: str | None = None
    audio_duration_sec: float = 0.0
    use_streaming: bool = False


class CalibrationState(BaseModel):
    """Synchronized calibration click pulse state."""

    active: bool = False
    anchor_server_ms: float = 0.0
    interval_ms: float = 1000.0


class RoomSnapshot(BaseModel):
    """Complete room state snapshot sent on join and major transitions."""

    room_code: str
    room_name: str
    owner_user_id: str
    owner_name: str
    is_public: bool
    has_passcode: bool
    leader_speaker_id: str | None = None
    leader_user_id: str | None = None
    allow_collab_queue: bool = False
    pending_leader_requests: list[LeaderRequest] = Field(default_factory=list)
    server_now_ms: float
    playback: PlaybackState
    queue: list[Track]
    shuffle: bool
    repeat: RepeatMode
    master_volume: float
    master_muted: bool
    allow_guest_control: bool
    calibration: CalibrationState
    speakers: list[SpeakerState]

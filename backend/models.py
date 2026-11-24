"""Pydantic data models representing SoundMesh v2 tracks, speakers, spatial/EQ, and room state."""

from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator

from backend.security import sanitize_thumbnail_url

_TRACK_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

DeviceType = Literal["desktop", "mobile", "tablet"]
SpeakerRole = Literal["stereo", "left", "right", "mono", "subwoofer"]
SyncStatus = Literal["locked", "syncing", "buffering", "idle", "needs_unlock"]
PlaybackStatus = Literal["stopped", "loading", "playing", "paused"]
LoadingPhase = Literal["", "downloading", "buffering"]
RepeatMode = Literal["off", "all", "one"]
AuthProvider = Literal["google", "guest"]
EqPreset = Literal["flat", "party_bass", "hostel_boost", "vocal", "late_night"]
TrackSource = Literal["youtube", "local"]


class Track(BaseModel):
    """Normalized YouTube Music / local uploaded track metadata."""

    video_id: str
    title: str
    artist: str = "Unknown Artist"
    album: str = ""
    duration_sec: float = 0.0
    thumbnail: str = ""
    cached: bool = False
    votes: list[str] = Field(default_factory=list)
    source: TrackSource = "youtube"

    @field_validator("video_id")
    @classmethod
    def _check_video_id(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not _TRACK_VIDEO_ID_RE.match(cleaned):
            raise ValueError("Invalid video_id format")
        return cleaned

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        return (v or "Untitled").strip()[:200] or "Untitled"

    @field_validator("artist", "album")
    @classmethod
    def _clean_text(cls, v: str) -> str:
        return (v or "").strip()[:140]

    @field_validator("duration_sec")
    @classmethod
    def _clamp_duration(cls, v: float) -> float:
        return max(0.0, min(86400.0, float(v or 0.0)))

    @field_validator("thumbnail")
    @classmethod
    def _clean_thumbnail(cls, v: str) -> str:
        return sanitize_thumbnail_url(v)


class SpeakerState(BaseModel):
    """State, spatial position, EQ, and telemetry for a connected speaker device."""

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
    pos_x: float = Field(default=0.0, ge=-1.0, le=1.0)
    pos_y: float = Field(default=0.0, ge=-1.0, le=1.0)
    spatial_enabled: bool = False
    eq_preset: EqPreset = "flat"
    eq_bands: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
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
    party_lightshow: bool = False
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

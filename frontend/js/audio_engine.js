/**
 * Precision Web Audio Multi-Speaker Sync Engine.
 * Supports:
 * - Instant mobile/Safari unlock + WakeLock + MediaSession integration
 * - Live role routing (stereo, left, right, mono, subwoofer with DSP crossover)
 * - Precision decoded AudioBuffer mode with sub-ms scheduling + continuous drift PLL
 * - Streaming <audio> fallback mode for long tracks (> 12 min)
 * - Synchronized calibration click metronome + speaker identify chime
 */
export class SyncAudioEngine {
  constructor(clockSync, { onReady, onTelemetry, onIdentifyVisual } = {}) {
    this.clockSync = clockSync;
    this.onReady = onReady;
    this.onTelemetry = onTelemetry;
    this.onIdentifyVisual = onIdentifyVisual;

    this.audioCtx = null;
    this.unlocked = false;

    // Speaker & room gain state
    this.role = "stereo";
    this.speakerVolume = 1.0;
    this.speakerMuted = false;
    this.masterVolume = 1.0;
    this.masterMuted = false;
    this.offsetMs = 0.0;

    // DSP graph nodes
    this.splitter = null;
    this.gainLL = null;
    this.gainLR = null;
    this.gainRL = null;
    this.gainRR = null;
    this.subLowpass = null;
    this.subShelf = null;
    this.dryBus = null;
    this.subBus = null;
    this.merger = null;
    this.outputGain = null;
    this.analyser = null;

    // Decoded audio mode
    this.currentBuffer = null;
    this.currentVideoId = null;
    this._fetchAbort = null;
    this._activeSource = null;
    this._activeSourceGain = null;
    this._activeRate = 1.0;
    this._lastRateCtxTime = 0;
    this._integratedBufferPos = 0;

    // Streaming audio mode
    this._audioEl = null;
    this._mediaSource = null;
    this._streamingReadyUrl = null;

    // Playback state mirror
    this.playbackState = null;
    this.syncStatus = "needs_unlock";
    this.lastDriftMs = 0;
    this._driftTimer = null;

    // Calibration metronome
    this.calibrationState = { active: false, anchor_server_ms: 0, interval_ms: 1000 };
    this._calTimer = null;
    this._calNextIndex = -1;

    this._wakeLock = null;
  }

  // ------------------------------------------------------------------
  // Unlock & Audio Graph Initialization (MUST be synchronous on tap!)
  // ------------------------------------------------------------------

  unlock() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!this.audioCtx) {
      this.audioCtx = new AudioCtx({ latencyHint: "playback" });
      this._buildGraph();
    }

    if (this.audioCtx.state === "suspended") {
      this.audioCtx.resume().catch(() => {});
    }

    // Play a 1-sample silent buffer synchronously to unlock iOS Safari audio
    try {
      const silent = this.audioCtx.createBuffer(1, 1, this.audioCtx.sampleRate);
      const src = this.audioCtx.createBufferSource();
      src.buffer = silent;
      src.connect(this.outputGain);
      src.start(0);
    } catch {
      // Ignore if already unlocked
    }

    this.unlocked = true;
    this.syncStatus = "idle";
    this._requestWakeLock();
    this._startLoops();

    // If a track is already ready or playing, apply immediately
    if (this.playbackState) {
      this.applyPlaybackState(this.playbackState, true);
    }
    return true;
  }

  _buildGraph() {
    const ctx = this.audioCtx;

    this.splitter = ctx.createChannelSplitter(2);
    this.gainLL = ctx.createGain();
    this.gainLR = ctx.createGain();
    this.gainRL = ctx.createGain();
    this.gainRR = ctx.createGain();

    this.merger = ctx.createChannelMerger(2);

    this.dryBus = ctx.createGain();
    this.subBus = ctx.createGain();

    this.subLowpass = ctx.createBiquadFilter();
    this.subLowpass.type = "lowpass";
    this.subLowpass.frequency.value = 180;
    this.subLowpass.Q.value = 0.707;

    this.subShelf = ctx.createBiquadFilter();
    this.subShelf.type = "lowshelf";
    this.subShelf.frequency.value = 80;
    this.subShelf.gain.value = 6.0;

    this.outputGain = ctx.createGain();
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 128;
    this.analyser.smoothingTimeConstant = 0.78;

    // Matrix: L -> (LL -> outL, LR -> outR), R -> (RL -> outL, RR -> outR)
    this.splitter.connect(this.gainLL, 0);
    this.splitter.connect(this.gainLR, 0);
    this.splitter.connect(this.gainRL, 1);
    this.splitter.connect(this.gainRR, 1);

    this.gainLL.connect(this.merger, 0, 0);
    this.gainRL.connect(this.merger, 0, 0);
    this.gainLR.connect(this.merger, 0, 1);
    this.gainRR.connect(this.merger, 0, 1);

    // Dual bus: dry bypass + subwoofer crossover path
    this.merger.connect(this.dryBus);
    this.merger.connect(this.subLowpass);
    this.subLowpass.connect(this.subShelf);
    this.subShelf.connect(this.subBus);

    this.dryBus.connect(this.outputGain);
    this.subBus.connect(this.outputGain);

    this.outputGain.connect(this.analyser);
    this.analyser.connect(ctx.destination);

    this._applyRoleRouting(true);
    this._applyGain(true);
  }

  _startLoops() {
    if (this._driftTimer === null) {
      this._driftTimer = setInterval(() => this._runDriftCheck(), 1200);
    }
    if (this._calTimer === null) {
      this._calTimer = setInterval(() => this._scheduleCalibrationClicks(), 50);
    }
  }

  async _requestWakeLock() {
    try {
      if ("wakeLock" in navigator && !this._wakeLock) {
        this._wakeLock = await navigator.wakeLock.request("screen");
        this._wakeLock.addEventListener("release", () => {
          this._wakeLock = null;
        });
      }
    } catch {
      // WakeLock requires HTTPS on some mobile browsers; fail silently over LAN HTTP
    }
  }

  reacquireWakeLockIfNeeded() {
    if (this.unlocked && document.visibilityState === "visible") {
      this._requestWakeLock();
      if (this.audioCtx && this.audioCtx.state === "suspended") {
        this.audioCtx.resume().catch(() => {});
      }
    }
  }

  // ------------------------------------------------------------------
  // Settings: Role, Volume, Mute, Offset
  // ------------------------------------------------------------------

  setRole(role) {
    if (!["stereo", "left", "right", "mono", "subwoofer"].includes(role)) return;
    this.role = role;
    this._applyRoleRouting(false);
  }

  _applyRoleRouting(immediate = false) {
    if (!this.audioCtx) return;
    const now = this.audioCtx.currentTime;
    const tc = immediate ? 0.001 : 0.02;

    let ll = 1, lr = 0, rl = 0, rr = 1, dry = 1, sub = 0;
    switch (this.role) {
      case "left":
        ll = 1; lr = 1; rl = 0; rr = 0;
        break;
      case "right":
        ll = 0; lr = 0; rl = 1; rr = 1;
        break;
      case "mono":
        ll = 0.5; lr = 0.5; rl = 0.5; rr = 0.5;
        break;
      case "subwoofer":
        ll = 0.5; lr = 0.5; rl = 0.5; rr = 0.5;
        dry = 0;
        sub = 1;
        break;
      case "stereo":
      default:
        ll = 1; lr = 0; rl = 0; rr = 1;
        break;
    }

    const set = (node, val) => {
      node.gain.cancelScheduledValues(now);
      if (immediate) {
        node.gain.setValueAtTime(val, now);
      } else {
        node.gain.setTargetAtTime(val, now, tc);
      }
    };

    set(this.gainLL, ll);
    set(this.gainLR, lr);
    set(this.gainRL, rl);
    set(this.gainRR, rr);
    set(this.dryBus, dry);
    set(this.subBus, sub);
  }

  setVolumes({ speakerVolume, speakerMuted, masterVolume, masterMuted }) {
    if (speakerVolume !== undefined) this.speakerVolume = Math.max(0, Math.min(1, Number(speakerVolume)));
    if (speakerMuted !== undefined) this.speakerMuted = Boolean(speakerMuted);
    if (masterVolume !== undefined) this.masterVolume = Math.max(0, Math.min(1, Number(masterVolume)));
    if (masterMuted !== undefined) this.masterMuted = Boolean(masterMuted);
    this._applyGain(false);
  }

  _applyGain(immediate = false) {
    if (!this.audioCtx || !this.outputGain) return;
    const effective =
      this.speakerMuted || this.masterMuted
        ? 0.0
        : this.speakerVolume * this.masterVolume;
    const now = this.audioCtx.currentTime;
    this.outputGain.gain.cancelScheduledValues(now);
    if (immediate) {
      this.outputGain.gain.setValueAtTime(effective, now);
    } else {
      this.outputGain.gain.setTargetAtTime(effective, now, 0.02);
    }
  }

  setOffsetMs(ms) {
    const clamped = Math.max(-500, Math.min(500, Number(ms) || 0));
    const delta = Math.abs(clamped - this.offsetMs);
    this.offsetMs = clamped;
    if (this.unlocked && this.playbackState && this.playbackState.status === "playing") {
      if (delta >= 25) {
        this._reschedulePlaying(true);
      } else {
        this._runDriftCheck();
      }
    }
  }

  // ------------------------------------------------------------------
  // Hardware Output Latency Compensation
  // ------------------------------------------------------------------

  _getHwLatencySec() {
    if (!this.audioCtx) return 0;
    const out = this.audioCtx.outputLatency;
    if (typeof out === "number" && out > 0 && out < 1.0) return out;
    const base = this.audioCtx.baseLatency;
    if (typeof base === "number" && base > 0 && base < 0.5) return base;
    return 0;
  }

  // ------------------------------------------------------------------
  // Playback Synchronization
  // ------------------------------------------------------------------

  async applyPlaybackState(pb, forceResync = false) {
    const prev = this.playbackState;
    this.playbackState = pb ? { ...pb } : null;

    if (!pb || pb.status === "stopped" || !pb.track) {
      this._stopPlaybackNodes();
      this.syncStatus = this.unlocked ? "idle" : "needs_unlock";
      this._emitTelemetry(0);
      return;
    }

    this._updateMediaSession(pb);

    if (!this.unlocked) {
      this.syncStatus = "needs_unlock";
      return;
    }

    if (this.audioCtx && this.audioCtx.state === "suspended") {
      this.audioCtx.resume().catch(() => {});
    }

    // Check if track audio needs fetching/decoding
    if (pb.audio_ready && pb.audio_url) {
      if (pb.use_streaming) {
        this._prepareStreamingAudio(pb);
      } else if (this.currentVideoId !== pb.track.video_id || !this.currentBuffer) {
        await this._loadDecodedAudio(pb.track.video_id, pb.audio_url, pb.generation);
      } else if (pb.status === "loading" && typeof this.onReady === "function") {
        this.onReady(pb.generation);
      }
    } else if (pb.status === "loading") {
      this._stopPlaybackNodes();
      this.syncStatus = "buffering";
      this._emitTelemetry(0);
      return;
    }

    // Re-read latest playbackState after async decode
    const latest = this.playbackState;
    if (!latest || latest.generation !== pb.generation) {
      // Even if generation advanced while decoding, latest state handles itself
      if (!latest || latest.status !== "playing") return;
    }

    if (latest.status === "paused") {
      this._stopPlaybackNodes();
      this.syncStatus = "idle";
      this._emitTelemetry(0);
      return;
    }

    if (latest.status === "playing" && latest.audio_ready) {
      const sameGeneration = prev && prev.generation === latest.generation && prev.status === "playing";
      if (!sameGeneration || forceResync || (!this._activeSource && !latest.use_streaming)) {
        this._reschedulePlaying(forceResync);
      }
    }
  }

  async _loadDecodedAudio(videoId, audioUrl, generation) {
    if (this._fetchAbort) {
      this._fetchAbort.abort();
      this._fetchAbort = null;
    }
    this._stopPlaybackNodes();

    const controller = new AbortController();
    this._fetchAbort = controller;
    this.syncStatus = "buffering";
    this._emitTelemetry(0);

    try {
      const resp = await fetch(audioUrl, { signal: controller.signal });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const arrBuf = await resp.arrayBuffer();
      if (controller.signal.aborted) return;

      // Release old buffer before decoding to keep mobile RAM low
      this.currentBuffer = null;
      this.currentVideoId = null;

      const decoded = await this.audioCtx.decodeAudioData(arrBuf);
      if (controller.signal.aborted) return;

      this.currentBuffer = decoded;
      this.currentVideoId = videoId;
      this._fetchAbort = null;

      if (typeof this.onReady === "function") {
        this.onReady(generation);
      }

      if (this.playbackState && this.playbackState.status === "playing" && this.playbackState.audio_ready) {
        this._reschedulePlaying(true);
      }
    } catch (err) {
      if (err && err.name === "AbortError") return;
      console.error("Audio decode failed:", err);
      this.syncStatus = "buffering";
    }
  }

  _prepareStreamingAudio(pb) {
    if (!this._audioEl) {
      this._audioEl = document.createElement("audio");
      this._audioEl.crossOrigin = "anonymous";
      this._audioEl.preload = "auto";
      this._mediaSource = this.audioCtx.createMediaElementSource(this._audioEl);
      this._mediaSource.connect(this.splitter);
    }

    if (this._streamingReadyUrl !== pb.audio_url) {
      this._streamingReadyUrl = pb.audio_url;
      this._audioEl.src = pb.audio_url;
      this._audioEl.load();
    }

    if (typeof this.onReady === "function") {
      this.onReady(pb.generation);
    }
  }

  _stopPlaybackNodes() {
    if (this._activeSource) {
      const oldSrc = this._activeSource;
      const oldGain = this._activeSourceGain;
      this._activeSource = null;
      this._activeSourceGain = null;
      try {
        if (oldGain && this.audioCtx) {
          const now = this.audioCtx.currentTime;
          oldGain.gain.setValueAtTime(oldGain.gain.value, now);
          oldGain.gain.linearRampToValueAtTime(0, now + 0.018);
          oldSrc.stop(now + 0.022);
        } else {
          oldSrc.stop();
        }
      } catch {
        // Ignore already stopped
      }
    }
    if (this._audioEl && !this._audioEl.paused) {
      this._audioEl.pause();
    }
  }

  _reschedulePlaying(fadeIn = false) {
    const pb = this.playbackState;
    if (!pb || pb.status !== "playing" || !this.unlocked || !this.audioCtx) return;

    if (pb.use_streaming) {
      this._rescheduleStreaming(pb);
      return;
    }

    if (!this.currentBuffer || this.currentVideoId !== pb.track?.video_id) return;

    this._stopPlaybackNodes();

    const ctx = this.audioCtx;
    const hwLatencySec = this._getHwLatencySec();
    const targetStartPerfMs =
      pb.anchor_server_ms - this.clockSync.offsetMs + this.offsetMs - hwLatencySec * 1000;
    const delaySec = (targetStartPerfMs - performance.now()) / 1000.0;
    const rate = pb.playback_rate || 1.0;

    let whenCtxTime;
    let startOffsetSec;
    if (delaySec >= 0) {
      whenCtxTime = ctx.currentTime + delaySec;
      startOffsetSec = Math.max(0, pb.anchor_position_sec);
    } else {
      const safetyLead = 0.045;
      whenCtxTime = ctx.currentTime + safetyLead;
      startOffsetSec = Math.max(0, pb.anchor_position_sec + (-delaySec + safetyLead) * rate);
    }

    if (startOffsetSec >= this.currentBuffer.duration) {
      this.syncStatus = "idle";
      return;
    }

    const src = ctx.createBufferSource();
    src.buffer = this.currentBuffer;
    src.playbackRate.setValueAtTime(rate, ctx.currentTime);

    const srcGain = ctx.createGain();
    if (fadeIn) {
      srcGain.gain.setValueAtTime(0, whenCtxTime);
      srcGain.gain.linearRampToValueAtTime(1, whenCtxTime + 0.02);
    } else {
      srcGain.gain.setValueAtTime(1, ctx.currentTime);
    }

    src.connect(srcGain);
    srcGain.connect(this.splitter);
    src.start(whenCtxTime, startOffsetSec);

    this._activeSource = src;
    this._activeSourceGain = srcGain;
    this._activeRate = rate;
    this._lastRateCtxTime = whenCtxTime;
    this._integratedBufferPos = startOffsetSec;
    this.syncStatus = "locked";
    this.lastDriftMs = 0;
    this._emitTelemetry(0);
  }

  _rescheduleStreaming(pb) {
    if (!this._audioEl) return;
    const hwLatencySec = this._getHwLatencySec();
    const effectiveServerNow =
      this.clockSync.getServerTimeMs() - this.offsetMs + hwLatencySec * 1000;
    const expectedPos =
      pb.anchor_position_sec +
      Math.max(0, (effectiveServerNow - pb.anchor_server_ms) / 1000.0) * (pb.playback_rate || 1.0);
    this._audioEl.currentTime = Math.max(0, expectedPos);
    this._audioEl.playbackRate = pb.playback_rate || 1.0;
    this._audioEl.preservesPitch = true;
    this._audioEl.play().catch(() => {});
    this.syncStatus = "locked";
  }

  getExpectedPositionSec() {
    const pb = this.playbackState;
    if (!pb || !pb.track) return 0;
    if (pb.status !== "playing") return Math.max(0, pb.anchor_position_sec || 0);
    const serverNow = this.clockSync.getServerTimeMs();
    if (serverNow < pb.anchor_server_ms) return Math.max(0, pb.anchor_position_sec || 0);
    return Math.max(
      0,
      pb.anchor_position_sec + ((serverNow - pb.anchor_server_ms) / 1000.0) * (pb.playback_rate || 1.0)
    );
  }

  _getActualBufferPositionSec() {
    if (!this.audioCtx || !this._activeSource) return 0;
    const nowCtx = this.audioCtx.currentTime;
    if (nowCtx < this._lastRateCtxTime) {
      return this._integratedBufferPos;
    }
    return this._integratedBufferPos + (nowCtx - this._lastRateCtxTime) * this._activeRate;
  }

  _runDriftCheck() {
    const pb = this.playbackState;
    if (!this.unlocked || !this.audioCtx || !pb || pb.status !== "playing" || !pb.audio_ready) {
      return;
    }

    const hwLatencySec = this._getHwLatencySec();
    const effectiveServerNow =
      this.clockSync.getServerTimeMs() - this.offsetMs + hwLatencySec * 1000;

    if (effectiveServerNow < pb.anchor_server_ms) {
      return; // Still waiting for scheduled future start
    }

    const baseRate = pb.playback_rate || 1.0;
    const expectedPos =
      pb.anchor_position_sec + ((effectiveServerNow - pb.anchor_server_ms) / 1000.0) * baseRate;

    if (pb.use_streaming && this._audioEl) {
      if (this._audioEl.paused) {
        this._audioEl.play().catch(() => {});
      }
      const driftMs = (expectedPos - this._audioEl.currentTime) * 1000.0;
      this.lastDriftMs = driftMs;
      const absDrift = Math.abs(driftMs);
      if (absDrift < 8) {
        this._audioEl.playbackRate = baseRate;
        this.syncStatus = "locked";
      } else if (absDrift <= 120) {
        const adj = Math.max(-0.015, Math.min(0.015, driftMs / 4000.0));
        this._audioEl.playbackRate = baseRate + adj;
        this.syncStatus = "syncing";
      } else {
        this._audioEl.currentTime = Math.max(0, expectedPos);
        this._audioEl.playbackRate = baseRate;
        this.syncStatus = "syncing";
      }
      this._emitTelemetry(driftMs);
      return;
    }

    if (!this._activeSource || !this.currentBuffer) return;
    if (expectedPos >= this.currentBuffer.duration) return;

    const nowCtx = this.audioCtx.currentTime;
    const actualPos = this._getActualBufferPositionSec();
    const driftMs = (expectedPos - actualPos) * 1000.0;
    this.lastDriftMs = driftMs;
    const absDrift = Math.abs(driftMs);

    if (absDrift < 4) {
      if (this._activeRate !== baseRate) {
        this._integratedBufferPos = actualPos;
        this._lastRateCtxTime = nowCtx;
        this._activeRate = baseRate;
        this._activeSource.playbackRate.setTargetAtTime(baseRate, nowCtx, 0.05);
      }
      this.syncStatus = "locked";
    } else if (absDrift <= 80) {
      const adj = Math.max(-0.015, Math.min(0.015, driftMs / 4000.0));
      const nextRate = baseRate + adj;
      this._integratedBufferPos = actualPos;
      this._lastRateCtxTime = nowCtx;
      this._activeRate = nextRate;
      this._activeSource.playbackRate.setTargetAtTime(nextRate, nowCtx, 0.05);
      this.syncStatus = "syncing";
    } else {
      // Large drift (> 80ms) -> clean micro-crossfade hard resync
      this._reschedulePlaying(true);
      this.syncStatus = "syncing";
    }

    this._emitTelemetry(driftMs);
  }

  _emitTelemetry(driftMs) {
    if (typeof this.onTelemetry === "function") {
      this.onTelemetry({
        rtt_ms: this.clockSync.getRttMs(),
        drift_ms: driftMs,
        sync_status: this.syncStatus,
      });
    }
  }

  // ------------------------------------------------------------------
  // Calibration Click Metronome & Identify Ping
  // ------------------------------------------------------------------

  setCalibrationState(cal) {
    this.calibrationState = cal
      ? { ...cal }
      : { active: false, anchor_server_ms: 0, interval_ms: 1000 };
    this._calNextIndex = -1;
  }

  _scheduleCalibrationClicks() {
    if (!this.unlocked || !this.audioCtx || !this.calibrationState.active) return;

    const { anchor_server_ms: anchorServerMs, interval_ms: intervalMs } = this.calibrationState;
    if (!intervalMs || intervalMs <= 0) return;

    const ctx = this.audioCtx;
    const hwLatencyMs = this._getHwLatencySec() * 1000.0;
    const serverNow = this.clockSync.getServerTimeMs();
    const lookaheadMs = 200;

    // Anchor shifted by speaker offset & hw latency matches exact music playback timing
    const effectiveAnchorPerfMs =
      anchorServerMs - this.clockSync.offsetMs + this.offsetMs - hwLatencyMs;
    const nowPerf = performance.now();

    if (this._calNextIndex < 0) {
      const elapsed = serverNow - anchorServerMs;
      this._calNextIndex = Math.max(0, Math.ceil(elapsed / intervalMs));
    }

    while (true) {
      const clickPerfMs = effectiveAnchorPerfMs + this._calNextIndex * intervalMs;
      if (clickPerfMs > nowPerf + lookaheadMs) break;

      const delaySec = (clickPerfMs - nowPerf) / 1000.0;
      if (delaySec >= -0.02) {
        this._fireClickAt(ctx.currentTime + Math.max(0.005, delaySec), this._calNextIndex % 4 === 0);
      }
      this._calNextIndex += 1;
    }
  }

  _fireClickAt(whenCtx, accent = false) {
    const ctx = this.audioCtx;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(accent ? 1600 : 1200, whenCtx);

    gain.gain.setValueAtTime(0.0001, whenCtx);
    gain.gain.exponentialRampToValueAtTime(accent ? 0.65 : 0.45, whenCtx + 0.002);
    gain.gain.exponentialRampToValueAtTime(0.0001, whenCtx + 0.022);

    osc.connect(gain);
    gain.connect(this.outputGain);
    osc.start(whenCtx);
    osc.stop(whenCtx + 0.025);
  }

  playIdentifyChime() {
    if (!this.unlocked || !this.audioCtx) return;
    if (this.audioCtx.state === "suspended") {
      this.audioCtx.resume().catch(() => {});
    }
    const ctx = this.audioCtx;
    const now = ctx.currentTime + 0.01;
    const notes = [880, 1318.5];
    notes.forEach((freq, idx) => {
      const start = now + idx * 0.14;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "triangle";
      osc.frequency.setValueAtTime(freq, start);

      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.45, start + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.28);

      // Connect straight to analyser->destination so even muted speakers can be located
      osc.connect(gain);
      gain.connect(this.analyser);
      osc.start(start);
      osc.stop(start + 0.3);
    });

    if (typeof this.onIdentifyVisual === "function") {
      this.onIdentifyVisual();
    }
  }

  // ------------------------------------------------------------------
  // Visualizer & Media Session
  // ------------------------------------------------------------------

  getFrequencyData(targetUint8Array) {
    if (!this.analyser || !this.unlocked) {
      targetUint8Array.fill(0);
      return false;
    }
    this.analyser.getByteFrequencyData(targetUint8Array);
    return true;
  }

  _updateMediaSession(pb) {
    if (!("mediaSession" in navigator) || !pb.track) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: pb.track.title,
        artist: pb.track.artist,
        album: pb.track.album || "SoundMesh Multi-Speaker",
        artwork: pb.track.thumbnail
          ? [{ src: pb.track.thumbnail, sizes: "512x512", type: "image/jpeg" }]
          : [],
      });
      navigator.mediaSession.playbackState =
        pb.status === "playing" ? "playing" : pb.status === "paused" ? "paused" : "none";
    } catch {
      // Ignore MediaSession unsupported errors
    }
  }
}

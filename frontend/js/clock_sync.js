/**
 * High-precision NTP-style clock synchronization over WebSocket.
 * Keeps client-to-server monotonic clock offset so all speakers share
 * a sub-10ms unified timeline: serverNowMs = performance.now() + offsetMs.
 */
export class ClockSync {
  constructor(sendPingFn, onSyncUpdateFn = null) {
    this.sendPingFn = sendPingFn;
    this.onSyncUpdateFn = onSyncUpdateFn;

    this.samples = [];
    this.maxSamples = 16;
    this.offsetMs = 0;
    this.rttMs = 0;
    this.jitterMs = 0;
    this.synced = false;

    this._seq = 0;
    this._steadyTimer = null;
    this._burstTimers = [];
  }

  reset() {
    this.stop();
    this.samples = [];
    this.synced = false;
  }

  startInitialBurst() {
    this.stop();
    // 12 rapid pings spaced 65ms apart to lock clock quickly on join
    for (let i = 0; i < 12; i++) {
      const timer = setTimeout(() => {
        this._sendOnePing();
      }, i * 65);
      this._burstTimers.push(timer);
    }

    this._steadyTimer = setInterval(() => {
      this._sendOnePing();
    }, 3000);
  }

  triggerQuickResync() {
    for (let i = 0; i < 5; i++) {
      const timer = setTimeout(() => {
        this._sendOnePing();
      }, i * 60);
      this._burstTimers.push(timer);
    }
  }

  stop() {
    for (const t of this._burstTimers) {
      clearTimeout(t);
    }
    this._burstTimers = [];
    if (this._steadyTimer !== null) {
      clearInterval(this._steadyTimer);
      this._steadyTimer = null;
    }
  }

  _sendOnePing() {
    if (typeof this.sendPingFn !== "function") return;
    this._seq += 1;
    this.sendPingFn({
      type: "sync_ping",
      seq: this._seq,
      t0: performance.now(),
    });
  }

  onPong(msg) {
    const t3 = performance.now();
    const t0 = Number(msg.t0);
    const t1 = Number(msg.t1);
    const t2 = Number(msg.t2);

    if (!Number.isFinite(t0) || !Number.isFinite(t1) || !Number.isFinite(t2)) {
      return;
    }

    const rtt = Math.max(0, t3 - t0 - (t2 - t1));
    const offset = (t1 - t0 + (t2 - t3)) / 2;

    this.samples.push({ rtt, offset, receivedAt: t3 });
    if (this.samples.length > this.maxSamples) {
      this.samples.shift();
    }

    // Select lowest-RTT 40% of samples (min 1, max 6) to filter Wi-Fi spikes
    const sortedByRtt = [...this.samples].sort((a, b) => a.rtt - b.rtt);
    const keepCount = Math.max(1, Math.min(6, Math.ceil(sortedByRtt.length * 0.4)));
    const best = sortedByRtt.slice(0, keepCount);

    const offsets = best.map((s) => s.offset).sort((a, b) => a - b);
    const mid = Math.floor(offsets.length / 2);
    const medianOffset =
      offsets.length % 2 === 1
        ? offsets[mid]
        : (offsets[mid - 1] + offsets[mid]) / 2;

    const minRtt = best[0].rtt;
    const meanRtt = best.reduce((acc, s) => acc + s.rtt, 0) / best.length;
    const variance =
      best.reduce((acc, s) => acc + Math.pow(s.rtt - meanRtt, 2), 0) /
      best.length;

    if (!this.synced || this.samples.length <= 8 || Math.abs(medianOffset - this.offsetMs) > 40) {
      this.offsetMs = medianOffset;
    } else {
      this.offsetMs = this.offsetMs * 0.75 + medianOffset * 0.25;
    }

    this.rttMs = minRtt;
    this.jitterMs = Math.sqrt(variance);
    this.synced = true;

    if (typeof this.onSyncUpdateFn === "function") {
      this.onSyncUpdateFn({
        offsetMs: this.offsetMs,
        rttMs: this.rttMs,
        jitterMs: this.jitterMs,
        synced: this.synced,
      });
    }
  }

  getServerTimeMs() {
    return performance.now() + this.offsetMs;
  }

  getRttMs() {
    return this.rttMs;
  }

  getJitterMs() {
    return this.jitterMs;
  }

  isSynced() {
    return this.synced;
  }
}

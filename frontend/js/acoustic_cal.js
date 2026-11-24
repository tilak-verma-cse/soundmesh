/**
 * Acoustic Self-Calibration Helper.
 * 1. Emits timed linear chirps (2kHz -> 7kHz) for room acoustic testing.
 * 2. Listens via device microphone to measure pulse arrival phase vs server clock
 *    and compute suggested offsetMs + approximate speaker distance.
 */
export class AcousticCalibrator {
  constructor(audioCtx, clockSync) {
    this.audioCtx = audioCtx;
    this.clockSync = clockSync;
  }

  emitChirpAt(whenCtx, targetNode) {
    if (!this.audioCtx || !targetNode) return;
    const ctx = this.audioCtx;
    const durationSec = 0.045;
    const sampleRate = ctx.sampleRate;
    const length = Math.floor(sampleRate * durationSec);
    const buffer = ctx.createBuffer(1, length, sampleRate);
    const data = buffer.getChannelData(0);

    const f0 = 2000;
    const f1 = 7000;
    for (let i = 0; i < length; i++) {
      const t = i / sampleRate;
      const frac = t / durationSec;
      const env = Math.sin(Math.PI * frac);
      const phase = 2 * Math.PI * (f0 * t + ((f1 - f0) / (2 * durationSec)) * t * t);
      data[i] = env * 0.6 * Math.sin(phase);
    }

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(targetNode);
    src.start(Math.max(ctx.currentTime + 0.01, whenCtx));
  }

  async measureOffsetFromPulse(calibrationState, durationMs = 10500, onProgress = null) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("Microphone access is not supported in this browser.");
    }
    if (!calibrationState?.active) {
      throw new Error("Turn on Calibrate Pulse first so speakers are clicking.");
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });

    const AudioCtx = globalThis.AudioContext || globalThis.webkitAudioContext;
    const listenCtx = this.audioCtx || new AudioCtx();
    if (listenCtx.state === "suspended") {
      await listenCtx.resume();
    }

    const micSource = listenCtx.createMediaStreamSource(stream);
    const bandpass = listenCtx.createBiquadFilter();
    bandpass.type = "bandpass";
    bandpass.frequency.value = 1400;
    bandpass.Q.value = 1.5;

    const analyser = listenCtx.createAnalyser();
    analyser.fftSize = 256;
    micSource.connect(bandpass);
    bandpass.connect(analyser);

    const timeData = new Float32Array(analyser.fftSize);
    const intervalMs = calibrationState.interval_ms || 1000;
    const anchorMs = calibrationState.anchor_server_ms || 0;
    const samples = [];

    try {
      const startPerf = performance.now();
      let lastSec = -1;
      while (performance.now() - startPerf < durationMs) {
        const elapsed = performance.now() - startPerf;
        if (typeof onProgress === "function") {
          const secLeft = Math.max(1, Math.ceil((durationMs - elapsed) / 1000));
          if (secLeft !== lastSec) {
            lastSec = secLeft;
            onProgress(secLeft);
          }
        }
        analyser.getFloatTimeDomainData(timeData);
        let peak = 0;
        for (let i = 0; i < timeData.length; i++) {
          const abs = Math.abs(timeData[i]);
          if (abs > peak) peak = abs;
        }
        const serverNow = this.clockSync.getServerTimeMs();
        const phaseMs = ((serverNow - anchorMs) % intervalMs + intervalMs) % intervalMs;
        // Convert phase into signed [-interval/2, +interval/2]
        const signedMs = phaseMs > intervalMs / 2 ? phaseMs - intervalMs : phaseMs;
        samples.push({ peak, signedMs });
        await new Promise((r) => setTimeout(r, 4));
      }
    } finally {
      try {
        micSource.disconnect();
        bandpass.disconnect();
      } catch {
        // Ignore
      }
      stream.getTracks().forEach((t) => t.stop());
    }

    if (!samples.length) {
      throw new Error("No audio samples captured.");
    }

    // Average background noise floor and find strongest click transient
    const sortedPeaks = [...samples].sort((a, b) => a.peak - b.peak);
    const medianNoise = sortedPeaks[Math.floor(sortedPeaks.length * 0.5)].peak;
    const maxSample = sortedPeaks[sortedPeaks.length - 1];

    if (maxSample.peak < Math.max(0.015, medianNoise * 2.2)) {
      throw new Error("Could not hear calibration click clearly. Move closer or raise volume.");
    }

    // Take median phase among top transient peaks (> 70% of max peak)
    const threshold = maxSample.peak * 0.7;
    const hits = samples
      .filter((s) => s.peak >= threshold)
      .map((s) => s.signedMs)
      .sort((a, b) => a - b);
    const measuredLagMs = hits[Math.floor(hits.length / 2)];

    // If speaker sound arrived +lagMs late, negative offset compensates earlier
    const suggestedOffsetMs = Math.max(-500, Math.min(500, Math.round(-measuredLagMs)));
    const distanceMeters = Math.max(0, (Math.abs(measuredLagMs) * 0.343).toFixed(2));

    return {
      measuredLagMs: Math.round(measuredLagMs * 10) / 10,
      suggestedOffsetMs,
      distanceMeters: Number(distanceMeters),
    };
  }
}

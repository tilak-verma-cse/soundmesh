# SoundMesh v2 — Multi-Room Spatial Audio & YouTube Music Sync

SoundMesh turns phones, tablets, and laptops into a **millisecond-synchronized multi-speaker array** playing YouTube Music, personal Google YouTube playlists, and local MP3/WAV uploads — with **3 structural UI modes**, **dynamic Room Leader delegation**, **2D Spatial Room Mapping**, **5-Band EQ**, and **Acoustic Mic Auto-Calibration**.

---

## Three Adaptive UI Modes (Switch Anytime)

- **Minimal (Default):** Clean Google-style player (with Light & Dark themes) that folds complex speaker & EQ controls into slide-over drawers so anyone can join and enjoy music in seconds.
- **Studio Pro:** Frosted obsidian 3-column command deck with live RTT/Drift telemetry, **2D Spatial Room Map** (drag speakers around Front/Rear/Left/Right), **5-Band EQ + Limiter**, and **Acoustic Auto-Sync**.
- **Party Stage:** Full-screen ambient lightshow pulsing in sync with the bass, toggleable **Live Lyrics**, and floating live **Emoji Reactions**.

---

## Clean 3-Tier Architecture

- **Frontend (`frontend/`):** Zero-build ES module **PWA** (`manifest.json`, `sw.js`) with `env.js` Bearer token auth and `vercel.json` — ready for Vercel, Render, or wrapping into **Capacitor (Android Play Store)** and **Tauri (Windows/Linux)**.
- **Backend (`backend/`):** FastAPI + WebSockets + `ytmusicapi` + `yt-dlp` (`deno`) + local MP3/WAV upload + CORS + NTP clock sync + Ready-Barrier scheduler.
- **Database (`backend/db.py`):** Automatic **Supabase PostgreSQL (`DATABASE_URL`)** in production or zero-setup **SQLite** locally.

---

## Quick Start (Local PC)

1. Double-click **[start.bat](start.bat)** for home Wi-Fi, or **[go_live.bat](go_live.bat)** for an instant public `https://*.trycloudflare.com` link.
2. See **[DEPLOY_FREE.md](DEPLOY_FREE.md)** for **Render + Supabase + Vercel** free cloud setup.

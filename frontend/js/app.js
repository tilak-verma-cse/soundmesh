import { apiFetch, apiUrl, exchangePendingOauthTicket, setSessionToken } from "./env.js";
import { ClockSync } from "./clock_sync.js";
import { WSClient } from "./ws_client.js";
import { EQ_PRESETS, SyncAudioEngine } from "./audio_engine.js";

// ------------------------------------------------------------------
// Helpers, SVG Icons & Local Identity
// ------------------------------------------------------------------

const el = (id) => document.getElementById(id);

function svgIcon(name, size = 16) {
  return `<svg class="svg-i" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function getOrCreateClientId() {
  let id = localStorage.getItem("soundmesh_client_id");
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14)).slice(0, 12);
    localStorage.setItem("soundmesh_client_id", id);
  }
  return id;
}

function getOrCreateReconnectKey() {
  let key = localStorage.getItem("soundmesh_reconnect_key");
  if (!key || key.length < 16) {
    const raw = crypto.randomUUID
      ? crypto.randomUUID().replaceAll("-", "")
      : (Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2)).slice(0, 32);
    key = raw.slice(0, 32);
    localStorage.setItem("soundmesh_reconnect_key", key);
  }
  return key;
}

function detectDeviceType() {
  const ua = navigator.userAgent || "";
  if (/iPad|Tablet|PlayBook|Silk/i.test(ua)) return "tablet";
  if (/Mobi|Android|iPhone|iPod/i.test(ua)) return "mobile";
  return "desktop";
}

function defaultDeviceLabel(type) {
  if (type === "mobile") return "Phone";
  if (type === "tablet") return "Tablet";
  return "Studio";
}

function escapeHtml(str) {
  return String(str || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (hrs > 0) return `${hrs}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function safeImageUrl(raw) {
  const s = String(raw || "").trim();
  if (!s) return "";
  if (s.startsWith("data:image/png;base64,")) return s;
  if (/["'()\\\s<>]/.test(s)) return "";
  try {
    const u = new URL(s);
    if (u.protocol === "https:") return u.href;
  } catch {
    return "";
  }
  return "";
}

function avatarHtml(name, avatarUrl, sizeClass = "avatar-sm") {
  const safeAvatar = safeImageUrl(avatarUrl);
  if (safeAvatar) {
    return `<img class="${sizeClass}" src="${escapeHtml(safeAvatar)}" alt="" referrerpolicy="no-referrer" />`;
  }
  const initial = (String(name || "?").trim()[0] || "?").toUpperCase();
  return `<span class="${sizeClass}">${escapeHtml(initial)}</span>`;
}

function showToast(text) {
  const container = el("toastContainer");
  if (!container) return;
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = text;
  container.appendChild(node);
  setTimeout(() => node.remove(), 3600);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const deviceType = detectDeviceType();
const clientId = getOrCreateClientId();
const reconnectKey = getOrCreateReconnectKey();
let mySpeakerId = clientId;
const savedRole = localStorage.getItem("soundmesh_role") || "stereo";
const savedOffsetMs = Number(localStorage.getItem("soundmesh_offset_ms") || "0") || 0;
let currentUiMode = localStorage.getItem("soundmesh_ui_mode") || "minimal";
let currentMinimalTheme = localStorage.getItem("soundmesh_minimal_theme") || "dark";
let activeRoomPage = "stage";
const expandedSpeakerIds = new Set();

// ------------------------------------------------------------------
// Dynamic Chameleon Album Color Engine
// ------------------------------------------------------------------

const CURATED_PALETTES = [
  { r: 255, g: 107, b: 74, a2: "#ffb043" },
  { r: 255, g: 149, b: 56, a2: "#ffd166" },
  { r: 24, g: 210, b: 146, a2: "#64dfdf" },
  { r: 244, g: 93, b: 128, a2: "#ff9a76" },
  { r: 130, g: 115, b: 255, a2: "#ff7eb3" },
  { r: 58, g: 168, b: 255, a2: "#72efdd" },
];

let activeAccentRgb = { r: 255, g: 107, b: 74 };
let lastPaletteKey = "";

function setRootAccent(r, g, b, secondaryHex) {
  activeAccentRgb = { r, g, b };
  const root = document.documentElement;
  root.style.setProperty("--accent", `rgb(${r}, ${g}, ${b})`);
  root.style.setProperty("--accent-rgb", `${r}, ${g}, ${b}`);
  root.style.setProperty("--accent-2", secondaryHex);
}

function applyFallbackPaletteFromSeed(seedStr) {
  if (!seedStr) {
    setRootAccent(255, 107, 74, "#ffb043");
    return;
  }
  let hash = 0;
  for (let i = 0; i < seedStr.length; i++) {
    hash = (hash * 31 + seedStr.charCodeAt(i)) >>> 0;
  }
  const p = CURATED_PALETTES[hash % CURATED_PALETTES.length];
  setRootAccent(p.r, p.g, p.b, p.a2);
}

function extractPaletteFromImage(imgEl, fallbackSeed) {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 24;
    canvas.height = 24;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    if (!ctx) {
      applyFallbackPaletteFromSeed(fallbackSeed);
      return;
    }
    ctx.drawImage(imgEl, 0, 0, 24, 24);
    const data = ctx.getImageData(0, 0, 24, 24).data;

    let bestScore = -1;
    let bestR = 255;
    let bestG = 107;
    let bestB = 74;

    for (let i = 0; i < data.length; i += 4) {
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      const lum = (max + min) / 510;
      const sat = max === 0 ? 0 : (max - min) / max;
      if (lum < 0.18 || lum > 0.86 || sat < 0.25) continue;
      const score = sat * 1.4 + (1 - Math.abs(lum - 0.54));
      if (score > bestScore) {
        bestScore = score;
        bestR = r;
        bestG = g;
        bestB = b;
      }
    }

    if (bestScore < 0) {
      applyFallbackPaletteFromSeed(fallbackSeed);
      return;
    }

    const boost = (v) => Math.min(255, Math.max(65, Math.round(v * 1.08 + 12)));
    const r1 = boost(bestR);
    const g1 = boost(bestG);
    const b1 = boost(bestB);
    const r2 = Math.min(255, Math.round(r1 * 0.8 + 55));
    const g2 = Math.min(255, Math.round(g1 * 0.85 + 38));
    const b2 = Math.min(255, Math.round(b1 * 0.75 + 25));
    setRootAccent(r1, g1, b1, `rgb(${r2}, ${g2}, ${b2})`);
  } catch {
    applyFallbackPaletteFromSeed(fallbackSeed);
  }
}

// ------------------------------------------------------------------
// DOM Elements
// ------------------------------------------------------------------

const brandHomeBtn = el("brandHomeBtn");
const leaveRoomBtn = el("leaveRoomBtn");
const roomHeaderCluster = el("roomHeaderCluster");
const roomCodeBadge = el("roomCodeBadge");
const headerRoomName = el("headerRoomName");
const headerRoomCode = el("headerRoomCode");
const headerSyncDot = el("headerSyncDot");
const headerSpeakerCount = el("headerSpeakerCount");
const headerRttText = el("headerRttText");

const layoutMenuWrap = el("layoutMenuWrap");
const layoutMenuBtn = el("layoutMenuBtn");
const layoutDropdownMenu = el("layoutDropdownMenu");
const activeLayoutLabel = el("activeLayoutLabel");
const uiModeSwitcher = el("uiModeSwitcher");
const themeToggleBtn = el("themeToggleBtn");
const themeToggleLabel = el("themeToggleLabel");
const installPwaBtn = el("installPwaBtn");

const roomTopControls = el("roomTopControls");
const openQrBtn = el("openQrBtn");
const calibrateToggleBtn = el("calibrateToggleBtn");
const guestControlWrap = el("guestControlWrap");
const guestControlToggle = el("guestControlToggle");
const masterMuteBtn = el("masterMuteBtn");
const masterVolumeSlider = el("masterVolumeSlider");
const masterVolumeLabel = el("masterVolumeLabel");

const navAccountArea = el("navAccountArea");
const accountDropdownMenu = el("accountDropdownMenu");
const navDropName = el("navDropName");
const navDropMeta = el("navDropMeta");
const navDropGoogleBtn = el("navDropGoogleBtn");
const navDropYtBtn = el("navDropYtBtn");
const navDropLogoutBtn = el("navDropLogoutBtn");

const lobbyView = el("lobbyView");
const publicHostBar = el("publicHostBar");
const publicHostInactive = el("publicHostInactive");
const publicHostActive = el("publicHostActive");
const startTunnelBtn = el("startTunnelBtn");
const livePublicLinkAnchor = el("livePublicLinkAnchor");
const copyLiveLinkBtn = el("copyLiveLinkBtn");
const stopTunnelBtn = el("stopTunnelBtn");
const lobbyLanNotice = el("lobbyLanNotice");
const accountModeBadge = el("accountModeBadge");
const lobbySignedOut = el("lobbySignedOut");
const lobbyGoogleLoginBtn = el("lobbyGoogleLoginBtn");
const includeYoutubeScopeCheck = el("includeYoutubeScopeCheck");
const guestLoginForm = el("guestLoginForm");
const guestNameInput = el("guestNameInput");
const lobbySignedIn = el("lobbySignedIn");
const profileAvatarWrap = el("profileAvatarWrap");
const profileNameText = el("profileNameText");
const profileEmailText = el("profileEmailText");
const profileYtBadge = el("profileYtBadge");
const upgradeGoogleBtn = el("upgradeGoogleBtn");
const connectYtScopeBtn = el("connectYtScopeBtn");
const lobbyLogoutBtn = el("lobbyLogoutBtn");

const lobbyHubTabs = el("lobbyHubTabs");
const hubTabJoin = el("hubTabJoin");
const hubTabCreate = el("hubTabCreate");
const hubTabBrowse = el("hubTabBrowse");
const createRoomForm = el("createRoomForm");
const createRoomNameInput = el("createRoomNameInput");
const createRoomVisibility = el("createRoomVisibility");
const createRoomPasscode = el("createRoomPasscode");
const joinCodeForm = el("joinCodeForm");
const joinCodeInput = el("joinCodeInput");
const joinPasscodeInput = el("joinPasscodeInput");
const lobbyRoomTabs = el("lobbyRoomTabs");
const refreshRoomsBtn = el("refreshRoomsBtn");
const roomsDirectoryGrid = el("roomsDirectoryGrid");

const roomView = el("roomView");
const reactionFloatLayer = el("reactionFloatLayer");
const connectionBanner = el("connectionBanner");
const calibrationBanner = el("calibrationBanner");
const stopCalibrationBtn = el("stopCalibrationBtn");

const desktopPageNav = el("desktopPageNav");
const desktopSearchTab = el("desktopSearchTab");
const queueTabBadge = el("queueTabBadge");
const localAudioFileInput = el("localAudioFileInput");

const unlockOverlay = el("unlockOverlay");
const unlockRoomBadge = el("unlockRoomBadge");
const unlockNameInput = el("unlockNameInput");
const unlockRoleSelect = el("unlockRoleSelect");
const unlockBtn = el("unlockBtn");
const controllerOnlyBtn = el("controllerOnlyBtn");

const searchForm = el("searchForm");
const searchInput = el("searchInput");
const searchSubmitBtn = el("searchSubmitBtn");
const searchFilterTabs = el("searchFilterTabs");
const quickChips = el("quickChips");
const collectionHeader = el("collectionHeader");
const collectionBackBtn = el("collectionBackBtn");
const collectionTitle = el("collectionTitle");
const collectionSubtitle = el("collectionSubtitle");
const collectionPlayAllBtn = el("collectionPlayAllBtn");
const collectionQueueAllBtn = el("collectionQueueAllBtn");
const searchStatus = el("searchStatus");
const searchResults = el("searchResults");

const stageBackdrop = el("stageBackdrop");
const haloCanvas = el("haloCanvas");
const nowArtwork = el("nowArtwork");
const nowArtworkPlaceholder = el("nowArtworkPlaceholder");
const loadingOverlay = el("loadingOverlay");
const loadingPhaseText = el("loadingPhaseText");
const nowTitle = el("nowTitle");
const nowArtist = el("nowArtist");
const nowError = el("nowError");
const lyricsPanel = el("lyricsPanel");
const closeLyricsBtn = el("closeLyricsBtn");
const lyricsBody = el("lyricsBody");
const visualizerCanvas = el("visualizerCanvas");
const timeCurrent = el("timeCurrent");
const timeDuration = el("timeDuration");
const seekSlider = el("seekSlider");

const shuffleBtn = el("shuffleBtn");
const prevBtn = el("prevBtn");
const playPauseBtn = el("playPauseBtn");
const nextBtn = el("nextBtn");
const repeatBtn = el("repeatBtn");
const repeatOneBadge = el("repeatOneBadge");
const reactMenuBtn = el("reactMenuBtn");
const reactPopover = el("reactPopover");
const stageOptionsBtn = el("stageOptionsBtn");
const stageOptionsMenu = el("stageOptionsMenu");
const toggleLyricsBtn = el("toggleLyricsBtn");
const resyncAllBtn = el("resyncAllBtn");
const loadRadioBtn = el("loadRadioBtn");
const queueAutoRadioBtn = el("queueAutoRadioBtn");
const reactionBar = el("reactionBar");
const queueCountBadge = el("queueCountBadge");
const clearQueueBtn = el("clearQueueBtn");
const queueList = el("queueList");

const speakerToolsMenuBtn = el("speakerToolsMenuBtn");
const speakerToolsDropdown = el("speakerToolsDropdown");
const autoStereoBtn = el("autoStereoBtn");
const pingAllBtn = el("pingAllBtn");
const leaderBannerName = el("leaderBannerName");
const leaderBannerDevice = el("leaderBannerDevice");
const requestLeaderBtn = el("requestLeaderBtn");
const takeLeaderBtn = el("takeLeaderBtn");
const leaderOwnerControls = el("leaderOwnerControls");
const muteMeControllerToggle = el("muteMeControllerToggle");
const collabQueueToggle = el("collabQueueToggle");
const leaderRequestsDrawer = el("leaderRequestsDrawer");

const speakerSubTabs = el("speakerSubTabs");
const spTabList = el("spTabList");
const spTabMap = el("spTabMap");
const spTabEq = el("spTabEq");
const spTabSync = el("spTabSync");
const speakerList = el("speakerList");

const spatialEnableToggle = el("spatialEnableToggle");
const resetSpatialPosBtn = el("resetSpatialPosBtn");
const spatialPresetsRow = el("spatialPresetsRow");
const spatialRoomCanvas = el("spatialRoomCanvas");
const spatialNodesLayer = el("spatialNodesLayer");

const eqPresetButtons = el("eqPresetButtons");
const eqBandsContainer = el("eqBandsContainer");
const eqActivePresetLabel = el("eqActivePresetLabel");
const eqCurvePath = el("eqCurvePath");
const eqCurveArea = el("eqCurveArea");

const localSyncBadge = el("localSyncBadge");
const localRttStat = el("localRttStat");
const localDriftStat = el("localDriftStat");
const localOffsetStat = el("localOffsetStat");
const sonarCard = el("sonarCard");
const micAutoCalBtn = el("micAutoCalBtn");
const acousticResultText = el("acousticResultText");

const qrModal = el("qrModal");
const closeQrBtn = el("closeQrBtn");
const qrRoomCodeText = el("qrRoomCodeText");
const qrImage = el("qrImage");
const qrIpPickerWrap = el("qrIpPickerWrap");
const qrIpSelect = el("qrIpSelect");
const qrUrlInput = el("qrUrlInput");
const copyUrlBtn = el("copyUrlBtn");
const nativeShareBtn = el("nativeShareBtn");
const denoWarning = el("denoWarning");

const passcodeModal = el("passcodeModal");
const closePasscodeBtn = el("closePasscodeBtn");
const passcodeRoomLabel = el("passcodeRoomLabel");
const passcodeModalForm = el("passcodeModalForm");
const passcodeModalInput = el("passcodeModalInput");
const passcodeModalError = el("passcodeModalError");

const oauthSetupModal = el("oauthSetupModal");
const closeOauthSetupBtn = el("closeOauthSetupBtn");
const oauthCallbackPreview = el("oauthCallbackPreview");

const mobileNav = el("mobileNav");
const mobileSearchNav = el("mobileSearchNav");
const mobileQueueNav = el("mobileQueueNav");

// ------------------------------------------------------------------
// Application State
// ------------------------------------------------------------------

let currentUser = null;
let authConfig = {
  google_enabled: false,
  youtube_scope_enabled: true,
  callback_url: apiUrl("/api/auth/google/callback"),
  base_url: location.origin,
  is_private_ip: false,
};

let activeRoomCode = null;
let activeRoomJoinTicket = "";
let roomState = null;
let myIsLeader = false;
let myIsOwner = false;

let lobbyTab = "public";
let lobbyRoomsData = { public_rooms: [], my_rooms: [] };

let currentFilter = "songs";
let currentSearchTracks = [];
let currentSearchCollections = [];
let currentLibraryPlaylists = [];
let currentCollectionTracks = [];
let isViewingCollection = false;
let isSeeking = false;
let lastRenderedSpeakerSignature = "";
let deferredInstallPrompt = null;
let lastLyricsVideoId = "";

// ------------------------------------------------------------------
// Dropdown / Popover Choreography & Page Navigation
// ------------------------------------------------------------------

const POPOVER_PAIRS = [
  [layoutMenuBtn, layoutDropdownMenu],
  [el("navAccountChipBtn"), accountDropdownMenu],
  [speakerToolsMenuBtn, speakerToolsDropdown],
  [reactMenuBtn, reactPopover],
  [stageOptionsBtn, stageOptionsMenu],
];

function closeAllPopovers(exceptMenu = null) {
  POPOVER_PAIRS.forEach(([btn, menu]) => {
    if (!menu || menu === exceptMenu) return;
    menu.classList.add("hidden");
    btn?.setAttribute("aria-expanded", "false");
  });
  accountDropdownMenu?.classList.add("hidden");
}

function togglePopover(triggerBtn, menuEl) {
  if (!menuEl) return;
  const willOpen = menuEl.classList.contains("hidden");
  closeAllPopovers(willOpen ? menuEl : null);
  menuEl.classList.toggle("hidden", !willOpen);
  triggerBtn?.setAttribute("aria-expanded", willOpen ? "true" : "false");
}

layoutMenuBtn?.addEventListener("click", (e) => {
  e.stopPropagation();
  togglePopover(layoutMenuBtn, layoutDropdownMenu);
});

speakerToolsMenuBtn?.addEventListener("click", (e) => {
  e.stopPropagation();
  togglePopover(speakerToolsMenuBtn, speakerToolsDropdown);
});

reactMenuBtn?.addEventListener("click", (e) => {
  e.stopPropagation();
  togglePopover(reactMenuBtn, reactPopover);
});

stageOptionsBtn?.addEventListener("click", (e) => {
  e.stopPropagation();
  togglePopover(stageOptionsBtn, stageOptionsMenu);
});

document.addEventListener("click", (e) => {
  if (!e.target.closest(".dropdown-wrap")) {
    closeAllPopovers();
  }
});

globalThis.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeAllPopovers();
    qrModal.classList.add("hidden");
    passcodeModal.classList.add("hidden");
    oauthSetupModal.classList.add("hidden");
  }
});

function setActivePage(pageName) {
  const valid = ["stage", "search", "queue", "speaker"];
  if (!valid.includes(pageName)) pageName = "stage";
  activeRoomPage = pageName;

  document.body.classList.remove("page-stage", "page-search", "page-queue", "page-speaker");
  document.body.classList.add(`page-${pageName}`);

  desktopPageNav?.querySelectorAll(".page-pill").forEach((b) => {
    b.classList.toggle("active", b.dataset.page === pageName);
  });
  mobileNav?.querySelectorAll(".mobile-nav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.target === pageName);
  });

  document.querySelectorAll(".workspace > .panel").forEach((p) => {
    p.classList.toggle("mobile-active", p.dataset.tab === pageName);
  });
}

desktopPageNav?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-page]");
  if (!btn) return;
  setActivePage(btn.dataset.page || "stage");
});

mobileNav?.addEventListener("click", (e) => {
  const btn = e.target.closest(".mobile-nav-btn");
  if (!btn) return;
  setActivePage(btn.dataset.target || "stage");
});

const LAYOUT_NAMES = {
  minimal: "Focus",
  studio: "Studio",
  party: "Stage",
};

function applyUiMode(mode) {
  if (!["minimal", "studio", "party"].includes(mode)) mode = "minimal";
  currentUiMode = mode;
  localStorage.setItem("soundmesh_ui_mode", mode);

  document.body.classList.remove("ui-minimal", "ui-studio", "ui-party");
  document.body.classList.add(`ui-${mode}`);
  document.body.classList.toggle("theme-light", mode === "minimal" && currentMinimalTheme === "light");

  uiModeSwitcher.querySelectorAll(".mode-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.uiMode === mode);
  });
  if (activeLayoutLabel) activeLayoutLabel.textContent = LAYOUT_NAMES[mode] || "Focus";
  themeToggleBtn.classList.toggle("hidden", mode !== "minimal");
  if (themeToggleLabel) {
    themeToggleLabel.textContent =
      currentMinimalTheme === "light" ? "Switch to Obsidian Dark" : "Switch to Ceramic Light";
  }

  // In Split Studio, left pane shouldn't be stuck on "stage"
  if (mode === "studio" && activeRoomPage === "stage") {
    setActivePage("search");
  }
  closeAllPopovers();
}

uiModeSwitcher.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-ui-mode]");
  if (!btn) return;
  applyUiMode(btn.dataset.uiMode);
});

themeToggleBtn.addEventListener("click", () => {
  currentMinimalTheme = currentMinimalTheme === "light" ? "dark" : "light";
  localStorage.setItem("soundmesh_minimal_theme", currentMinimalTheme);
  applyUiMode(currentUiMode);
});

// PWA Registration & Install Prompt
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

globalThis.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredInstallPrompt = e;
  installPwaBtn.classList.remove("hidden");
});

installPwaBtn.addEventListener("click", async () => {
  if (!deferredInstallPrompt) return;
  deferredInstallPrompt.prompt();
  await deferredInstallPrompt.userChoice;
  deferredInstallPrompt = null;
  installPwaBtn.classList.add("hidden");
  closeAllPopovers();
});

nowArtwork.addEventListener("load", () => {
  const track = roomState?.playback?.track;
  if (track) {
    extractPaletteFromImage(nowArtwork, `${track.title}-${track.artist}`);
  }
});

// ------------------------------------------------------------------
// Sync & Audio Engine
// ------------------------------------------------------------------

let wsClient = null;

const clockSync = new ClockSync(
  (ping) => {
    if (wsClient) wsClient.send(ping);
  },
  (info) => {
    headerRttText.textContent = `${Math.round(info.rttMs)} ms`;
    localRttStat.textContent = `${Math.round(info.rttMs)} ms`;
  }
);

function flashSpeakerCard(targetId) {
  const card = document.querySelector(`.speaker-card[data-speaker-id="${CSS.escape(targetId)}"]`);
  if (card) {
    card.classList.remove("ping-flash");
    void card.offsetWidth;
    card.classList.add("ping-flash");
    setTimeout(() => card.classList.remove("ping-flash"), 1600);
  }
}

const audioEngine = new SyncAudioEngine(clockSync, {
  onReady: (generation) => {
    wsClient?.send({ type: "track_ready", generation });
  },
  onTelemetry: (tel) => {
    updateLocalTelemetryUI(tel);
    wsClient?.send({ type: "telemetry", ...tel });
  },
  onIdentifyVisual: () => {
    flashSpeakerCard(mySpeakerId);
  },
  onMediaAction: (action) => {
    if (!canControlRoom() || !wsClient) return;
    if (action === "play") wsClient.send({ type: "resume" });
    else if (action === "pause") wsClient.send({ type: "pause" });
    else if (action === "next") wsClient.send({ type: "next" });
    else if (action === "prev") wsClient.send({ type: "prev" });
  },
});

audioEngine.setRole(savedRole);
audioEngine.setOffsetMs(savedOffsetMs);

wsClient = new WSClient({
  clockSync,
  onConnectionChange: (connected) => {
    connectionBanner.classList.toggle("hidden", connected || !activeRoomCode);
    headerSyncDot.classList.toggle("offline", !connected);
    if (connected && audioEngine.unlocked) {
      wsClient.send({
        type: "update_speaker",
        target_id: mySpeakerId,
        fields: { audio_unlocked: true },
      });
    }
  },
  onFatalClose: (code) => {
    if (code === 4403) openPasscodeModal(activeRoomCode || "");
    else if (code === 4429) {
      showToast("Too many PIN attempts. Please wait a minute.");
      navigateToLobby();
    } else if (code === 4404) {
      showToast("Room not found or closed.");
      navigateToLobby();
    }
  },
  onMessage: (msg) => handleServerMessage(msg),
});

// ------------------------------------------------------------------
// Auth & Account Popover
// ------------------------------------------------------------------

async function refreshAuth() {
  await exchangePendingOauthTicket();
  const resp = await apiFetch("/api/auth/me");
  const data = await resp.json();
  currentUser = data.user || null;
  authConfig = data.auth_config || authConfig;
  renderAccountUI();
}

function startGoogleLogin(includeYt = true) {
  if (!authConfig.google_enabled) {
    oauthCallbackPreview.textContent = authConfig.callback_url;
    oauthSetupModal.classList.remove("hidden");
    return;
  }
  const nextPath = activeRoomCode ? `/room/${activeRoomCode}` : "/";
  const url = apiUrl(
    `/api/auth/google/login?next=${encodeURIComponent(nextPath)}&include_youtube=${includeYt ? "1" : "0"}`
  );
  location.href = url;
}

function renderAccountUI() {
  lobbyLanNotice.classList.toggle("hidden", !authConfig.is_private_ip);

  if (!currentUser) {
    accountModeBadge.textContent = "Signed Out";
    lobbySignedOut.classList.remove("hidden");
    lobbySignedIn.classList.add("hidden");
    navAccountArea.innerHTML = `<button id="navGoogleSignInBtn" class="btn btn-outline btn-xs">Sign In</button>`;
    el("navGoogleSignInBtn")?.addEventListener("click", () => startGoogleLogin(true));

    navDropName.textContent = "Not Signed In";
    navDropMeta.textContent = "Guest or Google access";
    navDropGoogleBtn.classList.remove("hidden");
    navDropYtBtn.classList.add("hidden");
    navDropLogoutBtn.classList.add("hidden");
    return;
  }

  const isGoogle = currentUser.provider === "google";
  accountModeBadge.textContent = isGoogle ? "Google Verified" : "Guest Pass";
  lobbySignedOut.classList.add("hidden");
  lobbySignedIn.classList.remove("hidden");

  profileAvatarWrap.innerHTML = avatarHtml(currentUser.name, currentUser.avatar_url, "avatar-sm");
  profileNameText.textContent = currentUser.name;
  profileEmailText.textContent = isGoogle ? currentUser.email : "Guest Session";
  profileYtBadge.classList.toggle("hidden", !(isGoogle && currentUser.has_youtube_scope));
  upgradeGoogleBtn.classList.toggle("hidden", isGoogle);
  connectYtScopeBtn.classList.toggle(
    "hidden",
    !(isGoogle && !currentUser.has_youtube_scope && authConfig.youtube_scope_enabled)
  );

  navAccountArea.innerHTML = `
    <button id="navAccountChipBtn" class="account-chip" aria-haspopup="true" aria-expanded="false">
      ${avatarHtml(currentUser.name, currentUser.avatar_url, "avatar-sm")}
      <span>${escapeHtml(currentUser.name)}</span>
      <svg class="svg-i" width="12" height="12"><use href="#i-chevron"/></svg>
    </button>
  `;
  el("navAccountChipBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    togglePopover(el("navAccountChipBtn"), accountDropdownMenu);
  });

  navDropName.textContent = currentUser.name;
  navDropMeta.textContent = isGoogle ? currentUser.email : "Guest Session";
  navDropGoogleBtn.classList.toggle("hidden", isGoogle);
  navDropYtBtn.classList.toggle(
    "hidden",
    !(isGoogle && !currentUser.has_youtube_scope && authConfig.youtube_scope_enabled)
  );
  navDropLogoutBtn.classList.remove("hidden");
}

navDropGoogleBtn?.addEventListener("click", () => {
  closeAllPopovers();
  startGoogleLogin(true);
});
navDropYtBtn?.addEventListener("click", () => {
  closeAllPopovers();
  startGoogleLogin(true);
});
navDropLogoutBtn?.addEventListener("click", () => {
  closeAllPopovers();
  void logoutUser();
});

async function logoutUser() {
  await apiFetch("/api/auth/logout", { method: "POST" });
  setSessionToken("");
  currentUser = null;
  renderAccountUI();
  if (activeRoomCode) navigateToLobby();
  else refreshRoomsDirectory();
}

async function saveCloudPreset(fields) {
  if (!currentUser) return;
  try {
    await apiFetch(`/api/me/preset/${encodeURIComponent(clientId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
  } catch {
    // Non-critical preset persistence
  }
}

lobbyGoogleLoginBtn.addEventListener("click", () => startGoogleLogin(includeYoutubeScopeCheck.checked));
upgradeGoogleBtn.addEventListener("click", () => startGoogleLogin(true));
connectYtScopeBtn.addEventListener("click", () => startGoogleLogin(true));
lobbyLogoutBtn.addEventListener("click", logoutUser);

guestLoginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = guestNameInput.value.trim();
  if (!name) return;
  const resp = await apiFetch("/api/auth/guest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const data = await resp.json();
  currentUser = data.user;
  if (data.session_token) setSessionToken(data.session_token);
  localStorage.setItem("soundmesh_speaker_name", `${currentUser.name}'s ${defaultDeviceLabel(deviceType)}`);
  renderAccountUI();
  refreshRoomsDirectory();
  showToast(`Welcome, ${currentUser.name}`);
});

// ------------------------------------------------------------------
// Tabbed Lobby Hub & Multi-Room Routing
// ------------------------------------------------------------------

lobbyHubTabs?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-hub-tab]");
  if (!btn) return;
  const tab = btn.dataset.hubTab;
  lobbyHubTabs.querySelectorAll(".tab-chip").forEach((b) => b.classList.toggle("active", b === btn));
  hubTabJoin.classList.toggle("hidden", tab !== "join");
  hubTabCreate.classList.toggle("hidden", tab !== "create");
  hubTabBrowse.classList.toggle("hidden", tab !== "browse");
  if (tab === "browse") refreshRoomsDirectory();
});

async function ensureSignedInForLobbyAction() {
  if (currentUser) return true;
  const typedName = guestNameInput.value.trim() || "Music Host";
  const resp = await apiFetch("/api/auth/guest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: typedName }),
  });
  const data = await resp.json();
  currentUser = data.user;
  if (data.session_token) setSessionToken(data.session_token);
  renderAccountUI();
  return true;
}

async function refreshRoomsDirectory() {
  try {
    const resp = await apiFetch("/api/rooms");
    if (!resp.ok) return;
    lobbyRoomsData = await resp.json();
    renderRoomsDirectory();
  } catch {
    // Ignore
  }
}

function renderRoomsDirectory() {
  const list = lobbyTab === "mine" ? lobbyRoomsData.my_rooms || [] : lobbyRoomsData.public_rooms || [];
  if (list.length === 0) {
    roomsDirectoryGrid.innerHTML = `<div class="empty-state compact"><p>${
      lobbyTab === "mine"
        ? "No rooms created under this profile yet."
        : "No public rooms on-air right now."
    }</p></div>`;
    return;
  }

  roomsDirectoryGrid.innerHTML = list
    .map((r) => {
      const playingIndicator = r.now_playing_title
        ? `<span class="eq-mini" aria-hidden="true"><i></i><i></i><i></i></span>${escapeHtml(r.now_playing_title)} — ${escapeHtml(r.now_playing_artist)}`
        : "Standby · Ready";
      return `
      <div class="room-dir-card">
        <div class="room-dir-top">
          <div>
            <div class="room-dir-title">${escapeHtml(r.name)} ${r.has_passcode ? "· PIN" : ""}</div>
            <span class="mono-sm">#${escapeHtml(r.code)}</span>
          </div>
          <span class="badge-subtle">${r.speaker_count} node${r.speaker_count === 1 ? "" : "s"}</span>
        </div>
        <div class="room-dir-meta">
          <span>DJ: <strong>${escapeHtml(r.leader_name)}</strong></span>
          <span>${playingIndicator}</span>
        </div>
        <div class="room-dir-actions">
          <button class="btn btn-primary btn-xs" data-join-code="${escapeHtml(r.code)}" data-has-pass="${r.has_passcode ? "1" : "0"}">Tune In</button>
          ${
            currentUser && r.owner_user_id === currentUser.id
              ? `<button class="btn btn-ghost btn-xs" data-delete-code="${escapeHtml(r.code)}">Close</button>`
              : ""
          }
        </div>
      </div>`;
    })
    .join("");
}

lobbyRoomTabs.addEventListener("click", (e) => {
  const btn = e.target.closest(".tab-chip");
  if (!btn) return;
  lobbyTab = btn.dataset.roomTab || "public";
  lobbyRoomTabs.querySelectorAll(".tab-chip").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  renderRoomsDirectory();
});

refreshRoomsBtn.addEventListener("click", refreshRoomsDirectory);

roomsDirectoryGrid.addEventListener("click", async (e) => {
  const joinBtn = e.target.closest("[data-join-code]");
  if (joinBtn) {
    await attemptEnterRoom(joinBtn.dataset.joinCode, "", joinBtn.dataset.hasPass === "1");
    return;
  }
  const delBtn = e.target.closest("[data-delete-code]");
  if (delBtn) {
    const code = delBtn.dataset.deleteCode;
    const resp = await apiFetch(`/api/rooms/${encodeURIComponent(code)}`, { method: "DELETE" });
    if (resp.ok) {
      showToast(`Closed room #${code}`);
      refreshRoomsDirectory();
    }
  }
});

createRoomForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  await ensureSignedInForLobbyAction();
  const payload = {
    name: createRoomNameInput.value.trim(),
    is_public: createRoomVisibility.value === "public",
    passcode: createRoomPasscode.value.trim(),
  };
  const resp = await apiFetch("/api/rooms", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    showToast("Could not create room.");
    return;
  }
  const data = await resp.json();
  enterRoom(data.room.code, "", true);
});

function extractRoomCode(rawInput) {
  const s = String(rawInput || "").trim();
  if (!s) return "";
  try {
    const u = new URL(s);
    const m = u.pathname.match(/\/room\/([A-Za-z0-9_-]+)/i);
    if (m) return m[1].toUpperCase();
    const q = u.searchParams.get("room");
    if (q) return q.trim().toUpperCase();
  } catch {
    // Plain room code or path
  }
  const pathMatch = s.match(/\/room\/([A-Za-z0-9_-]+)/i);
  if (pathMatch) return pathMatch[1].toUpperCase();
  return s.replace(/^#/, "").toUpperCase();
}

joinCodeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = extractRoomCode(joinCodeInput.value);
  if (!code) return;
  await attemptEnterRoom(code, joinPasscodeInput.value.trim(), false);
});

async function attemptEnterRoom(code, passcode = "", forcePrompt = false) {
  const cleanCode = code.trim().toUpperCase();
  const resp = await apiFetch(`/api/rooms/${encodeURIComponent(cleanCode)}`);
  if (!resp.ok) {
    showToast(`Room #${cleanCode} not found.`);
    return;
  }
  const roomSummary = (await resp.json()).room;
  const isOwner = currentUser && roomSummary.owner_user_id === currentUser.id;
  let joinTicket = "";
  if (roomSummary.has_passcode && !isOwner) {
    if (passcode) {
      const vResp = await apiFetch(`/api/rooms/${encodeURIComponent(cleanCode)}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode }),
      });
      if (vResp.status === 429) {
        showToast("Too many PIN attempts. Please wait a minute.");
        return;
      }
      const vData = await vResp.json();
      if (!vData.ok) {
        openPasscodeModal(cleanCode);
        return;
      }
      joinTicket = vData.join_ticket || "";
    } else if (forcePrompt || !passcode) {
      openPasscodeModal(cleanCode);
      return;
    }
  }
  enterRoom(cleanCode, joinTicket, true);
}

function openPasscodeModal(code) {
  passcodeRoomLabel.textContent = `#${code}`;
  passcodeModalInput.value = "";
  passcodeModalError.classList.add("hidden");
  passcodeModal.dataset.targetCode = code;
  passcodeModal.classList.remove("hidden");
  passcodeModalInput.focus();
}

closePasscodeBtn.addEventListener("click", () => passcodeModal.classList.add("hidden"));

passcodeModalForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = passcodeModal.dataset.targetCode;
  const pass = passcodeModalInput.value.trim();
  const vResp = await apiFetch(`/api/rooms/${encodeURIComponent(code)}/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode: pass }),
  });
  if (vResp.status === 429) {
    passcodeModalError.textContent = "Too many attempts. Wait 60s.";
    passcodeModalError.classList.remove("hidden");
    return;
  }
  const vData = await vResp.json();
  if (!vData.ok) {
    passcodeModalError.textContent = "Incorrect room PIN.";
    passcodeModalError.classList.remove("hidden");
    return;
  }
  passcodeModal.classList.add("hidden");
  enterRoom(code, vData.join_ticket || "", true);
});

function enterRoom(roomCode, joinTicket = "", pushHistory = true) {
  const cleanCode = roomCode.trim().toUpperCase();
  activeRoomCode = cleanCode;
  activeRoomJoinTicket = joinTicket;
  mySpeakerId = clientId;
  roomState = null;
  lastRenderedSpeakerSignature = "";

  if (pushHistory && location.pathname !== `/room/${cleanCode}`) {
    history.pushState({ roomCode: cleanCode }, "", `/room/${cleanCode}`);
  }

  lobbyView.classList.add("hidden");
  roomView.classList.remove("hidden");
  roomHeaderCluster.classList.remove("hidden");
  roomTopControls.classList.remove("hidden");
  layoutMenuWrap.classList.remove("hidden");
  document.body.classList.add("in-room");
  applyUiMode(currentUiMode);
  setActivePage(currentUiMode === "studio" ? "search" : "stage");

  const suggestedName =
    localStorage.getItem("soundmesh_speaker_name") ||
    (currentUser ? `${currentUser.name}'s ${defaultDeviceLabel(deviceType)}` : `${defaultDeviceLabel(deviceType)} Node`);
  unlockNameInput.value = suggestedName;
  unlockRoleSelect.value = savedRole;
  unlockRoomBadge.textContent = `ROOM #${cleanCode}`;
  if (!audioEngine.unlocked) unlockOverlay.classList.remove("hidden");

  const wsParams = {
    client_id: clientId,
    rk: reconnectKey,
    name: suggestedName,
    device_type: deviceType,
    role: savedRole,
    offset_ms: String(savedOffsetMs),
  };
  if (activeRoomJoinTicket) wsParams.join_ticket = activeRoomJoinTicket;
  wsClient.connect(cleanCode, wsParams);
}

function navigateToLobby(pushHistory = true) {
  if (activeRoomCode) {
    wsClient.send({ type: "leave_room" });
  }
  wsClient.close();
  activeRoomCode = null;
  activeRoomJoinTicket = "";
  roomState = null;
  connectionBanner.classList.add("hidden");
  document.body.classList.remove("in-room", "is-playing");
  closeAllPopovers();

  roomView.classList.add("hidden");
  roomHeaderCluster.classList.add("hidden");
  roomTopControls.classList.add("hidden");
  layoutMenuWrap.classList.add("hidden");
  lobbyView.classList.remove("hidden");

  if (pushHistory && location.pathname !== "/") {
    history.pushState({}, "", "/");
  }
  refreshRoomsDirectory();
}

brandHomeBtn.addEventListener("click", () => {
  if (activeRoomCode) navigateToLobby();
});
leaveRoomBtn.addEventListener("click", () => navigateToLobby());
globalThis.addEventListener("popstate", () => handleInitialRoute(false));

function leaveRoomOnUnload() {
  if (!activeRoomCode) return;
  wsClient.send({ type: "leave_room" });
  wsClient.close();
}
globalThis.addEventListener("pagehide", leaveRoomOnUnload);
globalThis.addEventListener("beforeunload", leaveRoomOnUnload);

// ------------------------------------------------------------------
// Audio Unlock
// ------------------------------------------------------------------

function completeUnlock(controllerOnly = false) {
  const chosenName = unlockNameInput.value.trim() || `${defaultDeviceLabel(deviceType)} Node`;
  const chosenRole = unlockRoleSelect.value || "stereo";
  localStorage.setItem("soundmesh_speaker_name", chosenName);
  localStorage.setItem("soundmesh_role", chosenRole);

  audioEngine.unlock();
  audioEngine.setRole(chosenRole);
  if (controllerOnly) audioEngine.setVolumes({ speakerMuted: true });
  unlockOverlay.classList.add("hidden");

  wsClient.send({
    type: "update_speaker",
    target_id: mySpeakerId,
    fields: { name: chosenName, role: chosenRole, muted: controllerOnly, audio_unlocked: true },
  });
}

unlockBtn.addEventListener("click", () => completeUnlock(false));
controllerOnlyBtn.addEventListener("click", () => completeUnlock(true));

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && activeRoomCode) {
    clockSync.triggerQuickResync();
    audioEngine.reacquireWakeLockIfNeeded();
  }
});

// ------------------------------------------------------------------
// WebSocket Messages & Floating Reactions
// ------------------------------------------------------------------

function spawnFloatingReaction(emoji, userName) {
  const bubble = document.createElement("div");
  bubble.className = "floating-emoji";
  bubble.style.left = `${14 + Math.random() * 72}%`;
  bubble.title = userName || "";
  bubble.textContent = emoji;
  reactionFloatLayer.appendChild(bubble);
  setTimeout(() => bubble.remove(), 2450);
}

function pulseSonarVisual() {
  if (!sonarCard) return;
  sonarCard.classList.add("sonar-listening");
  setTimeout(() => sonarCard.classList.remove("sonar-listening"), 1800);
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case "welcome":
      if (msg.speaker_id) mySpeakerId = msg.speaker_id;
      myIsLeader = Boolean(msg.is_leader);
      myIsOwner = Boolean(msg.is_owner);
      applyFullRoomState(msg.state);
      break;
    case "room_state":
      applyFullRoomState(msg.state);
      break;
    case "playback_update":
      if (roomState) {
        roomState.playback = msg.playback;
        audioEngine.applyPlaybackState(msg.playback, Boolean(msg.force_resync));
        renderPlaybackUI();
        scheduleGaplessPrefetch();
      }
      break;
    case "speakers_update":
      if (roomState) {
        roomState.speakers = msg.speakers || [];
        syncSelfFromRoom();
        renderSpeakersUI();
        renderSpatialMap();
        renderLeaderBanner();
      }
      break;
    case "identify_speaker":
      audioEngine.playIdentifyChime();
      break;
    case "acoustic_chirp":
      if (msg.emitter_id) flashSpeakerCard(msg.emitter_id);
      pulseSonarVisual();
      if (msg.emitter_id === mySpeakerId && msg.start_server_ms) {
        audioEngine.emitTimedChirp(msg.start_server_ms);
        showToast("Emitting 2–7 kHz acoustic sonar sweep…");
      }
      break;
    case "reaction":
      spawnFloatingReaction(msg.emoji || "🔥", msg.user_name);
      break;
    case "track_skipped":
      showToast(`Skipped "${msg.title}": stream unavailable`);
      break;
    case "leader_changed":
      showToast(
        msg.reason === "auto_handoff"
          ? `${msg.leader_name} took over as DJ`
          : `${msg.leader_name} is now DJ`
      );
      break;
    case "leader_request":
      if (msg.request) showToast(`${msg.request.user_name} asked for DJ control`);
      break;
    case "leader_request_declined":
      showToast(`DJ request declined by ${msg.by || "Leader"}`);
      break;
    case "room_closed":
      showToast("Room closed by host.");
      navigateToLobby();
      break;
    case "kicked":
      showToast(`Removed from room${msg.by ? ` by ${msg.by}` : ""}.`);
      navigateToLobby();
      break;
    default:
      break;
  }
}

function scheduleGaplessPrefetch() {
  if (!roomState || !audioEngine.unlocked) return;
  if (roomState.playback?.use_yt_iframe) return;
  const q = roomState.queue || [];
  const curIdx = roomState.playback?.queue_index ?? -1;
  if (roomState.playback?.status === "playing" && curIdx >= 0 && curIdx + 1 < q.length) {
    const nextTrack = q[curIdx + 1];
    if (nextTrack?.cached && nextTrack.duration_sec <= 720) {
      audioEngine.prefetchNext(nextTrack.video_id);
    }
  }
}

function applyFullRoomState(state) {
  if (!state) return;
  roomState = state;
  syncSelfFromRoom();

  audioEngine.setVolumes({
    masterVolume: state.master_volume,
    masterMuted: state.master_muted,
  });
  audioEngine.setCalibrationState(state.calibration);
  audioEngine.applyPlaybackState(state.playback, false);
  scheduleGaplessPrefetch();

  renderTopBar();
  renderLeaderBanner();
  renderLeaderRequests();
  renderPlaybackUI();
  renderQueueUI();
  renderSpeakersUI();
  renderSpatialMap();
  renderPermissionsUI();
}

function syncSelfFromRoom() {
  if (!roomState || !Array.isArray(roomState.speakers)) return;
  const me = roomState.speakers.find((s) => s.id === mySpeakerId);
  if (!me) return;
  myIsLeader = Boolean(me.is_leader);
  myIsOwner = Boolean(me.is_owner);
  audioEngine.setRole(me.role);
  audioEngine.setSpatial({
    pos_x: me.pos_x,
    pos_y: me.pos_y,
    spatial_enabled: me.spatial_enabled,
  });
  audioEngine.setEq({ eq_preset: me.eq_preset, eq_bands: me.eq_bands });
  audioEngine.setVolumes({ speakerVolume: me.volume, speakerMuted: me.muted });
  if (Math.abs(audioEngine.offsetMs - me.offset_ms) > 0.05) {
    audioEngine.setOffsetMs(me.offset_ms);
    localStorage.setItem("soundmesh_offset_ms", String(me.offset_ms));
  }
  localOffsetStat.textContent = `${Math.round(me.offset_ms)} ms`;
  muteMeControllerToggle.checked = Boolean(me.muted);
  spatialEnableToggle.checked = Boolean(me.spatial_enabled);
  syncEqSlidersUI(me.eq_preset, me.eq_bands);
}

function canControlRoom() {
  if (!roomState) return myIsLeader;
  return myIsLeader || Boolean(roomState.allow_guest_control);
}

function canQueueTracks() {
  if (!roomState) return myIsLeader;
  return canControlRoom() || Boolean(roomState.allow_collab_queue);
}

function canAssignLeader() {
  return myIsLeader || myIsOwner;
}

// ------------------------------------------------------------------
// Rendering
// ------------------------------------------------------------------

function renderTopBar() {
  if (!roomState) return;
  headerRoomName.textContent = roomState.room_name || "SoundMesh";
  headerRoomCode.textContent = `#${roomState.room_code || "STUDIO"}`;
  const count = roomState.speakers.length;
  headerSpeakerCount.textContent = `${count} Node${count === 1 ? "" : "s"}`;

  const calActive = Boolean(roomState.calibration?.active);
  calibrationBanner.classList.toggle("hidden", !calActive);
  calibrateToggleBtn.classList.toggle("active", calActive);

  guestControlToggle.checked = Boolean(roomState.allow_guest_control);
  guestControlWrap.classList.toggle("hidden", !canAssignLeader());

  const volPct = Math.round((roomState.master_volume ?? 1) * 100);
  masterVolumeSlider.value = String(volPct);
  masterVolumeLabel.textContent = `${volPct}%`;
  masterMuteBtn.innerHTML = svgIcon(roomState.master_muted ? "mute" : "vol", 15);
}

function renderLeaderBanner() {
  if (!roomState) return;
  const leader = (roomState.speakers || []).find((s) => s.id === roomState.leader_speaker_id);
  const leaderLabel = leader ? leader.user_name || leader.name : "Awaiting DJ…";
  leaderBannerName.textContent = leaderLabel;
  leaderBannerDevice.textContent = leader ? `· ${leader.name}` : "";

  const alreadyRequested = (roomState.pending_leader_requests || []).some(
    (r) => r.speaker_id === mySpeakerId
  );

  requestLeaderBtn.classList.toggle("hidden", myIsLeader || myIsOwner);
  requestLeaderBtn.disabled = alreadyRequested;
  requestLeaderBtn.textContent = alreadyRequested ? "Pending…" : "Ask to Lead";
  takeLeaderBtn.classList.toggle("hidden", !(myIsOwner && !myIsLeader));

  leaderOwnerControls.classList.toggle("hidden", !canAssignLeader());
  collabQueueToggle.checked = Boolean(roomState.allow_collab_queue);
}

function renderLeaderRequests() {
  if (!roomState) return;
  const reqs = roomState.pending_leader_requests || [];
  if (!canAssignLeader() || reqs.length === 0) {
    leaderRequestsDrawer.classList.add("hidden");
    leaderRequestsDrawer.innerHTML = "";
    return;
  }

  leaderRequestsDrawer.classList.remove("hidden");
  leaderRequestsDrawer.innerHTML = reqs
    .map(
      (r) => `
      <div class="leader-req-row">
        <span><strong>${escapeHtml(r.user_name)}</strong> (${escapeHtml(r.name)}) wants DJ control</span>
        <div class="leader-req-actions">
          <button class="btn btn-primary btn-xs" data-approve-req="${escapeHtml(r.speaker_id)}">Pass DJ</button>
          <button class="btn btn-ghost btn-xs" data-decline-req="${escapeHtml(r.speaker_id)}">Dismiss</button>
        </div>
      </div>`
    )
    .join("");
}

function renderPermissionsUI() {
  const allowed = canControlRoom();
  const canSearchOrQueue = canQueueTracks();
  [
    playPauseBtn, prevBtn, nextBtn, shuffleBtn, repeatBtn, seekSlider,
    clearQueueBtn, autoStereoBtn, calibrateToggleBtn, masterVolumeSlider,
    masterMuteBtn, resyncAllBtn, loadRadioBtn, queueAutoRadioBtn,
  ].forEach((c) => {
    if (c) c.disabled = !allowed;
  });
  if (searchSubmitBtn) searchSubmitBtn.disabled = !canSearchOrQueue;
  if (desktopSearchTab) desktopSearchTab.style.display = canSearchOrQueue ? "inline-flex" : "none";
  if (mobileSearchNav) mobileSearchNav.style.display = canSearchOrQueue ? "flex" : "none";
  if (mobileQueueNav) mobileQueueNav.style.display = "flex";
}

function renderPlaybackUI() {
  const pb = roomState?.playback;
  if (!pb) return;

  const isPlaying = pb.status === "playing";
  document.body.classList.toggle("is-playing", isPlaying);

  const track = pb.track;
  if (track) {
    nowTitle.textContent = track.title;
    nowArtist.textContent = track.album ? `${track.artist} · ${track.album}` : track.artist;
    const safeThumb = safeImageUrl(track.thumbnail);
    const paletteKey = `${track.video_id}:${safeThumb}`;
    if (safeThumb) {
      if (nowArtwork.getAttribute("src") !== safeThumb) {
        nowArtwork.src = safeThumb;
      }
      nowArtwork.classList.remove("hidden");
      nowArtworkPlaceholder.classList.add("hidden");
      stageBackdrop.style.backgroundImage = `url("${safeThumb}")`;
    } else {
      nowArtwork.classList.add("hidden");
      nowArtworkPlaceholder.classList.remove("hidden");
      stageBackdrop.style.backgroundImage = "none";
      if (lastPaletteKey !== paletteKey) {
        applyFallbackPaletteFromSeed(track.title);
      }
    }
    lastPaletteKey = paletteKey;

    if (!lyricsPanel.classList.contains("hidden") && lastLyricsVideoId !== track.video_id) {
      fetchAndRenderLyrics(track.video_id);
    }
  } else {
    nowTitle.textContent = "Ready to Spin";
    nowArtist.textContent = myIsLeader
      ? "Open Music & Library to choose a song"
      : "Phase-locked to DJ Leader — waiting for track";
    nowArtwork.classList.add("hidden");
    nowArtworkPlaceholder.classList.remove("hidden");
    stageBackdrop.style.backgroundImage = "none";
  }

  loadingOverlay.classList.toggle("hidden", pb.status !== "loading");
  loadingPhaseText.textContent =
    pb.loading_phase === "buffering" ? "Syncing speakers…" : "Loading stream…";

  if (pb.loading_error) {
    nowError.textContent = pb.loading_error;
    nowError.classList.remove("hidden");
  } else {
    nowError.classList.add("hidden");
  }

  playPauseBtn.innerHTML = svgIcon(isPlaying ? "pause" : "play", 24);
  shuffleBtn.classList.toggle("active", Boolean(roomState.shuffle));
  repeatBtn.classList.toggle("active", roomState.repeat !== "off");
  repeatOneBadge?.classList.toggle("hidden", roomState.repeat !== "one");

  const dur = pb.audio_duration_sec || track?.duration_sec || 0;
  timeDuration.textContent = formatTime(dur);
  seekSlider.max = String(Math.max(1, Math.floor(dur)));
  seekSlider.disabled = !pb.audio_ready || !canControlRoom();
}

function renderQueueUI() {
  if (!roomState) return;
  const q = roomState.queue || [];
  const curIdx = roomState.playback?.queue_index ?? -1;
  queueCountBadge.textContent = String(q.length);
  if (queueTabBadge) queueTabBadge.textContent = String(q.length);

  if (q.length === 0) {
    queueList.innerHTML = `<div class="empty-state compact"><p>Queue is empty — pick a song in Music &amp; Library or tap <strong>Auto Radio</strong>.</p></div>`;
    return;
  }

  const allowed = canControlRoom();
  const myKey = currentUser?.id || mySpeakerId;

  queueList.innerHTML = q
    .map((t, idx) => {
      const votes = Array.isArray(t.votes) ? t.votes : [];
      const voted = votes.includes(myKey);
      const isUpcoming = idx > curIdx;
      const activeBars =
        idx === curIdx
          ? `<span class="eq-mini" aria-hidden="true"><i></i><i></i><i></i></span>`
          : "";
      return `
      <div class="track-row ${idx === curIdx ? "active-track" : ""}" data-q-idx="${idx}">
        <img class="track-thumb" src="${escapeHtml(safeImageUrl(t.thumbnail))}" alt="" loading="lazy" />
        <div class="track-info" data-action="play-idx" data-idx="${idx}" style="cursor:pointer">
          <div class="track-title">${activeBars}${escapeHtml(t.title)}</div>
          <div class="track-sub">${escapeHtml(t.artist)} · ${formatTime(t.duration_sec)} ${
            t.cached ? '<span class="cached-tag">READY</span>' : ""
          }</div>
        </div>
        <div class="track-actions">
          ${
            isUpcoming
              ? `<button class="upvote-btn ${voted ? "voted" : ""}" data-action="upvote" data-idx="${idx}" title="Upvote">▲ ${votes.length}</button>`
              : ""
          }
          ${
            allowed
              ? `<button class="icon-btn" data-action="up" data-idx="${idx}" title="Up">↑</button>
                 <button class="icon-btn" data-action="down" data-idx="${idx}" title="Down">↓</button>
                 <button class="icon-btn" data-action="remove" data-idx="${idx}" title="Remove">✕</button>`
              : ""
          }
        </div>
      </div>`;
    })
    .join("");
}

function formatSyncStatus(status) {
  switch (status) {
    case "locked": return "Locked";
    case "syncing": return "Aligning";
    case "buffering": return "Buffering";
    case "idle": return "Armed";
    default: return "Standby";
  }
}

function updateLocalTelemetryUI(tel) {
  localRttStat.textContent = `${Math.round(tel.rtt_ms || 0)} ms`;
  const d = tel.drift_ms || 0;
  localDriftStat.textContent = `${d >= 0 ? "+" : ""}${d.toFixed(1)} ms`;
  localSyncBadge.className = `sync-badge ${tel.sync_status || "idle"}`;
  localSyncBadge.textContent = formatSyncStatus(tel.sync_status);
}

function renderSpeakersUI() {
  if (!roomState) return;
  const speakers = roomState.speakers || [];
  const sig =
    speakers.map((s) => `${s.id}:${s.is_leader}:${s.is_owner}:${s.muted}`).join("|") +
    `:${canControlRoom()}:${canAssignLeader()}`;
  const activeTag = document.activeElement?.tagName;
  const isEditing =
    activeTag === "INPUT" || activeTag === "SELECT"
      ? document.activeElement.closest(".speaker-card")
      : null;

  if (sig !== lastRenderedSpeakerSignature && !isEditing) {
    lastRenderedSpeakerSignature = sig;
    speakerList.innerHTML = speakers.map((s) => buildSpeakerCardHtml(s)).join("");
  } else {
    for (const s of speakers) {
      const card = speakerList.querySelector(`.speaker-card[data-speaker-id="${CSS.escape(s.id)}"]`);
      if (!card) {
        lastRenderedSpeakerSignature = "";
        speakerList.innerHTML = speakers.map((sp) => buildSpeakerCardHtml(sp)).join("");
        break;
      }
      const badge = card.querySelector("[data-role='sync-badge']");
      if (badge) {
        badge.className = `sync-badge ${s.sync_status}`;
        badge.textContent = formatSyncStatus(s.sync_status);
      }
      const tel = card.querySelector("[data-role='telemetry']");
      if (tel) {
        tel.textContent = `RTT ${Math.round(s.rtt_ms)}ms · Δ ${s.drift_ms >= 0 ? "+" : ""}${s.drift_ms.toFixed(1)}ms`;
      }
      const offLabel = card.querySelector("[data-role='offset-label']");
      if (offLabel) offLabel.textContent = `${Math.round(s.offset_ms)}ms`;
      const muteBtn = card.querySelector("[data-action='toggle-mute']");
      if (muteBtn) muteBtn.innerHTML = svgIcon(s.muted ? "mute" : "vol", 15);
    }
  }
  headerSpeakerCount.textContent = `${speakers.length} Node${speakers.length === 1 ? "" : "s"}`;
}

function buildSpeakerCardHtml(s) {
  const isSelf = s.id === mySpeakerId;
  const editable = isSelf || canControlRoom();
  const disabledAttr = editable ? "" : "disabled";
  const showMakeLeader = canAssignLeader() && !s.is_leader;
  const isExpanded = expandedSpeakerIds.has(s.id);

  return `
    <div class="speaker-card ${isSelf ? "is-self" : ""} ${s.is_leader ? "is-leader-card" : ""}" data-speaker-id="${escapeHtml(s.id)}">
      <div class="speaker-summary-row">
        <div class="speaker-id-group">
          ${avatarHtml(s.user_name || s.name, s.user_avatar, "avatar-sm")}
          <div class="speaker-name-wrap">
            <input class="speaker-name-input" type="text" data-field="name" value="${escapeHtml(s.name)}" ${disabledAttr} />
            <span class="speaker-user-sub">${escapeHtml(s.user_name || "Guest")}${isSelf ? " · You" : ""}</span>
          </div>
        </div>
        <div class="speaker-row-actions">
          ${s.is_leader ? '<span class="role-pill leader">DJ</span>' : ""}
          <span class="sync-badge ${s.sync_status}" data-role="sync-badge">${formatSyncStatus(s.sync_status)}</span>
          <button class="icon-btn" data-action="toggle-mute" ${disabledAttr} title="Quick Mute">${svgIcon(s.muted ? "mute" : "vol", 15)}</button>
          <button class="btn btn-outline btn-xs" data-action="toggle-tune" aria-expanded="${isExpanded}">
            ${svgIcon("sliders", 12)}
            <span>${isExpanded ? "Done" : "Tune ▾"}</span>
          </button>
        </div>
      </div>

      <div class="speaker-tune-drawer ${isExpanded ? "" : "hidden"}">
        <div class="speaker-controls">
          <select data-field="role" ${disabledAttr}>
            <option value="stereo" ${s.role === "stereo" ? "selected" : ""}>Stereo</option>
            <option value="left" ${s.role === "left" ? "selected" : ""}>Left (L)</option>
            <option value="right" ${s.role === "right" ? "selected" : ""}>Right (R)</option>
            <option value="mono" ${s.role === "mono" ? "selected" : ""}>Mono</option>
            <option value="subwoofer" ${s.role === "subwoofer" ? "selected" : ""}>Sub</option>
          </select>
          <input type="range" min="0" max="100" value="${Math.round(s.volume * 100)}" data-field="volume" ${disabledAttr} aria-label="Speaker volume" />
        </div>

        <div class="speaker-offset-row">
          <span>Trim:</span>
          <button class="nudge-btn" data-action="nudge" data-delta="-5" ${disabledAttr}>−5</button>
          <input type="range" min="-500" max="500" value="${Math.round(s.offset_ms)}" data-field="offset_ms" ${disabledAttr} aria-label="Sync offset ms" />
          <button class="nudge-btn" data-action="nudge" data-delta="5" ${disabledAttr}>+5</button>
          <span class="mono-sm" data-role="offset-label">${Math.round(s.offset_ms)}ms</span>
        </div>

        <div class="speaker-telemetry">
          <span data-role="telemetry">RTT ${Math.round(s.rtt_ms)}ms · Δ ${s.drift_ms >= 0 ? "+" : ""}${s.drift_ms.toFixed(1)}ms</span>
          <div class="speaker-actions-inline">
            ${showMakeLeader ? `<button class="btn btn-accent btn-xs" data-action="make-leader">Pass DJ</button>` : ""}
            ${!isSelf && (canAssignLeader() || canControlRoom()) ? `<button class="btn btn-ghost btn-xs" data-action="remove-speaker">Remove</button>` : ""}
            <button class="btn btn-ghost btn-xs" data-action="ping">Chime</button>
          </div>
        </div>
      </div>
    </div>
  `;
}

// ------------------------------------------------------------------
// DSP Tabs, Live EQ Curve, 1-Tap Sonar Sync & 2D Stage
// ------------------------------------------------------------------

speakerSubTabs.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-sp-tab]");
  if (!btn) return;
  const tab = btn.dataset.spTab;
  speakerSubTabs.querySelectorAll(".tab-chip").forEach((b) => b.classList.toggle("active", b === btn));
  spTabList.classList.toggle("hidden", tab !== "list");
  spTabMap.classList.toggle("hidden", tab !== "map");
  spTabEq.classList.toggle("hidden", tab !== "eq");
  spTabSync.classList.toggle("hidden", tab !== "sync");
});

function updateEqCurveSvg(bands) {
  if (!eqCurvePath || !eqCurveArea || !Array.isArray(bands) || bands.length !== 5) return;
  const xs = [24, 92, 160, 228, 296];
  const ys = bands.map((db) => {
    const clamped = Math.max(-12, Math.min(12, Number(db) || 0));
    return 48 - (clamped / 12) * 36;
  });

  const pts = [
    { x: 0, y: ys[0] },
    ...xs.map((x, i) => ({ x, y: ys[i] })),
    { x: 320, y: ys[4] },
  ];

  let d = `M ${pts[0].x},${pts[0].y.toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i];
    const p1 = pts[i + 1];
    const cx = ((p0.x + p1.x) / 2).toFixed(1);
    d += ` C ${cx},${p0.y.toFixed(1)} ${cx},${p1.y.toFixed(1)} ${p1.x},${p1.y.toFixed(1)}`;
  }
  eqCurvePath.setAttribute("d", d);
  eqCurveArea.setAttribute("d", `${d} L 320,96 L 0,96 Z`);
}

function renderSpatialMap() {
  if (!roomState) return;
  const speakers = roomState.speakers || [];
  const canDragAll = canControlRoom();
  spatialNodesLayer.innerHTML = speakers
    .map((s) => {
      const leftPct = Math.max(8, Math.min(92, 50 + (s.pos_x || 0) * 42));
      const topPct = Math.max(10, Math.min(90, 50 + (s.pos_y || 0) * 40));
      const isSelf = s.id === mySpeakerId;
      const draggable = isSelf || canDragAll;
      return `<div class="spatial-node ${isSelf ? "is-self" : ""} ${draggable ? "draggable" : ""}" data-spatial-id="${escapeHtml(s.id)}" style="left:${leftPct}%;top:${topPct}%">${
        s.is_leader ? "DJ · " : ""
      }${escapeHtml(s.name)}</div>`;
    })
    .join("");
}

let draggingSpatialId = "";

function updateSpatialFromPointer(targetId, clientX, clientY) {
  const rect = spatialRoomCanvas.getBoundingClientRect();
  if (!rect.width || !rect.height || !targetId) return;
  const nx = Math.max(-1, Math.min(1, ((clientX - rect.left) / rect.width - 0.5) / 0.42));
  const ny = Math.max(-1, Math.min(1, ((clientY - rect.top) / rect.height - 0.5) / 0.4));
  if (targetId === mySpeakerId) {
    audioEngine.setSpatial({ pos_x: nx, pos_y: ny, spatial_enabled: true });
    spatialEnableToggle.checked = true;
  }
  const node = spatialNodesLayer.querySelector(`.spatial-node[data-spatial-id="${CSS.escape(targetId)}"]`);
  if (node) {
    node.style.left = `${50 + nx * 42}%`;
    node.style.top = `${50 + ny * 40}%`;
  }
  return { nx, ny };
}

spatialRoomCanvas.addEventListener("pointerdown", (e) => {
  const node = e.target.closest(".spatial-node");
  if (!node) return;
  const targetId = node.dataset.spatialId || "";
  if (targetId !== mySpeakerId && !canControlRoom()) return;
  draggingSpatialId = targetId;
  spatialRoomCanvas.setPointerCapture(e.pointerId);
});

spatialRoomCanvas.addEventListener("pointermove", (e) => {
  if (!draggingSpatialId) return;
  updateSpatialFromPointer(draggingSpatialId, e.clientX, e.clientY);
});

spatialRoomCanvas.addEventListener("pointerup", (e) => {
  if (!draggingSpatialId) return;
  const targetId = draggingSpatialId;
  draggingSpatialId = "";
  const pos = updateSpatialFromPointer(targetId, e.clientX, e.clientY);
  if (pos) {
    wsClient.send({
      type: "update_speaker",
      target_id: targetId,
      fields: { pos_x: pos.nx, pos_y: pos.ny, spatial_enabled: true },
    });
    if (targetId === mySpeakerId) {
      saveCloudPreset({ pos_x: pos.nx, pos_y: pos.ny });
    }
  }
});

spatialEnableToggle.addEventListener("change", () => {
  const enabled = spatialEnableToggle.checked;
  audioEngine.setSpatial({ spatial_enabled: enabled });
  wsClient.send({ type: "update_speaker", target_id: mySpeakerId, fields: { spatial_enabled: enabled } });
});

resetSpatialPosBtn.addEventListener("click", () => {
  audioEngine.setSpatial({ pos_x: 0, pos_y: 0 });
  wsClient.send({ type: "update_speaker", target_id: mySpeakerId, fields: { pos_x: 0, pos_y: 0 } });
  renderSpatialMap();
});

spatialPresetsRow?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-spatial-preset]");
  if (!btn || !roomState) return;
  const preset = btn.dataset.spatialPreset;
  const speakers = roomState.speakers || [];
  const targets = canControlRoom() ? speakers : speakers.filter((s) => s.id === mySpeakerId);

  const surroundCoords = [
    [-0.85, -0.75],
    [0.85, -0.75],
    [-0.85, 0.75],
    [0.85, 0.75],
  ];

  targets.forEach((sp, idx) => {
    let fields;
    if (preset === "center_off") {
      fields = { pos_x: 0, pos_y: 0, spatial_enabled: false };
    } else if (preset === "stereo_pair") {
      const side = idx % 2 === 0 ? -0.85 : 0.85;
      fields = { pos_x: side, pos_y: -0.35, spatial_enabled: true };
    } else {
      const [px, py] = surroundCoords[idx % surroundCoords.length];
      fields = { pos_x: px, pos_y: py, spatial_enabled: true };
    }
    if (sp.id === mySpeakerId) {
      audioEngine.setSpatial(fields);
      spatialEnableToggle.checked = fields.spatial_enabled;
    }
    wsClient.send({ type: "update_speaker", target_id: sp.id, fields });
  });
});

const PRESET_LABELS = {
  flat: "REFERENCE FLAT",
  party_bass: "CLUB SUB",
  hostel_boost: "LOUDNESS",
  vocal: "CRISP VOCAL",
  late_night: "MIDNIGHT SOFT",
};

function syncEqSlidersUI(preset, bands) {
  eqPresetButtons.querySelectorAll("[data-eq]").forEach((b) => {
    b.classList.toggle("active", b.dataset.eq === preset);
  });
  if (eqActivePresetLabel) {
    eqActivePresetLabel.textContent = PRESET_LABELS[preset] || "CUSTOM";
  }
  if (Array.isArray(bands) && bands.length === 5) {
    bands.forEach((v, idx) => {
      const inp = eqBandsContainer.querySelector(`input[data-band="${idx}"]`);
      const lbl = el(`eqVal${idx}`);
      if (inp && document.activeElement !== inp) inp.value = String(v);
      if (lbl) lbl.textContent = `${v >= 0 ? "+" : ""}${Math.round(v)}dB`;
    });
    updateEqCurveSvg(bands);
  }
}

eqPresetButtons.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-eq]");
  if (!btn) return;
  const preset = btn.dataset.eq;
  const bands = [...(EQ_PRESETS[preset] || EQ_PRESETS.flat)];
  audioEngine.setEq({ eq_preset: preset, eq_bands: bands });
  syncEqSlidersUI(preset, bands);
  wsClient.send({ type: "update_speaker", target_id: mySpeakerId, fields: { eq_preset: preset, eq_bands: bands } });
  saveCloudPreset({ eq_preset: preset });
});

eqBandsContainer.addEventListener("input", (e) => {
  const inp = e.target.closest("input[data-band]");
  if (!inp) return;
  const bands = [0, 1, 2, 3, 4].map((i) => Number(eqBandsContainer.querySelector(`input[data-band="${i}"]`)?.value || 0));
  audioEngine.setEq({ eq_bands: bands });
  if (eqActivePresetLabel) eqActivePresetLabel.textContent = "CUSTOM";
  syncEqSlidersUI("", bands);
});

eqBandsContainer.addEventListener("change", () => {
  const bands = [0, 1, 2, 3, 4].map((i) => Number(eqBandsContainer.querySelector(`input[data-band="${i}"]`)?.value || 0));
  wsClient.send({ type: "update_speaker", target_id: mySpeakerId, fields: { eq_bands: bands } });
});

// 1-Tap Automated Acoustic Sonar Calibration
micAutoCalBtn.addEventListener("click", async () => {
  if (!audioEngine.unlocked) completeUnlock(false);
  micAutoCalBtn.disabled = true;
  sonarCard?.classList.add("sonar-listening");

  const wasCalActive = Boolean(roomState?.calibration?.active);
  const startedPulseAutomatically = !wasCalActive && canControlRoom();
  const otherUnlockedSpeakers = (roomState?.speakers || []).filter(
    (s) => s.id !== mySpeakerId && s.audio_unlocked && !s.muted
  ).length;
  const prevMuted = audioEngine.speakerMuted;

  try {
    if (!wasCalActive) {
      if (canControlRoom()) {
        acousticResultText.textContent = "Starting room reference pulse…";
        wsClient.send({ type: "set_calibration", active: true });
        await sleep(850);
      } else {
        throw new Error("Ask the DJ to enable Sync Pulse in Room Tools first.");
      }
    }

    if (otherUnlockedSpeakers > 0 && !prevMuted) {
      audioEngine.setVolumes({ speakerMuted: true });
    }

    acousticResultText.textContent = "Listening via microphone… 10s remaining";
    const res = await audioEngine.runMicAutoCalibrate((secLeft) => {
      acousticResultText.textContent = `Listening via microphone… ${secLeft}s remaining`;
    });

    audioEngine.setOffsetMs(res.suggestedOffsetMs);
    localStorage.setItem("soundmesh_offset_ms", String(res.suggestedOffsetMs));
    localOffsetStat.textContent = `${res.suggestedOffsetMs} ms`;
    acousticResultText.textContent = `Locked: ${res.measuredLagMs}ms lag (~${res.distanceMeters}m) → applied ${res.suggestedOffsetMs}ms trim`;

    wsClient.send({
      type: "update_speaker",
      target_id: mySpeakerId,
      fields: { offset_ms: res.suggestedOffsetMs },
    });
    saveCloudPreset({ offset_ms: res.suggestedOffsetMs });
  } catch (err) {
    acousticResultText.textContent = err.message;
  } finally {
    if (otherUnlockedSpeakers > 0 && !prevMuted) {
      audioEngine.setVolumes({ speakerMuted: false });
    }
    if (startedPulseAutomatically) {
      wsClient.send({ type: "set_calibration", active: false });
    }
    sonarCard?.classList.remove("sonar-listening");
    micAutoCalBtn.disabled = false;
  }
});

// ------------------------------------------------------------------
// Music Search, Personal Library, Local Upload & Auto Radio
// ------------------------------------------------------------------

async function loadPersonalLibrary() {
  isViewingCollection = false;
  collectionHeader.classList.add("hidden");
  currentSearchTracks = [];
  currentSearchCollections = [];
  currentLibraryPlaylists = [];

  if (!currentUser || currentUser.provider !== "google" || !currentUser.has_youtube_scope) {
    searchStatus.classList.add("hidden");
    searchResults.innerHTML = `
      <div class="empty-state">
        <div class="empty-glyph">${svgIcon("lyrics", 26)}</div>
        <p>Link your Google account with YouTube access to load your personal playlists and Liked Music.</p>
        <button id="inlineConnectYtBtn" class="btn btn-primary btn-sm" style="margin-top:0.6rem">Connect Google Library</button>
      </div>
    `;
    el("inlineConnectYtBtn")?.addEventListener("click", () => startGoogleLogin(true));
    return;
  }

  searchStatus.textContent = "Fetching your YouTube Music library…";
  searchStatus.classList.remove("hidden");
  try {
    const resp = await apiFetch("/api/me/playlists");
    const data = await resp.json();
    searchStatus.classList.add("hidden");
    if (!resp.ok) throw new Error(data.detail || "Unable to load playlists");
    currentLibraryPlaylists = data.playlists || [];
    if (!currentLibraryPlaylists.length) {
      searchResults.innerHTML = `<div class="empty-state compact"><p>No YouTube playlists found.</p></div>`;
      return;
    }
    searchResults.innerHTML = currentLibraryPlaylists
      .map(
        (c, idx) => `
        <div class="track-row" data-lib-idx="${idx}" style="cursor:pointer">
          <img class="track-thumb" src="${escapeHtml(safeImageUrl(c.thumbnail))}" alt="" loading="lazy" />
          <div class="track-info">
            <div class="track-title">${escapeHtml(c.title)}</div>
            <div class="track-sub">${escapeHtml(c.subtitle)} ${c.item_count ? `· ${escapeHtml(c.item_count)} tracks` : ""}</div>
          </div>
          <div class="track-actions"><button class="btn btn-outline btn-xs">Open</button></div>
        </div>`
      )
      .join("");
  } catch (err) {
    searchStatus.textContent = `Error: ${err.message}`;
  }
}

async function runSearch(query, filter = currentFilter) {
  if (filter === "library") {
    await loadPersonalLibrary();
    return;
  }
  const q = (query || "").trim();
  if (!q) return;

  isViewingCollection = false;
  collectionHeader.classList.add("hidden");
  searchStatus.textContent = "Searching catalog…";
  searchStatus.classList.remove("hidden");

  try {
    const resp = await apiFetch(`/api/search?q=${encodeURIComponent(q)}&filter=${encodeURIComponent(filter)}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Search failed");
    searchStatus.classList.add("hidden");
    currentSearchTracks = data.tracks || [];
    currentSearchCollections = data.collections || [];
    if (data.resolved_url && data.playlist_title && currentSearchTracks.length > 0) {
      showCollectionView(data.playlist_title, "Direct Link", currentSearchTracks);
      return;
    }
    renderSearchResults();
  } catch (err) {
    searchStatus.textContent = `Error: ${err.message}`;
  }
}

function renderSearchResults() {
  if (currentSearchCollections.length > 0) {
    searchResults.innerHTML = currentSearchCollections
      .map(
        (c, idx) => `
        <div class="track-row" data-col-idx="${idx}" style="cursor:pointer">
          <img class="track-thumb" src="${escapeHtml(safeImageUrl(c.thumbnail))}" alt="" loading="lazy" />
          <div class="track-info">
            <div class="track-title">${escapeHtml(c.title)}</div>
            <div class="track-sub">${escapeHtml(c.subtitle)} ${c.year ? `· ${escapeHtml(c.year)}` : ""}</div>
          </div>
          <div class="track-actions"><button class="btn btn-outline btn-xs">Open</button></div>
        </div>`
      )
      .join("");
    return;
  }

  if (!currentSearchTracks.length) {
    searchResults.innerHTML = `<div class="empty-state compact"><p>No matching tracks found.</p></div>`;
    return;
  }

  const canPlay = canControlRoom();
  searchResults.innerHTML = currentSearchTracks
    .map(
      (t, idx) => `
      <div class="track-row">
        <img class="track-thumb" src="${escapeHtml(safeImageUrl(t.thumbnail))}" alt="" loading="lazy" />
        <div class="track-info">
          <div class="track-title">${escapeHtml(t.title)}</div>
          <div class="track-sub">${escapeHtml(t.artist)} · ${formatTime(t.duration_sec)} ${
            t.cached ? '<span class="cached-tag">READY</span>' : ""
          }</div>
        </div>
        <div class="track-actions">
          ${canPlay ? `<button class="btn btn-primary btn-xs" data-action="play-now" data-idx="${idx}">Play</button>` : ""}
          <button class="btn btn-outline btn-xs" data-action="queue-add" data-idx="${idx}">+Q</button>
        </div>
      </div>`
    )
    .join("");
}

function showCollectionView(title, subtitle, tracks) {
  isViewingCollection = true;
  currentCollectionTracks = tracks || [];
  collectionTitle.textContent = title;
  collectionSubtitle.textContent = `${subtitle} · ${currentCollectionTracks.length} tracks`;
  collectionHeader.classList.remove("hidden");

  const canPlay = canControlRoom();
  searchResults.innerHTML = currentCollectionTracks
    .map(
      (t, idx) => `
      <div class="track-row">
        <img class="track-thumb" src="${escapeHtml(safeImageUrl(t.thumbnail))}" alt="" loading="lazy" />
        <div class="track-info">
          <div class="track-title">${escapeHtml(t.title)}</div>
          <div class="track-sub">${escapeHtml(t.artist)} · ${formatTime(t.duration_sec)}</div>
        </div>
        <div class="track-actions">
          ${canPlay ? `<button class="btn btn-primary btn-xs" data-action="play-now" data-idx="${idx}">Play</button>` : ""}
          <button class="btn btn-outline btn-xs" data-action="queue-add" data-idx="${idx}">+Q</button>
        </div>
      </div>`
    )
    .join("");
}

async function openCollection(col, isPersonalLibrary = false) {
  searchStatus.textContent = `Loading ${col.title}…`;
  searchStatus.classList.remove("hidden");
  try {
    const endpoint = isPersonalLibrary
      ? `/api/me/playlists/${encodeURIComponent(col.id)}/tracks`
      : col.kind === "album"
        ? `/api/album/${encodeURIComponent(col.id)}`
        : `/api/playlist/${encodeURIComponent(col.id)}`;
    const resp = await apiFetch(endpoint);
    const data = await resp.json();
    searchStatus.classList.add("hidden");
    showCollectionView(col.title || data.title, col.subtitle || data.subtitle, data.tracks || []);
  } catch (err) {
    searchStatus.textContent = `Failed loading ${col.title}: ${err.message}`;
  }
}

async function triggerAutoRadio() {
  const curTrack = roomState?.playback?.track || roomState?.queue?.[0];
  if (!curTrack || !curTrack.video_id || curTrack.video_id.startsWith("local_")) {
    showToast("Play a YouTube track first so Auto Radio can match its vibe.");
    return;
  }
  closeAllPopovers();
  if (loadRadioBtn) loadRadioBtn.disabled = true;
  if (queueAutoRadioBtn) queueAutoRadioBtn.disabled = true;
  showToast(`Building Auto Radio mix from "${curTrack.title}"…`);
  try {
    const qs = new URLSearchParams({
      title: curTrack.title || "",
      artist: curTrack.artist || "",
    });
    const resp = await apiFetch(`/api/related/${encodeURIComponent(curTrack.video_id)}?${qs.toString()}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Radio request failed");
    if (Array.isArray(data.tracks) && data.tracks.length > 0) {
      wsClient.send({ type: "add_to_queue", tracks: data.tracks, play_if_idle: true });
      showToast(`Added ${data.tracks.length} Auto Radio tracks`);
    } else {
      showToast("No related songs found for this track.");
    }
  } catch (err) {
    showToast(`Auto Radio failed: ${err.message}`);
  } finally {
    const allowed = canControlRoom();
    if (loadRadioBtn) loadRadioBtn.disabled = !allowed;
    if (queueAutoRadioBtn) queueAutoRadioBtn.disabled = !allowed;
  }
}

loadRadioBtn?.addEventListener("click", triggerAutoRadio);
queueAutoRadioBtn?.addEventListener("click", triggerAutoRadio);

localAudioFileInput.addEventListener("change", async () => {
  const file = localAudioFileInput.files?.[0];
  if (!file || !activeRoomCode) return;
  const fd = new FormData();
  fd.append("file", file);
  showToast(`Uploading ${file.name}…`);
  try {
    const upHeaders = activeRoomJoinTicket ? { "X-Room-Join-Ticket": activeRoomJoinTicket } : {};
    const resp = await apiFetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/upload`, {
      method: "POST",
      headers: upHeaders,
      body: fd,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Upload failed");
    if (canControlRoom()) {
      wsClient.send({ type: "play_now", track: data.track });
    } else {
      wsClient.send({ type: "add_to_queue", tracks: [data.track], play_if_idle: true });
    }
    showToast(`Loaded "${data.track.title}"`);
  } catch (err) {
    showToast(`Upload error: ${err.message}`);
  } finally {
    localAudioFileInput.value = "";
  }
});

async function fetchAndRenderLyrics(videoId) {
  if (!videoId || videoId.startsWith("local_")) {
    lyricsBody.textContent = "Lyrics unavailable for local uploads.";
    return;
  }
  lastLyricsVideoId = videoId;
  lyricsBody.textContent = "Loading lyrics…";
  try {
    const resp = await apiFetch(`/api/lyrics/${encodeURIComponent(videoId)}`);
    const data = await resp.json();
    lyricsBody.textContent = data.available && data.lyrics ? data.lyrics : "No synced lyrics found.";
  } catch {
    lyricsBody.textContent = "Unable to load lyrics right now.";
  }
}

toggleLyricsBtn.addEventListener("click", () => {
  closeAllPopovers();
  const willOpen = lyricsPanel.classList.contains("hidden");
  lyricsPanel.classList.toggle("hidden", !willOpen);
  if (willOpen && roomState?.playback?.track?.video_id) {
    fetchAndRenderLyrics(roomState.playback.track.video_id);
  }
});

closeLyricsBtn.addEventListener("click", () => lyricsPanel.classList.add("hidden"));

// ------------------------------------------------------------------
// Studio & Transport Event Listeners
// ------------------------------------------------------------------

searchForm.addEventListener("submit", (e) => {
  e.preventDefault();
  if (currentFilter === "library") {
    currentFilter = "songs";
    searchFilterTabs.querySelectorAll(".tab-chip").forEach((b) => b.classList.toggle("active", b.dataset.filter === "songs"));
  }
  runSearch(searchInput.value, currentFilter);
});

searchFilterTabs.addEventListener("click", (e) => {
  const btn = e.target.closest(".tab-chip");
  if (!btn) return;
  currentFilter = btn.dataset.filter || "songs";
  searchFilterTabs.querySelectorAll(".tab-chip").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  if (currentFilter === "library") loadPersonalLibrary();
  else if (searchInput.value.trim()) runSearch(searchInput.value, currentFilter);
});

quickChips.addEventListener("click", (e) => {
  const chip = e.target.closest(".quick-chip");
  if (!chip) return;
  const q = chip.dataset.query || "";
  searchInput.value = q;
  currentFilter = "songs";
  searchFilterTabs.querySelectorAll(".tab-chip").forEach((b) => b.classList.toggle("active", b.dataset.filter === "songs"));
  runSearch(q, "songs");
});

collectionBackBtn.addEventListener("click", () => {
  isViewingCollection = false;
  collectionHeader.classList.add("hidden");
  if (currentFilter === "library") loadPersonalLibrary();
  else renderSearchResults();
});

collectionPlayAllBtn.addEventListener("click", () => {
  if (!currentCollectionTracks.length) return;
  wsClient.send({ type: "clear_queue" });
  wsClient.send({ type: "add_to_queue", tracks: currentCollectionTracks, play_if_idle: true });
  if (currentUiMode === "minimal") setActivePage("stage");
});

collectionQueueAllBtn.addEventListener("click", () => {
  if (!currentCollectionTracks.length) return;
  wsClient.send({ type: "add_to_queue", tracks: currentCollectionTracks, play_if_idle: true });
});

searchResults.addEventListener("click", (e) => {
  const libRow = e.target.closest("[data-lib-idx]");
  if (libRow) {
    const col = currentLibraryPlaylists[Number(libRow.dataset.libIdx)];
    if (col) openCollection(col, true);
    return;
  }
  const colRow = e.target.closest("[data-col-idx]");
  if (colRow) {
    const col = currentSearchCollections[Number(colRow.dataset.colIdx)];
    if (col) openCollection(col, false);
    return;
  }
  const actionBtn = e.target.closest("[data-action]");
  if (!actionBtn) return;
  const list = isViewingCollection ? currentCollectionTracks : currentSearchTracks;
  const track = list[Number(actionBtn.dataset.idx)];
  if (!track) return;

  if (actionBtn.dataset.action === "play-now") {
    wsClient.send({ type: "play_now", track });
    if (currentUiMode === "minimal") setActivePage("stage");
  } else if (actionBtn.dataset.action === "queue-add") {
    wsClient.send({ type: "add_to_queue", tracks: [track], play_if_idle: true });
    showToast(`Queued "${track.title}"`);
  }
});

playPauseBtn.addEventListener("click", () => {
  if (!roomState) return;
  wsClient.send({ type: roomState.playback.status === "playing" ? "pause" : "resume" });
});
prevBtn.addEventListener("click", () => wsClient.send({ type: "prev" }));
nextBtn.addEventListener("click", () => wsClient.send({ type: "next" }));
resyncAllBtn.addEventListener("click", () => {
  closeAllPopovers();
  wsClient.send({ type: "force_resync" });
  showToast("Tightening sync across speakers…");
});
shuffleBtn.addEventListener("click", () => wsClient.send({ type: "set_shuffle", enabled: !roomState?.shuffle }));
repeatBtn.addEventListener("click", () => {
  const order = ["off", "all", "one"];
  wsClient.send({ type: "set_repeat", mode: order[(order.indexOf(roomState?.repeat || "off") + 1) % 3] });
});

seekSlider.addEventListener("input", () => {
  isSeeking = true;
  timeCurrent.textContent = formatTime(Number(seekSlider.value));
});
seekSlider.addEventListener("change", () => {
  isSeeking = false;
  wsClient.send({ type: "seek", position_sec: Number(seekSlider.value) });
});

reactionBar.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-emoji]");
  if (!btn) return;
  wsClient.send({ type: "send_reaction", emoji: btn.dataset.emoji });
  closeAllPopovers();
});

clearQueueBtn.addEventListener("click", () => wsClient.send({ type: "clear_queue" }));

queueList.addEventListener("click", (e) => {
  const target = e.target.closest("[data-action]");
  if (!target) return;
  const idx = Number(target.dataset.idx);
  const action = target.dataset.action;
  if (action === "play-idx") wsClient.send({ type: "play_index", index: idx });
  else if (action === "upvote") wsClient.send({ type: "upvote_queue", index: idx });
  else if (action === "up" && idx > 0) wsClient.send({ type: "move_queue", from_index: idx, to_index: idx - 1 });
  else if (action === "down" && roomState && idx < roomState.queue.length - 1) {
    wsClient.send({ type: "move_queue", from_index: idx, to_index: idx + 1 });
  } else if (action === "remove") wsClient.send({ type: "remove_queue", index: idx });
});

masterVolumeSlider.addEventListener("input", () => {
  const val = Number(masterVolumeSlider.value) / 100;
  masterVolumeLabel.textContent = `${masterVolumeSlider.value}%`;
  wsClient.send({ type: "set_master_volume", volume: val });
});
masterMuteBtn.addEventListener("click", () => wsClient.send({ type: "set_master_volume", muted: !roomState?.master_muted }));
guestControlToggle.addEventListener("change", () => wsClient.send({ type: "set_allow_guest_control", allowed: guestControlToggle.checked }));
collabQueueToggle.addEventListener("change", () => wsClient.send({ type: "set_allow_collab_queue", allowed: collabQueueToggle.checked }));
muteMeControllerToggle.addEventListener("change", () => {
  audioEngine.setVolumes({ speakerMuted: muteMeControllerToggle.checked });
  wsClient.send({ type: "update_speaker", target_id: mySpeakerId, fields: { muted: muteMeControllerToggle.checked } });
});

calibrateToggleBtn.addEventListener("click", () => {
  closeAllPopovers();
  wsClient.send({ type: "set_calibration", active: !roomState?.calibration?.active });
});
stopCalibrationBtn.addEventListener("click", () => wsClient.send({ type: "set_calibration", active: false }));
autoStereoBtn.addEventListener("click", () => {
  closeAllPopovers();
  wsClient.send({ type: "auto_assign_stereo" });
});
pingAllBtn.addEventListener("click", () => {
  closeAllPopovers();
  wsClient.send({ type: "identify_speaker", target_id: "all" });
});

requestLeaderBtn.addEventListener("click", () => {
  wsClient.send({ type: "request_leader" });
  showToast("Asked current DJ for the crown");
});
takeLeaderBtn.addEventListener("click", () => wsClient.send({ type: "assign_leader", target_id: mySpeakerId }));

leaderRequestsDrawer.addEventListener("click", (e) => {
  const appBtn = e.target.closest("[data-approve-req]");
  if (appBtn) {
    wsClient.send({ type: "respond_leader_request", requester_id: appBtn.dataset.approveReq, approve: true });
    return;
  }
  const decBtn = e.target.closest("[data-decline-req]");
  if (decBtn) wsClient.send({ type: "respond_leader_request", requester_id: decBtn.dataset.declineReq, approve: false });
});

document.querySelectorAll(".nudge-btn[data-nudge]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const raw = btn.dataset.nudge;
    const nextOffset = raw === "reset" ? 0 : Math.max(-500, Math.min(500, Math.round(audioEngine.offsetMs + Number(raw))));
    audioEngine.setOffsetMs(nextOffset);
    localStorage.setItem("soundmesh_offset_ms", String(nextOffset));
    localOffsetStat.textContent = `${nextOffset} ms`;
    wsClient.send({ type: "update_speaker", target_id: mySpeakerId, fields: { offset_ms: nextOffset } });
    saveCloudPreset({ offset_ms: nextOffset });
  });
});

speakerList.addEventListener("change", (e) => {
  const card = e.target.closest(".speaker-card");
  if (!card) return;
  const targetId = card.dataset.speakerId;
  const field = e.target.dataset.field;
  if (!field) return;

  let value = e.target.value;
  if (field === "volume") value = Number(value) / 100;
  if (field === "offset_ms") value = Number(value);

  if (targetId === mySpeakerId) {
    if (field === "name") {
      localStorage.setItem("soundmesh_speaker_name", value);
      saveCloudPreset({ name: value });
    }
    if (field === "role") {
      localStorage.setItem("soundmesh_role", value);
      audioEngine.setRole(value);
      saveCloudPreset({ role: value });
    }
    if (field === "offset_ms") {
      localStorage.setItem("soundmesh_offset_ms", String(value));
      audioEngine.setOffsetMs(value);
      localOffsetStat.textContent = `${Math.round(value)} ms`;
      saveCloudPreset({ offset_ms: value });
    }
  }
  wsClient.send({ type: "update_speaker", target_id: targetId, fields: { [field]: value } });
});

speakerList.addEventListener("input", (e) => {
  const card = e.target.closest(".speaker-card");
  if (!card) return;
  const targetId = card.dataset.speakerId;
  const field = e.target.dataset.field;
  if (field === "offset_ms") {
    const val = Number(e.target.value);
    const label = card.querySelector("[data-role='offset-label']");
    if (label) label.textContent = `${Math.round(val)}ms`;
    if (targetId === mySpeakerId) {
      audioEngine.setOffsetMs(val);
      localOffsetStat.textContent = `${Math.round(val)} ms`;
    }
  } else if (field === "volume" && targetId === mySpeakerId) {
    audioEngine.setVolumes({ speakerVolume: Number(e.target.value) / 100 });
  }
});

speakerList.addEventListener("click", (e) => {
  const card = e.target.closest(".speaker-card");
  const btn = e.target.closest("[data-action]");
  if (!card || !btn) return;
  const targetId = card.dataset.speakerId;
  const action = btn.dataset.action;

  if (action === "toggle-tune") {
    const drawer = card.querySelector(".speaker-tune-drawer");
    if (!drawer) return;
    const willOpen = drawer.classList.contains("hidden");
    drawer.classList.toggle("hidden", !willOpen);
    if (willOpen) expandedSpeakerIds.add(targetId);
    else expandedSpeakerIds.delete(targetId);
    btn.setAttribute("aria-expanded", String(willOpen));
    const lbl = btn.querySelector("span");
    if (lbl) lbl.textContent = willOpen ? "Done" : "Tune ▾";
    return;
  }

  if (action === "make-leader") wsClient.send({ type: "assign_leader", target_id: targetId });
  else if (action === "ping") wsClient.send({ type: "identify_speaker", target_id: targetId });
  else if (action === "remove-speaker") wsClient.send({ type: "remove_speaker", target_id: targetId });
  else if (action === "toggle-mute") {
    const sp = roomState?.speakers?.find((s) => s.id === targetId);
    wsClient.send({ type: "update_speaker", target_id: targetId, fields: { muted: !(sp?.muted ?? false) } });
  } else if (action === "nudge") {
    const sp = roomState?.speakers?.find((s) => s.id === targetId);
    const nextOffset = Math.max(-500, Math.min(500, Math.round((sp?.offset_ms || 0) + Number(btn.dataset.delta || 0))));
    if (targetId === mySpeakerId) {
      audioEngine.setOffsetMs(nextOffset);
      localStorage.setItem("soundmesh_offset_ms", String(nextOffset));
      localOffsetStat.textContent = `${nextOffset} ms`;
    }
    wsClient.send({ type: "update_speaker", target_id: targetId, fields: { offset_ms: nextOffset } });
  }
});

// QR, Share & Tunnel Modals
async function openQrModal() {
  const code = activeRoomCode || "STUDIO";
  qrRoomCodeText.textContent = `#${code}`;
  qrModal.classList.remove("hidden");
  nativeShareBtn.classList.toggle("hidden", !navigator.share);
  try {
    const resp = await apiFetch(`/api/rooms/${encodeURIComponent(code)}/info`);
    const info = await resp.json();
    qrImage.src = info.qr_data_uri;
    qrUrlInput.value = info.join_url;
    denoWarning.classList.toggle("hidden", Boolean(info.deno_available));
    if (Array.isArray(info.join_urls) && info.join_urls.length > 1) {
      qrIpPickerWrap.classList.remove("hidden");
      qrIpSelect.innerHTML = info.join_urls
        .map((url) => `<option value="${escapeHtml(url)}">${escapeHtml(url)}</option>`)
        .join("");
    } else {
      qrIpPickerWrap.classList.add("hidden");
    }
  } catch {
    qrUrlInput.value = `${location.origin}/room/${code}`;
  }
}

qrIpSelect.addEventListener("change", () => {
  const url = qrIpSelect.value;
  qrUrlInput.value = url;
  qrImage.src = apiUrl(`/api/qr?url=${encodeURIComponent(url)}`);
});

nativeShareBtn.addEventListener("click", async () => {
  try {
    await navigator.share({
      title: `Join SoundMesh Room #${activeRoomCode}`,
      text: "Connect your phone as a synced speaker in my SoundMesh room!",
      url: qrUrlInput.value,
    });
  } catch {
    // User cancelled share
  }
});

openQrBtn.addEventListener("click", openQrModal);
roomCodeBadge.addEventListener("click", openQrModal);
closeQrBtn.addEventListener("click", () => qrModal.classList.add("hidden"));
qrModal.addEventListener("click", (e) => {
  if (e.target === qrModal) qrModal.classList.add("hidden");
});
closeOauthSetupBtn.addEventListener("click", () => oauthSetupModal.classList.add("hidden"));

copyUrlBtn.addEventListener("click", async () => {
  const text = qrUrlInput.value;
  try {
    if (navigator.clipboard && globalThis.isSecureContext) await navigator.clipboard.writeText(text);
    else {
      qrUrlInput.select();
      document.execCommand("copy");
    }
    copyUrlBtn.textContent = "Copied";
    setTimeout(() => (copyUrlBtn.textContent = "Copy"), 1400);
  } catch {
    qrUrlInput.select();
  }
});

// ------------------------------------------------------------------
// Free Public HTTPS Link (Cloudflare Quick Tunnel)
// ------------------------------------------------------------------

let tunnelPollTimer = null;

async function refreshTunnelStatus() {
  try {
    const resp = await apiFetch("/api/tunnel/status");
    if (!resp.ok) return;
    const st = await resp.json();
    renderTunnelUI(st);
    if (st.starting && tunnelPollTimer === null) {
      tunnelPollTimer = setInterval(refreshTunnelStatus, 2000);
    } else if (!st.starting && tunnelPollTimer !== null) {
      clearInterval(tunnelPollTimer);
      tunnelPollTimer = null;
    }
  } catch {
    // Ignore
  }
}

function renderTunnelUI(st) {
  if (!st) return;
  const active = Boolean(st.active && st.public_url);
  const canControl = Boolean(st.can_control);
  if (!active && !canControl) {
    publicHostBar.classList.add("hidden");
    return;
  }
  publicHostBar.classList.remove("hidden");
  publicHostInactive.classList.toggle("hidden", active);
  publicHostActive.classList.toggle("hidden", !active);
  if (active) {
    livePublicLinkAnchor.href = st.public_url;
    livePublicLinkAnchor.textContent = st.public_url;
    stopTunnelBtn.classList.toggle("hidden", !canControl);
  } else {
    startTunnelBtn.disabled = Boolean(st.starting);
    startTunnelBtn.textContent = st.starting ? "Starting tunnel…" : "Go Live Online";
  }
}

startTunnelBtn.addEventListener("click", async () => {
  startTunnelBtn.disabled = true;
  startTunnelBtn.textContent = "Starting tunnel…";
  try {
    const resp = await apiFetch("/api/tunnel/start", { method: "POST" });
    const data = await resp.json();
    renderTunnelUI(data);
    if (data.active && data.public_url) showToast(`Public tunnel live: ${data.public_url}`);
    else if (data.error) showToast(`Tunnel error: ${data.error}`);
  } catch {
    showToast("Unable to start public tunnel.");
  } finally {
    refreshTunnelStatus();
  }
});

stopTunnelBtn.addEventListener("click", async () => {
  await apiFetch("/api/tunnel/stop", { method: "POST" });
  showToast("Public tunnel stopped.");
  refreshTunnelStatus();
});

copyLiveLinkBtn.addEventListener("click", async () => {
  const url = livePublicLinkAnchor.textContent || "";
  if (!url) return;
  try {
    if (navigator.clipboard && globalThis.isSecureContext) await navigator.clipboard.writeText(url);
    copyLiveLinkBtn.textContent = "Copied";
    setTimeout(() => (copyLiveLinkBtn.textContent = "Copy Link"), 1400);
  } catch {
    showToast(url);
  }
});

// ------------------------------------------------------------------
// 60fps Dual Visualizers: Mirrored Harmonic Bars + 360° Radial Halo
// ------------------------------------------------------------------

const freqBins = new Uint8Array(64);
const smoothBins = new Float32Array(44);
const vizCtx = visualizerCanvas.getContext("2d");
const haloCtx = haloCanvas ? haloCanvas.getContext("2d") : null;

function animationLoop() {
  const pb = roomState?.playback;
  if (pb && !isSeeking && activeRoomCode) {
    const pos = audioEngine.getExpectedPositionSec();
    const dur = pb.audio_duration_sec || pb.track?.duration_sec || 0;
    const clampedPos = dur > 0 ? Math.min(dur, pos) : pos;
    timeCurrent.textContent = formatTime(clampedPos);
    seekSlider.value = String(clampedPos);
  }

  if (activeRoomCode) {
    const bass = audioEngine.getBassEnergy();
    document.documentElement.style.setProperty("--bass", bass.toFixed(3));

    const hasSignal = audioEngine.getFrequencyData(freqBins);
    const { r, g, b } = activeAccentRgb;

    if (vizCtx) {
      const w = visualizerCanvas.width;
      const h = visualizerCanvas.height;
      vizCtx.clearRect(0, 0, w, h);

      const halfCount = 22;
      const totalBars = halfCount * 2;
      const gap = 3;
      const barW = (w - (totalBars - 1) * gap) / totalBars;
      const midY = h / 2;

      for (let i = 0; i < halfCount; i++) {
        const raw = hasSignal ? freqBins[i * 2] / 255 : 0.04;
        smoothBins[i] = smoothBins[i] * 0.76 + raw * 0.24;
        const amp = Math.max(0.05, smoothBins[i]);
        const halfH = Math.max(2, amp * (midY - 3));

        const alpha = hasSignal && raw > 0.06 ? 0.35 + amp * 0.65 : 0.16;
        vizCtx.fillStyle = `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;

        const rightIdx = halfCount + i;
        const leftIdx = halfCount - 1 - i;

        const xR = rightIdx * (barW + gap);
        const xL = leftIdx * (barW + gap);

        vizCtx.beginPath();
        vizCtx.roundRect(xR, midY - halfH, barW, halfH * 2, 2);
        vizCtx.roundRect(xL, midY - halfH, barW, halfH * 2, 2);
        vizCtx.fill();
      }
    }

    if (haloCtx && haloCanvas) {
      const hw = haloCanvas.width;
      const hh = haloCanvas.height;
      haloCtx.clearRect(0, 0, hw, hh);

      const cx = hw / 2;
      const cy = hh / 2;
      const baseR = currentUiMode === "party" ? 102 : 84;
      const rays = 56;

      haloCtx.lineCap = "round";
      haloCtx.lineWidth = currentUiMode === "party" ? 3 : 2;

      for (let i = 0; i < rays; i++) {
        const binIdx = i % 28;
        const val = hasSignal ? freqBins[binIdx] / 255 : 0.03;
        const rayLen = 3 + val * (currentUiMode === "party" ? 42 : 22);
        const angle = (i / rays) * Math.PI * 2 - Math.PI / 2;

        const x1 = cx + Math.cos(angle) * baseR;
        const y1 = cy + Math.sin(angle) * baseR;
        const x2 = cx + Math.cos(angle) * (baseR + rayLen);
        const y2 = cy + Math.sin(angle) * (baseR + rayLen);

        const alpha = hasSignal && val > 0.08 ? 0.22 + val * 0.68 : 0.09;
        haloCtx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;
        haloCtx.beginPath();
        haloCtx.moveTo(x1, y1);
        haloCtx.lineTo(x2, y2);
        haloCtx.stroke();
      }
    }
  }

  requestAnimationFrame(animationLoop);
}

// ------------------------------------------------------------------
// Bootstrap
// ------------------------------------------------------------------

function handleInitialRoute(push = false) {
  const params = new URLSearchParams(location.search);
  const authErr = params.get("auth_error");
  if (authErr) {
    showToast(`Google Sign-In failed: ${authErr}`);
    history.replaceState({}, "", location.pathname);
  }

  const roomMatch = location.pathname.match(/^\/room\/([A-Za-z0-9_-]+)/);
  if (roomMatch) {
    attemptEnterRoom(roomMatch[1], "", true);
    return;
  }
  const roomQuery = params.get("room");
  if (roomQuery) {
    attemptEnterRoom(roomQuery, "", true);
    return;
  }
  if (location.pathname.startsWith("/join")) {
    enterRoom("STUDIO", "", false);
    return;
  }
  navigateToLobby(push);
}

(async function init() {
  updateEqCurveSvg([0, 0, 0, 0, 0]);
  applyUiMode(currentUiMode);
  await refreshAuth();
  await refreshTunnelStatus();
  handleInitialRoute(false);
  requestAnimationFrame(animationLoop);
})();

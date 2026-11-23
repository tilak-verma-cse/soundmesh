import { ClockSync } from "./clock_sync.js";
import { WSClient } from "./ws_client.js";
import { SyncAudioEngine } from "./audio_engine.js";

// ------------------------------------------------------------------
// Helpers & Local Identity
// ------------------------------------------------------------------

const el = (id) => document.getElementById(id);

function getOrCreateClientId() {
  let id = localStorage.getItem("soundmesh_client_id");
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14)).slice(0, 12);
    localStorage.setItem("soundmesh_client_id", id);
  }
  return id;
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
  return "Computer";
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
  if (hrs > 0) {
    return `${hrs}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function avatarHtml(name, avatarUrl, sizeClass = "avatar-sm") {
  if (avatarUrl) {
    return `<img class="${sizeClass}" src="${escapeHtml(avatarUrl)}" alt="" referrerpolicy="no-referrer" />`;
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
  setTimeout(() => {
    node.remove();
  }, 3600);
}

const deviceType = detectDeviceType();
const clientId = getOrCreateClientId();
const savedRole = localStorage.getItem("soundmesh_role") || "stereo";
const savedOffsetMs = Number(localStorage.getItem("soundmesh_offset_ms") || "0") || 0;

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

const roomTopControls = el("roomTopControls");
const openQrBtn = el("openQrBtn");
const calibrateToggleBtn = el("calibrateToggleBtn");
const guestControlWrap = el("guestControlWrap");
const guestControlToggle = el("guestControlToggle");
const masterMuteBtn = el("masterMuteBtn");
const masterVolumeSlider = el("masterVolumeSlider");
const masterVolumeLabel = el("masterVolumeLabel");
const navAccountArea = el("navAccountArea");

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
const connectionBanner = el("connectionBanner");
const calibrationBanner = el("calibrationBanner");
const stopCalibrationBtn = el("stopCalibrationBtn");

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
const nowArtwork = el("nowArtwork");
const nowArtworkPlaceholder = el("nowArtworkPlaceholder");
const loadingOverlay = el("loadingOverlay");
const loadingPhaseText = el("loadingPhaseText");
const nowTitle = el("nowTitle");
const nowArtist = el("nowArtist");
const nowError = el("nowError");
const visualizerCanvas = el("visualizerCanvas");
const timeCurrent = el("timeCurrent");
const timeDuration = el("timeDuration");
const seekSlider = el("seekSlider");

const shuffleBtn = el("shuffleBtn");
const prevBtn = el("prevBtn");
const playPauseBtn = el("playPauseBtn");
const nextBtn = el("nextBtn");
const repeatBtn = el("repeatBtn");
const resyncAllBtn = el("resyncAllBtn");
const loadRadioBtn = el("loadRadioBtn");
const queueCountBadge = el("queueCountBadge");
const clearQueueBtn = el("clearQueueBtn");
const queueList = el("queueList");

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

const localSyncBadge = el("localSyncBadge");
const localRttStat = el("localRttStat");
const localDriftStat = el("localDriftStat");
const localOffsetStat = el("localOffsetStat");
const speakerList = el("speakerList");

const qrModal = el("qrModal");
const closeQrBtn = el("closeQrBtn");
const qrRoomCodeText = el("qrRoomCodeText");
const qrImage = el("qrImage");
const qrIpPickerWrap = el("qrIpPickerWrap");
const qrIpSelect = el("qrIpSelect");
const qrUrlInput = el("qrUrlInput");
const copyUrlBtn = el("copyUrlBtn");
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
  callback_url: `${location.origin}/api/auth/google/callback`,
  base_url: location.origin,
  is_private_ip: false,
};

let activeRoomCode = null;
let activeRoomPasscode = "";
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

// ------------------------------------------------------------------
// Sync & Audio Engine
// ------------------------------------------------------------------

let wsClient = null;

const clockSync = new ClockSync(
  (ping) => {
    if (wsClient) wsClient.send(ping);
  },
  (info) => {
    headerRttText.textContent = `${Math.round(info.rttMs)} ms RTT`;
    localRttStat.textContent = `${Math.round(info.rttMs)} ms`;
  }
);

const audioEngine = new SyncAudioEngine(clockSync, {
  onReady: (generation) => {
    wsClient?.send({ type: "track_ready", generation });
  },
  onTelemetry: (tel) => {
    updateLocalTelemetryUI(tel);
    wsClient?.send({ type: "telemetry", ...tel });
  },
  onIdentifyVisual: () => {
    const selfCard = document.querySelector(`.speaker-card[data-speaker-id="${clientId}"]`);
    if (selfCard) {
      selfCard.classList.remove("ping-flash");
      void selfCard.offsetWidth;
      selfCard.classList.add("ping-flash");
      setTimeout(() => selfCard.classList.remove("ping-flash"), 1600);
    }
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
        target_id: clientId,
        fields: { audio_unlocked: true },
      });
    }
  },
  onFatalClose: (code) => {
    if (code === 4403) {
      openPasscodeModal(activeRoomCode || "");
    } else if (code === 4404) {
      showToast("Room not found or has closed.");
      navigateToLobby();
    }
  },
  onMessage: (msg) => {
    handleServerMessage(msg);
  },
});

// ------------------------------------------------------------------
// Auth & Account UI
// ------------------------------------------------------------------

async function refreshAuth() {
  const resp = await fetch("/api/auth/me");
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
  const url = `/api/auth/google/login?next=${encodeURIComponent(nextPath)}&include_youtube=${
    includeYt ? "1" : "0"
  }`;
  location.href = url;
}

function renderAccountUI() {
  lobbyLanNotice.classList.toggle("hidden", !authConfig.is_private_ip);

  if (!currentUser) {
    accountModeBadge.textContent = "Not Signed In";
    lobbySignedOut.classList.remove("hidden");
    lobbySignedIn.classList.add("hidden");
    navAccountArea.innerHTML = `
      <button id="navGoogleSignInBtn" class="btn btn-outline btn-xs">Sign in with Google</button>
    `;
    el("navGoogleSignInBtn")?.addEventListener("click", () => startGoogleLogin(true));
    return;
  }

  const isGoogle = currentUser.provider === "google";
  accountModeBadge.textContent = isGoogle ? "Google Account" : "Guest Session";
  lobbySignedOut.classList.add("hidden");
  lobbySignedIn.classList.remove("hidden");

  profileAvatarWrap.innerHTML = avatarHtml(currentUser.name, currentUser.avatar_url, "avatar-lg");
  profileNameText.textContent = currentUser.name;
  profileEmailText.textContent = isGoogle ? currentUser.email : "Temporary Guest Session";
  profileYtBadge.classList.toggle("hidden", !(isGoogle && currentUser.has_youtube_scope));
  upgradeGoogleBtn.classList.toggle("hidden", isGoogle);
  connectYtScopeBtn.classList.toggle(
    "hidden",
    !(isGoogle && !currentUser.has_youtube_scope && authConfig.youtube_scope_enabled)
  );

  navAccountArea.innerHTML = `
    <div class="account-chip">
      ${avatarHtml(currentUser.name, currentUser.avatar_url, "avatar-sm")}
      <span>${escapeHtml(currentUser.name)}</span>
    </div>
    ${
      !isGoogle
        ? `<button id="navGoogleSignInBtn" class="btn btn-outline btn-xs">Google Sign-In</button>`
        : ""
    }
    <button id="navLogoutBtn" class="btn btn-ghost btn-xs">Sign Out</button>
  `;
  el("navGoogleSignInBtn")?.addEventListener("click", () => startGoogleLogin(true));
  el("navLogoutBtn")?.addEventListener("click", logoutUser);
}

async function logoutUser() {
  await fetch("/api/auth/logout", { method: "POST" });
  currentUser = null;
  renderAccountUI();
  if (activeRoomCode) {
    navigateToLobby();
  } else {
    refreshRoomsDirectory();
  }
}

lobbyGoogleLoginBtn.addEventListener("click", () => {
  startGoogleLogin(includeYoutubeScopeCheck.checked);
});
upgradeGoogleBtn.addEventListener("click", () => startGoogleLogin(true));
connectYtScopeBtn.addEventListener("click", () => startGoogleLogin(true));
lobbyLogoutBtn.addEventListener("click", logoutUser);

guestLoginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = guestNameInput.value.trim();
  if (!name) return;
  const resp = await fetch("/api/auth/guest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const data = await resp.json();
  currentUser = data.user;
  localStorage.setItem("soundmesh_speaker_name", `${currentUser.name}'s ${defaultDeviceLabel(deviceType)}`);
  renderAccountUI();
  refreshRoomsDirectory();
  showToast(`Signed in as ${currentUser.name}`);
});

// ------------------------------------------------------------------
// Lobby & Multi-Room Routing
// ------------------------------------------------------------------

async function ensureSignedInForLobbyAction() {
  if (currentUser) return true;
  const typedName = guestNameInput.value.trim() || "Music Host";
  const resp = await fetch("/api/auth/guest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: typedName }),
  });
  const data = await resp.json();
  currentUser = data.user;
  renderAccountUI();
  return true;
}

async function refreshRoomsDirectory() {
  try {
    const resp = await fetch("/api/rooms");
    if (!resp.ok) return;
    lobbyRoomsData = await resp.json();
    renderRoomsDirectory();
  } catch {
    // Ignore transient fetch error
  }
}

function renderRoomsDirectory() {
  const list =
    lobbyTab === "mine" ? lobbyRoomsData.my_rooms || [] : lobbyRoomsData.public_rooms || [];
  if (list.length === 0) {
    roomsDirectoryGrid.innerHTML = `<div class="empty-state compact"><p>${
      lobbyTab === "mine"
        ? "You haven't created any rooms yet. Create one above!"
        : "No public rooms live right now. Create one to start playing!"
    }</p></div>`;
    return;
  }

  roomsDirectoryGrid.innerHTML = list
    .map(
      (r) => `
      <div class="room-dir-card">
        <div class="room-dir-top">
          <div>
            <div class="room-dir-title">${escapeHtml(r.name)} ${r.has_passcode ? "🔒" : ""}</div>
            <span class="mono-sm">#${escapeHtml(r.code)}</span>
          </div>
          <span class="badge-subtle">${r.speaker_count} speaker${r.speaker_count === 1 ? "" : "s"}</span>
        </div>
        <div class="room-dir-meta">
          <span>👑 Leader: <strong>${escapeHtml(r.leader_name)}</strong></span>
          <span>${
            r.now_playing_title
              ? `🎵 ${escapeHtml(r.now_playing_title)} — ${escapeHtml(r.now_playing_artist)}`
              : "Idle • Ready for music"
          }</span>
        </div>
        <div class="room-dir-actions">
          <button class="btn btn-primary btn-xs" data-join-code="${escapeHtml(r.code)}" data-has-pass="${r.has_passcode ? "1" : "0"}">Join Room →</button>
          ${
            currentUser && r.owner_user_id === currentUser.id
              ? `<button class="btn btn-ghost btn-xs" data-delete-code="${escapeHtml(r.code)}">Delete</button>`
              : ""
          }
        </div>
      </div>`
    )
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
    const code = joinBtn.dataset.joinCode;
    const hasPass = joinBtn.dataset.hasPass === "1";
    await attemptEnterRoom(code, "", hasPass);
    return;
  }
  const delBtn = e.target.closest("[data-delete-code]");
  if (delBtn) {
    const code = delBtn.dataset.deleteCode;
    const resp = await fetch(`/api/rooms/${encodeURIComponent(code)}`, { method: "DELETE" });
    if (resp.ok) {
      showToast(`Deleted room #${code}`);
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
  const resp = await fetch("/api/rooms", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    showToast("Could not create room.");
    return;
  }
  const data = await resp.json();
  await enterRoom(data.room.code, payload.passcode, true);
});

joinCodeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = joinCodeInput.value.trim().toUpperCase();
  const pass = joinPasscodeInput.value.trim();
  if (!code) return;
  await attemptEnterRoom(code, pass, false);
});

async function attemptEnterRoom(code, passcode = "", forcePromptIfProtected = false) {
  const cleanCode = code.trim().toUpperCase();
  const resp = await fetch(`/api/rooms/${encodeURIComponent(cleanCode)}`);
  if (!resp.ok) {
    showToast(`Room #${cleanCode} not found.`);
    return;
  }
  const data = await resp.json();
  const roomSummary = data.room;
  const isOwner = currentUser && roomSummary.owner_user_id === currentUser.id;
  if (roomSummary.has_passcode && !isOwner) {
    if (passcode) {
      const vResp = await fetch(`/api/rooms/${encodeURIComponent(cleanCode)}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode }),
      });
      const vData = await vResp.json();
      if (!vData.ok) {
        openPasscodeModal(cleanCode);
        return;
      }
    } else if (forcePromptIfProtected || !passcode) {
      openPasscodeModal(cleanCode);
      return;
    }
  }
  await enterRoom(cleanCode, passcode, true);
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
  const vResp = await fetch(`/api/rooms/${encodeURIComponent(code)}/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode: pass }),
  });
  const vData = await vResp.json();
  if (!vData.ok) {
    passcodeModalError.classList.remove("hidden");
    return;
  }
  passcodeModal.classList.add("hidden");
  await enterRoom(code, pass, true);
});

function enterRoom(roomCode, passcode = "", pushHistory = true) {
  const cleanCode = roomCode.trim().toUpperCase();
  activeRoomCode = cleanCode;
  activeRoomPasscode = passcode;
  roomState = null;
  lastRenderedSpeakerSignature = "";

  if (pushHistory && location.pathname !== `/room/${cleanCode}`) {
    history.pushState({ roomCode: cleanCode }, "", `/room/${cleanCode}`);
  }

  lobbyView.classList.add("hidden");
  roomView.classList.remove("hidden");
  roomHeaderCluster.classList.remove("hidden");
  roomTopControls.classList.remove("hidden");
  document.body.classList.add("in-room");

  const suggestedName =
    localStorage.getItem("soundmesh_speaker_name") ||
    (currentUser ? `${currentUser.name}'s ${defaultDeviceLabel(deviceType)}` : `${defaultDeviceLabel(deviceType)} Speaker`);
  unlockNameInput.value = suggestedName;
  unlockRoleSelect.value = savedRole;
  unlockRoomBadge.textContent = `ROOM #${cleanCode}`;
  if (!audioEngine.unlocked) {
    unlockOverlay.classList.remove("hidden");
  }

  wsClient.connect(cleanCode, {
    client_id: clientId,
    name: suggestedName,
    device_type: deviceType,
    role: savedRole,
    offset_ms: String(savedOffsetMs),
    passcode: activeRoomPasscode,
  });

  setMobileTab(deviceType !== "desktop" ? "speaker" : "stage");
}

function navigateToLobby(pushHistory = true) {
  wsClient.close();
  activeRoomCode = null;
  activeRoomPasscode = "";
  roomState = null;
  connectionBanner.classList.add("hidden");
  document.body.classList.remove("in-room");

  roomView.classList.add("hidden");
  roomHeaderCluster.classList.add("hidden");
  roomTopControls.classList.add("hidden");
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
globalThis.addEventListener("popstate", () => {
  handleInitialRoute(false);
});

// ------------------------------------------------------------------
// Audio Unlock Overlay
// ------------------------------------------------------------------

function completeUnlock(controllerOnly = false) {
  const chosenName = unlockNameInput.value.trim() || `${defaultDeviceLabel(deviceType)} Speaker`;
  const chosenRole = unlockRoleSelect.value || "stereo";
  localStorage.setItem("soundmesh_speaker_name", chosenName);
  localStorage.setItem("soundmesh_role", chosenRole);

  audioEngine.unlock();
  audioEngine.setRole(chosenRole);
  if (controllerOnly) {
    audioEngine.setVolumes({ speakerMuted: true });
  }

  unlockOverlay.classList.add("hidden");

  wsClient.send({
    type: "update_speaker",
    target_id: clientId,
    fields: {
      name: chosenName,
      role: chosenRole,
      muted: controllerOnly,
      audio_unlocked: true,
    },
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
// WebSocket Room Messages
// ------------------------------------------------------------------

function handleServerMessage(msg) {
  switch (msg.type) {
    case "welcome":
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
      }
      break;
    case "speakers_update":
      if (roomState) {
        roomState.speakers = msg.speakers || [];
        syncSelfFromRoom();
        renderSpeakersUI();
        renderLeaderBanner();
      }
      break;
    case "identify_speaker":
      audioEngine.playIdentifyChime();
      break;
    case "leader_changed":
      if (msg.reason === "auto_handoff") {
        showToast(`👑 ${msg.leader_name} automatically became Room Leader`);
      } else {
        showToast(`👑 ${msg.leader_name} is now the Room Leader`);
      }
      break;
    case "leader_request":
      if (msg.request) {
        showToast(`✋ ${msg.request.user_name} requested to become Room Leader`);
      }
      break;
    case "leader_request_declined":
      showToast(`Leader request was declined by ${msg.by || "Leader"}`);
      break;
    case "room_closed":
      showToast("This room was closed by its owner.");
      navigateToLobby();
      break;
    default:
      break;
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

  renderTopBar();
  renderLeaderBanner();
  renderLeaderRequests();
  renderPlaybackUI();
  renderQueueUI();
  renderSpeakersUI();
  renderPermissionsUI();
}

function syncSelfFromRoom() {
  if (!roomState || !Array.isArray(roomState.speakers)) return;
  const me = roomState.speakers.find((s) => s.id === clientId);
  if (!me) return;
  myIsLeader = Boolean(me.is_leader);
  myIsOwner = Boolean(me.is_owner);
  audioEngine.setRole(me.role);
  audioEngine.setVolumes({
    speakerVolume: me.volume,
    speakerMuted: me.muted,
  });
  if (Math.abs(audioEngine.offsetMs - me.offset_ms) > 0.05) {
    audioEngine.setOffsetMs(me.offset_ms);
    localStorage.setItem("soundmesh_offset_ms", String(me.offset_ms));
  }
  localOffsetStat.textContent = `${Math.round(me.offset_ms)} ms`;
  muteMeControllerToggle.checked = Boolean(me.muted);
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
  headerSpeakerCount.textContent = `${count} Speaker${count === 1 ? "" : "s"}`;

  const calActive = Boolean(roomState.calibration?.active);
  calibrationBanner.classList.toggle("hidden", !calActive);
  calibrateToggleBtn.classList.toggle("active", calActive);

  guestControlToggle.checked = Boolean(roomState.allow_guest_control);
  guestControlWrap.classList.toggle("hidden", !canAssignLeader());

  const volPct = Math.round((roomState.master_volume ?? 1) * 100);
  masterVolumeSlider.value = String(volPct);
  masterVolumeLabel.textContent = `${volPct}%`;
  masterMuteBtn.textContent = roomState.master_muted ? "🔇" : "🔊";
}

function renderLeaderBanner() {
  if (!roomState) return;
  const leader = (roomState.speakers || []).find((s) => s.id === roomState.leader_speaker_id);
  if (leader) {
    leaderBannerName.textContent = leader.user_name || leader.name;
    leaderBannerDevice.textContent = `(${leader.name})`;
  } else {
    leaderBannerName.textContent = "Waiting for Leader…";
    leaderBannerDevice.textContent = "";
  }

  const alreadyRequested = (roomState.pending_leader_requests || []).some(
    (r) => r.speaker_id === clientId
  );

  requestLeaderBtn.classList.toggle("hidden", myIsLeader || myIsOwner);
  requestLeaderBtn.disabled = alreadyRequested;
  requestLeaderBtn.textContent = alreadyRequested ? "⏳ Requested…" : "✋ Request Leader";

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
        <span>✋ <strong>${escapeHtml(r.user_name)}</strong> (${escapeHtml(r.name)}) asks to lead</span>
        <div class="leader-req-actions">
          <button class="btn btn-primary btn-xs" data-approve-req="${escapeHtml(r.speaker_id)}">👑 Approve</button>
          <button class="btn btn-ghost btn-xs" data-decline-req="${escapeHtml(r.speaker_id)}">Decline</button>
        </div>
      </div>`
    )
    .join("");
}

function renderPermissionsUI() {
  const allowed = canControlRoom();
  const canSearchOrQueue = canQueueTracks();
  const controls = [
    playPauseBtn,
    prevBtn,
    nextBtn,
    shuffleBtn,
    repeatBtn,
    seekSlider,
    clearQueueBtn,
    autoStereoBtn,
    calibrateToggleBtn,
    masterVolumeSlider,
    masterMuteBtn,
    resyncAllBtn,
    loadRadioBtn,
  ];
  for (const c of controls) {
    if (c) c.disabled = !allowed;
  }
  if (searchSubmitBtn) searchSubmitBtn.disabled = !canSearchOrQueue;
  if (mobileSearchNav) mobileSearchNav.style.display = canSearchOrQueue ? "flex" : "none";
  if (mobileQueueNav) mobileQueueNav.style.display = allowed ? "flex" : "none";
}

function renderPlaybackUI() {
  const pb = roomState?.playback;
  if (!pb) return;

  const track = pb.track;
  if (track) {
    nowTitle.textContent = track.title;
    nowArtist.textContent = track.album ? `${track.artist} • ${track.album}` : track.artist;
    if (track.thumbnail) {
      nowArtwork.src = track.thumbnail;
      nowArtwork.classList.remove("hidden");
      nowArtworkPlaceholder.classList.add("hidden");
      stageBackdrop.style.backgroundImage = `url("${track.thumbnail}")`;
    } else {
      nowArtwork.classList.add("hidden");
      nowArtworkPlaceholder.classList.remove("hidden");
      stageBackdrop.style.backgroundImage = "none";
    }
  } else {
    nowTitle.textContent = "No Track Playing";
    nowArtist.textContent = myIsLeader
      ? "Search YouTube Music or open ★ My Library to start playing"
      : "Synced to Room Leader — waiting for next track";
    nowArtwork.classList.add("hidden");
    nowArtworkPlaceholder.classList.remove("hidden");
    stageBackdrop.style.backgroundImage = "none";
  }

  loadingOverlay.classList.toggle("hidden", pb.status !== "loading");
  loadingPhaseText.textContent =
    pb.loading_phase === "buffering" ? "Syncing speakers…" : "Downloading audio…";

  if (pb.loading_error) {
    nowError.textContent = pb.loading_error;
    nowError.classList.remove("hidden");
  } else {
    nowError.classList.add("hidden");
  }

  playPauseBtn.textContent = pb.status === "playing" ? "⏸" : "▶";
  shuffleBtn.classList.toggle("active", Boolean(roomState.shuffle));
  repeatBtn.classList.toggle("active", roomState.repeat !== "off");
  repeatBtn.textContent = roomState.repeat === "one" ? "↻¹" : "↻";

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

  if (q.length === 0) {
    queueList.innerHTML = `<div class="empty-state compact"><p>Queue is empty. Add songs to build a synchronized playlist.</p></div>`;
    return;
  }

  const allowed = canControlRoom();
  queueList.innerHTML = q
    .map(
      (t, idx) => `
      <div class="track-row ${idx === curIdx ? "active-track" : ""}" data-q-idx="${idx}">
        <img class="track-thumb" src="${escapeHtml(t.thumbnail)}" alt="" loading="lazy" />
        <div class="track-info" data-action="play-idx" data-idx="${idx}" style="cursor:pointer">
          <div class="track-title">${idx === curIdx ? "▶ " : ""}${escapeHtml(t.title)}</div>
          <div class="track-sub">${escapeHtml(t.artist)} • ${formatTime(t.duration_sec)} ${
            t.cached ? '<span class="cached-tag">Cached</span>' : ""
          }</div>
        </div>
        ${
          allowed
            ? `<div class="track-actions">
                <button class="icon-btn" data-action="up" data-idx="${idx}" title="Move up">↑</button>
                <button class="icon-btn" data-action="down" data-idx="${idx}" title="Move down">↓</button>
                <button class="icon-btn" data-action="remove" data-idx="${idx}" title="Remove">✕</button>
              </div>`
            : ""
        }
      </div>`
    )
    .join("");
}

function formatSyncStatus(status) {
  switch (status) {
    case "locked":
      return "Synced";
    case "syncing":
      return "Aligning…";
    case "buffering":
      return "Buffering…";
    case "idle":
      return "Ready";
    default:
      return "Tap to Unlock";
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
    speakers.map((s) => `${s.id}:${s.is_leader}:${s.is_owner}`).join("|") +
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
      const card = speakerList.querySelector(`.speaker-card[data-speaker-id="${s.id}"]`);
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
        tel.textContent = `RTT ${Math.round(s.rtt_ms)}ms • Drift ${s.drift_ms >= 0 ? "+" : ""}${s.drift_ms.toFixed(1)}ms`;
      }
      const nameInput = card.querySelector("[data-field='name']");
      if (nameInput && document.activeElement !== nameInput) nameInput.value = s.name;
      const roleSelect = card.querySelector("[data-field='role']");
      if (roleSelect && document.activeElement !== roleSelect) roleSelect.value = s.role;
      const volSlider = card.querySelector("[data-field='volume']");
      if (volSlider && document.activeElement !== volSlider) {
        volSlider.value = String(Math.round(s.volume * 100));
      }
      const muteBtn = card.querySelector("[data-action='toggle-mute']");
      if (muteBtn) muteBtn.textContent = s.muted ? "🔇" : "🔊";
      const offSlider = card.querySelector("[data-field='offset_ms']");
      if (offSlider && document.activeElement !== offSlider) {
        offSlider.value = String(Math.round(s.offset_ms));
      }
      const offLabel = card.querySelector("[data-role='offset-label']");
      if (offLabel) offLabel.textContent = `${Math.round(s.offset_ms)}ms`;
    }
  }

  headerSpeakerCount.textContent = `${speakers.length} Speaker${speakers.length === 1 ? "" : "s"}`;
}

function buildSpeakerCardHtml(s) {
  const isSelf = s.id === clientId;
  const editable = isSelf || canControlRoom();
  const disabledAttr = editable ? "" : "disabled";
  const showMakeLeader = canAssignLeader() && !s.is_leader;

  return `
    <div class="speaker-card ${isSelf ? "is-self" : ""} ${s.is_leader ? "is-leader-card" : ""}" data-speaker-id="${escapeHtml(s.id)}">
      <div class="speaker-top">
        <div class="speaker-id-group">
          ${avatarHtml(s.user_name || s.name, s.user_avatar, "avatar-sm")}
          <div class="speaker-name-wrap">
            <input class="speaker-name-input" type="text" data-field="name" value="${escapeHtml(s.name)}" ${disabledAttr} />
            <span class="speaker-user-sub">${escapeHtml(s.user_name || "Guest")}${s.auth_provider === "google" ? " • Google" : ""}</span>
          </div>
        </div>
        <div class="speaker-badges-row">
          ${s.is_leader ? '<span class="role-pill leader">👑 Leader</span>' : ""}
          ${s.is_owner ? '<span class="role-pill owner">Owner</span>' : ""}
          ${isSelf ? '<span class="role-pill">You</span>' : ""}
          <span class="sync-badge ${s.sync_status}" data-role="sync-badge">${formatSyncStatus(s.sync_status)}</span>
        </div>
      </div>

      <div class="speaker-telemetry">
        <span data-role="telemetry">RTT ${Math.round(s.rtt_ms)}ms • Drift ${s.drift_ms >= 0 ? "+" : ""}${s.drift_ms.toFixed(1)}ms</span>
        <div class="speaker-actions-inline">
          ${
            showMakeLeader
              ? `<button class="btn btn-accent btn-xs" data-action="make-leader" title="Hand playback control to this device">👑 Make Leader</button>`
              : ""
          }
          <button class="btn btn-ghost btn-xs" data-action="ping" title="Identify this speaker">🔔 Ping</button>
        </div>
      </div>

      <div class="speaker-controls">
        <select data-field="role" ${disabledAttr} title="Speaker Channel">
          <option value="stereo" ${s.role === "stereo" ? "selected" : ""}>Stereo</option>
          <option value="left" ${s.role === "left" ? "selected" : ""}>Left (L)</option>
          <option value="right" ${s.role === "right" ? "selected" : ""}>Right (R)</option>
          <option value="mono" ${s.role === "mono" ? "selected" : ""}>Mono</option>
          <option value="subwoofer" ${s.role === "subwoofer" ? "selected" : ""}>Sub</option>
        </select>
        <button class="icon-btn" data-action="toggle-mute" ${disabledAttr} title="Mute speaker">${s.muted ? "🔇" : "🔊"}</button>
        <input type="range" min="0" max="100" value="${Math.round(s.volume * 100)}" data-field="volume" ${disabledAttr} aria-label="Speaker volume" />
      </div>

      <div class="speaker-offset-row">
        <span>Sync:</span>
        <button class="nudge-btn" data-action="nudge" data-delta="-5" ${disabledAttr}>−5</button>
        <input type="range" min="-500" max="500" value="${Math.round(s.offset_ms)}" data-field="offset_ms" ${disabledAttr} aria-label="Sync offset ms" />
        <button class="nudge-btn" data-action="nudge" data-delta="5" ${disabledAttr}>+5</button>
        <span class="mono-sm" data-role="offset-label">${Math.round(s.offset_ms)}ms</span>
      </div>
    </div>
  `;
}

// ------------------------------------------------------------------
// YouTube Music Search & Personal Library
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
        <div class="empty-icon">★</div>
        <p>Sign in with a Google account (with YouTube permission) to browse your personal YouTube Music playlists and Liked Music.</p>
        <button id="inlineConnectYtBtn" class="btn btn-primary btn-sm">Sign in with Google for Library</button>
      </div>
    `;
    el("inlineConnectYtBtn")?.addEventListener("click", () => startGoogleLogin(true));
    return;
  }

  searchStatus.textContent = "Loading your YouTube playlists…";
  searchStatus.classList.remove("hidden");
  try {
    const resp = await fetch("/api/me/playlists");
    const data = await resp.json();
    searchStatus.classList.add("hidden");
    if (!resp.ok) throw new Error(data.detail || "Unable to load playlists");
    currentLibraryPlaylists = data.playlists || [];
    if (currentLibraryPlaylists.length === 0) {
      searchResults.innerHTML = `<div class="empty-state compact"><p>No YouTube playlists found on this Google account.</p></div>`;
      return;
    }
    searchResults.innerHTML = currentLibraryPlaylists
      .map(
        (c, idx) => `
        <div class="track-row" data-lib-idx="${idx}" style="cursor:pointer">
          <img class="track-thumb" src="${escapeHtml(c.thumbnail)}" alt="" loading="lazy" />
          <div class="track-info">
            <div class="track-title">${escapeHtml(c.title)}</div>
            <div class="track-sub">${escapeHtml(c.subtitle)} ${c.item_count ? `• ${escapeHtml(c.item_count)} items` : ""}</div>
          </div>
          <div class="track-actions">
            <button class="btn btn-outline btn-xs">Open →</button>
          </div>
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
  searchStatus.textContent = "Searching YouTube Music…";
  searchStatus.classList.remove("hidden");

  try {
    const resp = await fetch(`/api/search?q=${encodeURIComponent(q)}&filter=${encodeURIComponent(filter)}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Search failed");

    searchStatus.classList.add("hidden");
    currentSearchTracks = data.tracks || [];
    currentSearchCollections = data.collections || [];

    if (data.resolved_url && data.playlist_title && currentSearchTracks.length > 0) {
      showCollectionView(data.playlist_title, "Resolved YouTube URL", currentSearchTracks);
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
          <img class="track-thumb" src="${escapeHtml(c.thumbnail)}" alt="" loading="lazy" />
          <div class="track-info">
            <div class="track-title">${escapeHtml(c.title)}</div>
            <div class="track-sub">${escapeHtml(c.subtitle)} ${c.year ? `• ${escapeHtml(c.year)}` : ""}</div>
          </div>
          <div class="track-actions">
            <button class="btn btn-outline btn-xs">Open →</button>
          </div>
        </div>`
      )
      .join("");
    return;
  }

  if (currentSearchTracks.length === 0) {
    searchResults.innerHTML = `<div class="empty-state compact"><p>No matching tracks found. Try another search or paste a YouTube Music URL.</p></div>`;
    return;
  }

  const canPlay = canControlRoom();
  searchResults.innerHTML = currentSearchTracks
    .map(
      (t, idx) => `
      <div class="track-row" data-track-idx="${idx}">
        <img class="track-thumb" src="${escapeHtml(t.thumbnail)}" alt="" loading="lazy" />
        <div class="track-info">
          <div class="track-title">${escapeHtml(t.title)}</div>
          <div class="track-sub">${escapeHtml(t.artist)} • ${formatTime(t.duration_sec)} ${
            t.cached ? '<span class="cached-tag">Cached</span>' : ""
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
  collectionSubtitle.textContent = `${subtitle} • ${currentCollectionTracks.length} tracks`;
  collectionHeader.classList.remove("hidden");

  const canPlay = canControlRoom();
  searchResults.innerHTML = currentCollectionTracks
    .map(
      (t, idx) => `
      <div class="track-row" data-track-idx="${idx}">
        <img class="track-thumb" src="${escapeHtml(t.thumbnail)}" alt="" loading="lazy" />
        <div class="track-info">
          <div class="track-title">${escapeHtml(t.title)}</div>
          <div class="track-sub">${escapeHtml(t.artist)} • ${formatTime(t.duration_sec)}</div>
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
    let endpoint;
    if (isPersonalLibrary) {
      endpoint = `/api/me/playlists/${encodeURIComponent(col.id)}/tracks`;
    } else if (col.kind === "album") {
      endpoint = `/api/album/${encodeURIComponent(col.id)}`;
    } else {
      endpoint = `/api/playlist/${encodeURIComponent(col.id)}`;
    }
    const resp = await fetch(endpoint);
    const data = await resp.json();
    searchStatus.classList.add("hidden");
    showCollectionView(col.title || data.title, col.subtitle || data.subtitle, data.tracks || []);
  } catch (err) {
    searchStatus.textContent = `Failed loading ${col.title}: ${err.message}`;
  }
}

// ------------------------------------------------------------------
// Studio Event Listeners
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
  if (currentFilter === "library") {
    loadPersonalLibrary();
  } else if (searchInput.value.trim()) {
    runSearch(searchInput.value, currentFilter);
  }
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
  if (currentFilter === "library") {
    loadPersonalLibrary();
  } else {
    renderSearchResults();
  }
});

collectionPlayAllBtn.addEventListener("click", () => {
  if (!currentCollectionTracks.length) return;
  wsClient.send({ type: "clear_queue" });
  wsClient.send({ type: "add_to_queue", tracks: currentCollectionTracks, play_if_idle: true });
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
  } else if (actionBtn.dataset.action === "queue-add") {
    wsClient.send({ type: "add_to_queue", tracks: [track], play_if_idle: true });
    showToast(`Added "${track.title}" to queue`);
  }
});

// Transport & Queue
playPauseBtn.addEventListener("click", () => {
  if (!roomState) return;
  wsClient.send({ type: roomState.playback.status === "playing" ? "pause" : "resume" });
});
prevBtn.addEventListener("click", () => wsClient.send({ type: "prev" }));
nextBtn.addEventListener("click", () => wsClient.send({ type: "next" }));
resyncAllBtn.addEventListener("click", () => wsClient.send({ type: "force_resync" }));
shuffleBtn.addEventListener("click", () => wsClient.send({ type: "set_shuffle", enabled: !roomState?.shuffle }));
repeatBtn.addEventListener("click", () => {
  const order = ["off", "all", "one"];
  const nextMode = order[(order.indexOf(roomState?.repeat || "off") + 1) % order.length];
  wsClient.send({ type: "set_repeat", mode: nextMode });
});

seekSlider.addEventListener("input", () => {
  isSeeking = true;
  timeCurrent.textContent = formatTime(Number(seekSlider.value));
});
seekSlider.addEventListener("change", () => {
  isSeeking = false;
  wsClient.send({ type: "seek", position_sec: Number(seekSlider.value) });
});

loadRadioBtn.addEventListener("click", async () => {
  const vid = roomState?.playback?.track?.video_id;
  if (!vid) return;
  loadRadioBtn.disabled = true;
  try {
    const resp = await fetch(`/api/related/${encodeURIComponent(vid)}`);
    const data = await resp.json();
    if (Array.isArray(data.tracks) && data.tracks.length > 0) {
      wsClient.send({ type: "add_to_queue", tracks: data.tracks, play_if_idle: false });
      showToast(`Added ${data.tracks.length} YT Radio tracks`);
    }
  } finally {
    loadRadioBtn.disabled = !canControlRoom();
  }
});

clearQueueBtn.addEventListener("click", () => wsClient.send({ type: "clear_queue" }));

queueList.addEventListener("click", (e) => {
  const target = e.target.closest("[data-action]");
  if (!target) return;
  const idx = Number(target.dataset.idx);
  const action = target.dataset.action;
  if (action === "play-idx") wsClient.send({ type: "play_index", index: idx });
  else if (action === "up" && idx > 0) wsClient.send({ type: "move_queue", from_index: idx, to_index: idx - 1 });
  else if (action === "down" && roomState && idx < roomState.queue.length - 1) {
    wsClient.send({ type: "move_queue", from_index: idx, to_index: idx + 1 });
  } else if (action === "remove") wsClient.send({ type: "remove_queue", index: idx });
});

// Topbar & Leader Actions
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
  wsClient.send({
    type: "update_speaker",
    target_id: clientId,
    fields: { muted: muteMeControllerToggle.checked },
  });
});

calibrateToggleBtn.addEventListener("click", () => wsClient.send({ type: "set_calibration", active: !roomState?.calibration?.active }));
stopCalibrationBtn.addEventListener("click", () => wsClient.send({ type: "set_calibration", active: false }));
autoStereoBtn.addEventListener("click", () => wsClient.send({ type: "auto_assign_stereo" }));
pingAllBtn.addEventListener("click", () => wsClient.send({ type: "identify_speaker", target_id: "all" }));

requestLeaderBtn.addEventListener("click", () => {
  wsClient.send({ type: "request_leader" });
  showToast("Sent leadership request to Room Leader");
});
takeLeaderBtn.addEventListener("click", () => {
  wsClient.send({ type: "assign_leader", target_id: clientId });
});

leaderRequestsDrawer.addEventListener("click", (e) => {
  const appBtn = e.target.closest("[data-approve-req]");
  if (appBtn) {
    wsClient.send({ type: "respond_leader_request", requester_id: appBtn.dataset.approveReq, approve: true });
    return;
  }
  const decBtn = e.target.closest("[data-decline-req]");
  if (decBtn) {
    wsClient.send({ type: "respond_leader_request", requester_id: decBtn.dataset.declineReq, approve: false });
  }
});

// Local Nudge Buttons
document.querySelectorAll(".nudge-btn[data-nudge]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const raw = btn.dataset.nudge;
    const nextOffset =
      raw === "reset"
        ? 0
        : Math.max(-500, Math.min(500, Math.round(audioEngine.offsetMs + Number(raw))));
    audioEngine.setOffsetMs(nextOffset);
    localStorage.setItem("soundmesh_offset_ms", String(nextOffset));
    localOffsetStat.textContent = `${nextOffset} ms`;
    wsClient.send({
      type: "update_speaker",
      target_id: clientId,
      fields: { offset_ms: nextOffset },
    });
  });
});

// Speaker List Delegated Events
speakerList.addEventListener("change", (e) => {
  const card = e.target.closest(".speaker-card");
  if (!card) return;
  const targetId = card.dataset.speakerId;
  const field = e.target.dataset.field;
  if (!field) return;

  let value = e.target.value;
  if (field === "volume") value = Number(value) / 100;
  if (field === "offset_ms") value = Number(value);

  if (targetId === clientId) {
    if (field === "name") localStorage.setItem("soundmesh_speaker_name", value);
    if (field === "role") {
      localStorage.setItem("soundmesh_role", value);
      audioEngine.setRole(value);
    }
    if (field === "offset_ms") {
      localStorage.setItem("soundmesh_offset_ms", String(value));
      audioEngine.setOffsetMs(value);
      localOffsetStat.textContent = `${Math.round(value)} ms`;
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
    if (targetId === clientId) {
      audioEngine.setOffsetMs(val);
      localOffsetStat.textContent = `${Math.round(val)} ms`;
    }
  } else if (field === "volume" && targetId === clientId) {
    audioEngine.setVolumes({ speakerVolume: Number(e.target.value) / 100 });
  }
});

speakerList.addEventListener("click", (e) => {
  const card = e.target.closest(".speaker-card");
  const btn = e.target.closest("[data-action]");
  if (!card || !btn) return;
  const targetId = card.dataset.speakerId;
  const action = btn.dataset.action;

  if (action === "make-leader") {
    wsClient.send({ type: "assign_leader", target_id: targetId });
  } else if (action === "ping") {
    wsClient.send({ type: "identify_speaker", target_id: targetId });
  } else if (action === "toggle-mute") {
    const sp = roomState?.speakers?.find((s) => s.id === targetId);
    wsClient.send({ type: "update_speaker", target_id: targetId, fields: { muted: !(sp?.muted ?? false) } });
  } else if (action === "nudge") {
    const sp = roomState?.speakers?.find((s) => s.id === targetId);
    const delta = Number(btn.dataset.delta || 0);
    const nextOffset = Math.max(-500, Math.min(500, Math.round((sp?.offset_ms || 0) + delta)));
    if (targetId === clientId) {
      audioEngine.setOffsetMs(nextOffset);
      localStorage.setItem("soundmesh_offset_ms", String(nextOffset));
      localOffsetStat.textContent = `${nextOffset} ms`;
    }
    wsClient.send({ type: "update_speaker", target_id: targetId, fields: { offset_ms: nextOffset } });
  }
});

// QR & Setup Modals
async function openQrModal() {
  const code = activeRoomCode || "STUDIO";
  qrRoomCodeText.textContent = `#${code}`;
  qrModal.classList.remove("hidden");
  try {
    const resp = await fetch(`/api/rooms/${encodeURIComponent(code)}/info`);
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
  qrImage.src = `/api/qr?url=${encodeURIComponent(url)}`;
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
    if (navigator.clipboard && globalThis.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      qrUrlInput.select();
      document.execCommand("copy");
    }
    copyUrlBtn.textContent = "Copied!";
    setTimeout(() => {
      copyUrlBtn.textContent = "Copy Link";
    }, 1400);
  } catch {
    qrUrlInput.select();
  }
});

// Mobile Navigation
function setMobileTab(tabName) {
  mobileNav.querySelectorAll(".mobile-nav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.target === tabName);
  });
  document.body.classList.remove(
    "mobile-tab-speaker",
    "mobile-tab-stage",
    "mobile-tab-search",
    "mobile-tab-queue"
  );
  document.body.classList.add(`mobile-tab-${tabName}`);
  document.querySelectorAll(".workspace > .panel").forEach((p) => {
    const match =
      p.dataset.tab === tabName || (tabName === "queue" && p.dataset.tab === "stage");
    p.classList.toggle("mobile-active", match);
  });
}

mobileNav.addEventListener("click", (e) => {
  const btn = e.target.closest(".mobile-nav-btn");
  if (!btn) return;
  setMobileTab(btn.dataset.target || "speaker");
});

// ------------------------------------------------------------------
// 60fps Canvas Visualizer & Scrubber Loop
// ------------------------------------------------------------------

const freqBins = new Uint8Array(64);
const vizCtx = visualizerCanvas.getContext("2d");

function animationLoop() {
  const pb = roomState?.playback;
  if (pb && !isSeeking && activeRoomCode) {
    const pos = audioEngine.getExpectedPositionSec();
    const dur = pb.audio_duration_sec || pb.track?.duration_sec || 0;
    const clampedPos = dur > 0 ? Math.min(dur, pos) : pos;
    timeCurrent.textContent = formatTime(clampedPos);
    seekSlider.value = String(clampedPos);
  }

  if (vizCtx && activeRoomCode) {
    const w = visualizerCanvas.width;
    const h = visualizerCanvas.height;
    vizCtx.clearRect(0, 0, w, h);

    const hasSignal = audioEngine.getFrequencyData(freqBins);
    const barCount = 36;
    const gap = 3;
    const barWidth = (w - (barCount - 1) * gap) / barCount;

    for (let i = 0; i < barCount; i++) {
      const raw = hasSignal ? freqBins[i] : 6;
      const norm = Math.max(0.06, raw / 255);
      const barH = Math.max(3, norm * (h - 6));
      const x = i * (barWidth + gap);
      const y = h - barH;
      vizCtx.fillStyle = hasSignal && raw > 10 ? "rgba(34, 211, 238, 0.85)" : "rgba(148, 163, 184, 0.2)";
      vizCtx.beginPath();
      vizCtx.roundRect(x, y, barWidth, barH, 2);
      vizCtx.fill();
    }
  }

  requestAnimationFrame(animationLoop);
}

// ------------------------------------------------------------------
// Bootstrap
// ------------------------------------------------------------------

async function handleInitialRoute(push = false) {
  const params = new URLSearchParams(location.search);
  const authErr = params.get("auth_error");
  if (authErr) {
    showToast(`Google Sign-In failed: ${authErr}`);
    history.replaceState({}, "", location.pathname);
  }

  const roomMatch = location.pathname.match(/^\/room\/([A-Za-z0-9_-]+)/);
  if (roomMatch) {
    await attemptEnterRoom(roomMatch[1], "", true);
    return;
  }
  if (location.pathname.startsWith("/join")) {
    await enterRoom("STUDIO", "", false);
    return;
  }
  navigateToLobby(push);
}

// ------------------------------------------------------------------
// Free Public HTTPS Link (Cloudflare Quick Tunnel)
// ------------------------------------------------------------------

let tunnelPollTimer = null;

async function refreshTunnelStatus() {
  try {
    const resp = await fetch("/api/tunnel/status");
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
    // Ignore transient errors
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
    startTunnelBtn.textContent = st.starting
      ? "Starting free HTTPS tunnel…"
      : "🌐 Go Live Online (Free HTTPS Link)";
  }
}

startTunnelBtn.addEventListener("click", async () => {
  startTunnelBtn.disabled = true;
  startTunnelBtn.textContent = "Starting free HTTPS tunnel…";
  try {
    const resp = await fetch("/api/tunnel/start", { method: "POST" });
    const data = await resp.json();
    renderTunnelUI(data);
    if (data.active && data.public_url) {
      showToast(`Live public site ready: ${data.public_url}`);
    } else if (data.error) {
      showToast(`Tunnel error: ${data.error}`);
    }
  } catch {
    showToast("Unable to start public tunnel.");
  } finally {
    refreshTunnelStatus();
  }
});

stopTunnelBtn.addEventListener("click", async () => {
  await fetch("/api/tunnel/stop", { method: "POST" });
  showToast("Public tunnel stopped.");
  refreshTunnelStatus();
});

copyLiveLinkBtn.addEventListener("click", async () => {
  const url = livePublicLinkAnchor.textContent || "";
  if (!url) return;
  try {
    if (navigator.clipboard && globalThis.isSecureContext) {
      await navigator.clipboard.writeText(url);
    } else {
      const tmp = document.createElement("input");
      tmp.value = url;
      document.body.appendChild(tmp);
      tmp.select();
      document.execCommand("copy");
      tmp.remove();
    }
    copyLiveLinkBtn.textContent = "Copied!";
    setTimeout(() => {
      copyLiveLinkBtn.textContent = "Copy Public Link";
    }, 1400);
  } catch {
    showToast(url);
  }
});

(async function init() {
  await refreshAuth();
  await refreshTunnelStatus();
  await handleInitialRoute(false);
  requestAnimationFrame(animationLoop);
})();

/**
 * Decoupled API / WebSocket base URL, one-time ticket exchange, and Bearer token manager.
 * Works identically whether frontend & backend share one origin (Render / localhost)
 * or frontend is hosted separately on Vercel / Capacitor / Tauri.
 */

const TOKEN_KEY = "soundmesh_token";
const BACKEND_OVERRIDE_KEY = "soundmesh_backend_url";
const DEFAULT_CLOUD_BACKEND_URL = "https://soundmesh-gqve.onrender.com";

let pendingOauthTicket = "";

// Capture #auth_token=... returned after cross-origin Google OAuth redirect
(function captureAuthHash() {
  try {
    if (location.hash && location.hash.includes("auth_token=")) {
      const params = new URLSearchParams(location.hash.slice(1));
      const tok = params.get("auth_token");
      if (tok) {
        if (tok.startsWith("otk_")) {
          pendingOauthTicket = tok;
        } else {
          localStorage.setItem(TOKEN_KEY, tok);
        }
      }
      history.replaceState({}, "", location.pathname + location.search);
    }
  } catch {
    // Ignore storage/history errors
  }
})();

export function getApiBase() {
  const fromGlobal = typeof globalThis.SOUNDMESH_BACKEND_URL === "string"
    ? globalThis.SOUNDMESH_BACKEND_URL
    : "";
  const fromStorage = localStorage.getItem(BACKEND_OVERRIDE_KEY) || "";
  const autoCloud =
    location.hostname.endsWith(".vercel.app") ? DEFAULT_CLOUD_BACKEND_URL : "";
  const raw = (fromGlobal || fromStorage || autoCloud).trim().replace(/\/+$/, "");
  return raw;
}

export function getWsBase() {
  const apiBase = getApiBase();
  if (apiBase) {
    return apiBase.replace(/^https:/i, "wss:").replace(/^http:/i, "ws:");
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}`;
}

export function apiUrl(path) {
  const base = getApiBase();
  if (!path.startsWith("/")) return `${base}/${path}`;
  return `${base}${path}`;
}

export function getSessionToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setSessionToken(token) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

export async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getSessionToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return await fetch(apiUrl(path), {
    ...options,
    headers,
    credentials: getApiBase() ? "include" : "same-origin",
  });
}

export async function exchangePendingOauthTicket() {
  if (!pendingOauthTicket) return;
  const ticket = pendingOauthTicket;
  pendingOauthTicket = "";
  try {
    const resp = await fetch(apiUrl("/api/auth/exchange"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket }),
      credentials: getApiBase() ? "include" : "same-origin",
    });
    if (resp.ok) {
      const data = await resp.json();
      if (data.session_token) {
        setSessionToken(data.session_token);
      }
    }
  } catch {
    // Ignore transient network errors
  }
}

export async function fetchWsTicket() {
  if (!getSessionToken()) return "";
  try {
    const resp = await apiFetch("/api/auth/ws-ticket", { method: "POST" });
    if (!resp.ok) return "";
    const data = await resp.json();
    return data.ws_ticket || "";
  } catch {
    return "";
  }
}

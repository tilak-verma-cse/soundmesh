import { fetchWsTicket, getWsBase } from "./env.js";

/**
 * Reconnecting WebSocket client using short-lived single-use WS tickets
 * (keeping long-lived session tokens and room passcodes out of proxy logs)
 * and direct fast-path routing for NTP sync_pong.
 */
export class WSClient {
  constructor({ onMessage, onConnectionChange, onFatalClose, clockSync }) {
    this.onMessage = onMessage;
    this.onConnectionChange = onConnectionChange;
    this.onFatalClose = onFatalClose;
    this.clockSync = clockSync;

    this.ws = null;
    this.roomCode = "STUDIO";
    this.params = null;
    this.connected = false;
    this.reconnectDelayMs = 500;
    this._reconnectTimer = null;
    this._intentionallyClosed = false;
    this._connectSeq = 0;
  }

  connect(roomCode, params) {
    this.roomCode = (roomCode || "STUDIO").trim().toUpperCase();
    this.params = params;
    this._intentionallyClosed = false;
    void this._openSocket();
  }

  _buildUrl(wsTicket = "") {
    const qs = new URLSearchParams(this.params || {});
    if (wsTicket) {
      qs.set("ticket", wsTicket);
    }
    const encodedRoom = encodeURIComponent(this.roomCode);
    return `${getWsBase()}/ws/${encodedRoom}?${qs.toString()}`;
  }

  async _openSocket() {
    const seq = ++this._connectSeq;
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }

    if (this.ws) {
      try {
        this.ws.onopen = null;
        this.ws.onmessage = null;
        this.ws.onclose = null;
        this.ws.onerror = null;
        this.ws.close();
      } catch {
        // Ignore cleanup errors
      }
      this.ws = null;
    }

    const wsTicket = await fetchWsTicket();
    if (this._intentionallyClosed || seq !== this._connectSeq) return;

    const ws = new WebSocket(this._buildUrl(wsTicket));
    this.ws = ws;

    ws.onopen = () => {
      this.connected = true;
      this.reconnectDelayMs = 500;
      if (typeof this.onConnectionChange === "function") {
        this.onConnectionChange(true);
      }
      this.clockSync.startInitialBurst();
    };

    ws.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      if (data.type === "sync_pong") {
        this.clockSync.onPong(data);
        return;
      }
      if (typeof this.onMessage === "function") {
        this.onMessage(data);
      }
    };

    ws.onclose = (event) => {
      const wasConnected = this.connected;
      this.connected = false;
      this.clockSync.stop();

      if (event.code === 4403 || event.code === 4404 || event.code === 4429) {
        this._intentionallyClosed = true;
        if (typeof this.onFatalClose === "function") {
          this.onFatalClose(event.code, event.reason || "");
        }
        return;
      }

      if (wasConnected && typeof this.onConnectionChange === "function") {
        this.onConnectionChange(false);
      }
      if (!this._intentionallyClosed) {
        this._scheduleReconnect();
      }
    };

    ws.onerror = () => {
      // onclose handles reconnect
    };
  }

  _scheduleReconnect() {
    if (this._reconnectTimer !== null) return;
    const delay = this.reconnectDelayMs;
    this.reconnectDelayMs = Math.min(4000, this.reconnectDelayMs * 1.8);
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      void this._openSocket();
    }, delay);
  }

  send(payload) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return false;
    }
    this.ws.send(JSON.stringify(payload));
    return true;
  }

  close() {
    this._intentionallyClosed = true;
    this._connectSeq++;
    this.clockSync.stop();
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.onclose = null;
        this.ws.close();
      } catch {
        // Ignore
      }
      this.ws = null;
    }
    this.connected = false;
  }
}

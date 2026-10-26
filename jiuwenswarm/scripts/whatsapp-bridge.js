#!/usr/bin/env node
/* eslint-disable no-console */

const fs = require("fs");
const path = require("path");
const { WebSocketServer } = require("ws");
const pino = require("pino");
const qrcodeTerminal = require("qrcode-terminal");

async function loadBaileys() {
  return await import("@whiskeysockets/baileys");
}

let makeWASocket = null;
let useMultiFileAuthState = null;
let DisconnectReason = {};
let fetchLatestWaWebVersion = null;
let Browsers = null;

async function initBaileys() {
  const waModule = await loadBaileys();
  makeWASocket = waModule.default || waModule.makeWASocket || null;
  useMultiFileAuthState = waModule.useMultiFileAuthState || null;
  DisconnectReason = waModule.DisconnectReason || {};
  fetchLatestWaWebVersion = waModule.fetchLatestWaWebVersion || null;
  Browsers = waModule.Browsers || null;
  if (typeof makeWASocket !== "function" || typeof useMultiFileAuthState !== "function") {
    throw new Error("Unsupported Baileys version: makeWASocket/useMultiFileAuthState missing");
  }
}

const HOST = process.env.WA_BRIDGE_HOST || "127.0.0.1";
const PORT = Number(process.env.WA_BRIDGE_PORT || "19600");
const PATHNAME = process.env.WA_BRIDGE_PATH || "/ws";
const AUTH_DIR = process.env.WA_AUTH_DIR || path.join(process.cwd(), "workspace", ".whatsapp-auth");
const PRINT_QR = (process.env.WA_PRINT_QR || "1") !== "0";

if (!fs.existsSync(AUTH_DIR)) {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
}

const clients = new Set();
let sock = null;
let reconnectTimer = null;
let connected = false;

function safeSend(ws, payload) {
  try {
    if (ws.readyState === ws.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  } catch (_) {}
}

function broadcast(payload) {
  for (const ws of clients) {
    safeSend(ws, payload);
  }
}

function nowMs() {
  return Date.now();
}

function emitStatus(state, extra = {}) {
  const payload = { type: "status", state, ts: nowMs(), ...extra };
  broadcast(payload);
}

function extractText(message) {
  if (!message || typeof message !== "object") return "";
  if (typeof message.conversation === "string") return message.conversation;
  if (message.extendedTextMessage && typeof message.extendedTextMessage.text === "string") {
    return message.extendedTextMessage.text;
  }
  if (message.imageMessage && typeof message.imageMessage.caption === "string") {
    return message.imageMessage.caption;
  }
  if (message.videoMessage && typeof message.videoMessage.caption === "string") {
    return message.videoMessage.caption;
  }
  if (message.documentMessage && typeof message.documentMessage.caption === "string") {
    return message.documentMessage.caption;
  }
  return "";
}

function scheduleReconnect(delayMs = 4000) {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connectWhatsApp();
  }, delayMs);
}

async function connectWhatsApp() {
  console.log("[whatsapp-bridge] connecting to WhatsApp...");
  emitStatus("connecting");
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  let waVersion = null;
  if (typeof fetchLatestWaWebVersion === "function") {
    try {
      const latest = await fetchLatestWaWebVersion();
      waVersion = latest?.version || null;
      if (Array.isArray(waVersion)) {
        console.log(`[whatsapp-bridge] WA web version ${waVersion.join(".")}`);
      }
    } catch (err) {
      console.log(`[whatsapp-bridge] version fetch failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  const socketOptions = {
    auth: state,
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
  };
  if (Array.isArray(waVersion) && waVersion.length >= 3) {
    socketOptions.version = waVersion;
  }
  if (Browsers && typeof Browsers.macOS === "function") {
    socketOptions.browser = Browsers.macOS("Desktop");
  }
  sock = makeWASocket(socketOptions);
  let firstUpdateSeen = false;
  setTimeout(() => {
    if (!firstUpdateSeen && !connected) {
      console.log("[whatsapp-bridge] still waiting for WhatsApp connection update...");
    }
  }, 20000);

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (update) => {
    firstUpdateSeen = true;
    const { connection, lastDisconnect, qr } = update || {};
    const statusCode = lastDisconnect?.error?.output?.statusCode;
    const reasonText = lastDisconnect?.error?.message || null;

    if (qr) {
      console.log("[whatsapp-bridge] QR received. Scan it with WhatsApp > Linked devices.");
      if (PRINT_QR) {
        qrcodeTerminal.generate(qr, { small: true });
      }
      broadcast({ type: "qr", qr, ts: nowMs() });
    }

    if (connection === "open") {
      connected = true;
      console.log("[whatsapp-bridge] connected");
      emitStatus("open");
      return;
    }

    if (connection === "close") {
      connected = false;
      console.log(`[whatsapp-bridge] closed status=${statusCode || "unknown"} reason=${reasonText || "n/a"}`);
      emitStatus("close", { status_code: statusCode || null });

      const loggedOutCode =
        DisconnectReason.loggedOut ||
        DisconnectReason.connectionClosed ||
        DisconnectReason.badSession;
      const shouldReconnect = statusCode !== loggedOutCode;
      if (shouldReconnect) {
        console.log("[whatsapp-bridge] reconnecting in 4s...");
        scheduleReconnect();
      } else {
        console.log("[whatsapp-bridge] logged out; please delete auth dir and re-link");
        emitStatus("logged_out");
      }
    }
  });

  sock.ev.on("messages.upsert", ({ messages }) => {
    if (!Array.isArray(messages)) return;
    for (const msg of messages) {
      const key = msg?.key || {};
      const remoteJid = key.remoteJid || "";
      if (!remoteJid || remoteJid === "status@broadcast") continue;
      if (key.fromMe) continue;

      const text = extractText(msg.message);
      if (!text || !text.trim()) continue;

      const payload = {
        type: "inbound",
        jid: remoteJid,
        sender: key.participant || key.remoteJid || "",
        text: text.trim(),
        message_id: key.id || "",
        push_name: msg.pushName || "",
        ts: Number(msg.messageTimestamp || 0),
      };
      broadcast(payload);
    }
  });
}

const wss = new WebSocketServer({ host: HOST, port: PORT, path: PATHNAME });
wss.on("connection", (ws) => {
  clients.add(ws);
  safeSend(ws, { type: "status", state: connected ? "open" : "connecting", ts: nowMs() });

  ws.on("close", () => {
    clients.delete(ws);
  });

  ws.on("message", async (raw) => {
    let payload = null;
    try {
      payload = JSON.parse(raw.toString("utf8"));
    } catch (_) {
      safeSend(ws, { type: "send_result", ok: false, error: "invalid json", ts: nowMs() });
      return;
    }
    if (!payload || typeof payload !== "object") {
      safeSend(ws, { type: "send_result", ok: false, error: "invalid payload", ts: nowMs() });
      return;
    }

    const msgType = String(payload.type || "").trim().toLowerCase();
    if (msgType === "ping") {
      safeSend(ws, { type: "pong", ts: nowMs() });
      return;
    }
    if (msgType !== "send") {
      safeSend(ws, { type: "send_result", ok: false, error: "unsupported type", ts: nowMs() });
      return;
    }

    const jid = String(payload.jid || "").trim();
    const text = String(payload.text || "").trim();
    const requestId = String(payload.request_id || "").trim();
    if (!jid || !text) {
      safeSend(ws, {
        type: "send_result",
        ok: false,
        request_id: requestId,
        error: "jid/text required",
        ts: nowMs(),
      });
      return;
    }
    if (!sock || !connected) {
      safeSend(ws, {
        type: "send_result",
        ok: false,
        request_id: requestId,
        error: "whatsapp not connected",
        ts: nowMs(),
      });
      return;
    }

    try {
      await sock.sendMessage(jid, { text });
      safeSend(ws, {
        type: "send_result",
        ok: true,
        request_id: requestId,
        jid,
        ts: nowMs(),
      });
    } catch (err) {
      safeSend(ws, {
        type: "send_result",
        ok: false,
        request_id: requestId,
        error: err instanceof Error ? err.message : String(err),
        ts: nowMs(),
      });
    }
  });
});

console.log(`[whatsapp-bridge] ws://${HOST}:${PORT}${PATHNAME}`);
void initBaileys().then(connectWhatsApp).catch((err) => {
  console.error("[whatsapp-bridge] startup failed:", err);
  process.exitCode = 1;
});

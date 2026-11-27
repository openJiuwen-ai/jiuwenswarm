# WhatsApp Channel Setup

This guide describes the WhatsApp integration currently implemented in this repo.

## Architecture

JiuwenClaw does not talk to WhatsApp directly from Python.

`WhatsApp app` <-> `Baileys bridge (Node.js)` <-> `WhatsAppChannel (Python)`

- Bridge WebSocket default: `ws://127.0.0.1:19600/ws`
- Bridge script: `jiuwenclaw/scripts/whatsapp-bridge.js`
- Python channel: `jiuwenclaw/channel/whatsapp_channel.py`
- Runtime config: `channels.whatsapp` in `config.yaml`

## What This Repo Implements

- The Node bridge uses Baileys to log in to WhatsApp Web and send/receive messages.
- The Python channel connects to the local bridge over WebSocket and exchanges JSON frames.
- The Python side now tracks connection state from bridge events and only sends messages when WhatsApp is actually connected.
- `allow_from` filters inbound WhatsApp senders by JID or by the number part.

## What This Repo Does Not Implement

- There is no Python-to-bridge auth handshake or shared-secret token.
- The bridge is local-only by default because it binds to `127.0.0.1`.
- Media download and attachment forwarding are not implemented.
- Voice-message transcription is not implemented.
- Python-side inbound message deduplication is not implemented.

## WebSocket Protocol

Python sends:

```json
{
  "type": "send",
  "jid": "123456789@s.whatsapp.net",
  "text": "hello",
  "request_id": "msg-123"
}
```

Bridge sends:

- `status`: bridge / WhatsApp connection state updates
- `qr`: QR code is available for linking
- `inbound`: inbound text message from WhatsApp
- `send_result`: acknowledgement or error for a `send`
- `pong`: reply to bridge ping

## Connection States

The Python channel tracks these states from bridge events:

- `stopped`: channel is stopped
- `bridge_connected`: Python is connected to the local bridge WebSocket, but WhatsApp may still be connecting
- `connecting`: bridge is trying to connect Baileys to WhatsApp
- `qr_pending`: a QR code is waiting to be scanned
- `open`: WhatsApp is connected and sending is allowed
- `close`: WhatsApp connection closed
- `logged_out`: WhatsApp session was logged out and needs relinking
- `bridge_disconnected`: Python lost the local bridge WebSocket connection

The distinction matters:

- `bridge_connected` only means Python can reach the local bridge.
- `open` means the bridge is actually logged in to WhatsApp and can send messages.

## Channel Metadata

`WhatsAppChannel.get_metadata()` now exposes runtime state in `extra`:

- `bridge_state`
- `bridge_ws_connected`
- `whatsapp_connected`
- `qr_pending`
- `last_status_ts_ms`
- `last_status_code`

## Prerequisites

- Python environment that can run `python -m jiuwenclaw.app`
- Node.js 20+ and npm
- A WhatsApp account with Linked Devices enabled

## 1. Install Bridge Dependencies

Run inside the inner project folder that contains `jiuwenclaw/package.json`:

```powershell
cd C:\Users\chiak\OneDrive\Desktop\jiuwenclaw\jiuwenclaw
npm install
```

If you only want the bridge dependencies:

```powershell
npm install @whiskeysockets/baileys ws pino qrcode-terminal
```

## 2. Configure WhatsApp

Edit your runtime config file:

`C:\Users\chiak\.jiuwenclaw\config\config.yaml`

Under `channels:` use:

```yaml
  whatsapp:
    bridge_ws_url: ws://127.0.0.1:19600/ws
    default_jid:
    allow_from: []
    enable_streaming: true
    auto_start_bridge: false
    bridge_command: node scripts/whatsapp-bridge.js
    bridge_workdir: C:/Users/chiak/OneDrive/Desktop/jiuwenclaw/jiuwenclaw
    enabled: true
```

Notes:

- `enable_streaming: true` forwards intermediate events such as token deltas and tool progress.
- `enable_streaming: false` suppresses `chat.delta` events and keeps output more final-only.
- `default_jid` is used as a fallback target for outbound sends.
- `allow_from` accepts either a full sender JID or the number part before `@`.
- `auto_start_bridge: true` lets Python start the Node bridge process automatically.

## 3. Start the Services

Open two terminals unless you use `auto_start_bridge: true`.

Terminal A, bridge:

```powershell
cd C:\Users\chiak\OneDrive\Desktop\jiuwenclaw\jiuwenclaw
npm run whatsapp:bridge
```

Expected line:

`[whatsapp-bridge] ws://127.0.0.1:19600/ws`

Terminal B, app:

```powershell
cd C:\Users\chiak\OneDrive\Desktop\jiuwenclaw
python -m jiuwenclaw.app
```

Expected behavior:

- The app logs that `WhatsAppChannel` was registered.
- The channel first reaches `bridge_connected`.
- It then moves to `connecting`, `qr_pending`, or `open` depending on login state.

## 4. Link WhatsApp

When the bridge has no valid auth state, it prints a QR code in the bridge terminal.

In WhatsApp:

`Settings` -> `Linked devices` -> `Link a device`

Then scan the QR code from Terminal A.

Auth state is stored at:

`jiuwenclaw/jiuwenclaw/workspace/.whatsapp-auth`

If the account is already linked, QR may not appear.

## 5. Sending and Receiving

- Inbound text messages are forwarded from the bridge to the Python channel as `inbound`.
- Outbound messages are sent from Python as `send`.
- Python now blocks sends unless channel state is `open`.
- If the bridge replies with `send_result.ok=false`, the Python channel logs the failure.

## 6. Security Notes

- The bridge listens on `127.0.0.1` by default, which limits access to the local machine.
- This repo currently does not implement bridge token auth.
- Do not expose the bridge port directly to other hosts without adding authentication or a trusted tunnel boundary.

## 7. LLM Config Still Matters

If inbound WhatsApp messages fail downstream with errors such as `405 Not Allowed`, the model config is probably placeholder or invalid.

Example `.env` values:

```env
MODEL_PROVIDER=OpenAI
MODEL_NAME=your-real-model
API_BASE=https://your-real-openai-compatible-endpoint/v1
API_KEY=your-real-key
```

Restart `python -m jiuwenclaw.app` after updating `.env`.

## Troubleshooting

### `Missing script: "whatsapp:bridge"`

You ran `npm` in the wrong folder. Use:

`C:\Users\chiak\OneDrive\Desktop\jiuwenclaw\jiuwenclaw`

### Bridge starts but no QR

1. Stop any old bridge processes that may still be holding the port or auth session.
2. Delete auth state to force relinking:
   `jiuwenclaw/jiuwenclaw/workspace/.whatsapp-auth`
3. Start the bridge again and wait for `QR received`.

### App connects to bridge but cannot send

Check the connection state in logs:

- If state is `bridge_connected` or `connecting`, the local bridge is reachable but WhatsApp is not ready yet.
- If state is `qr_pending`, scan the QR code.
- If state is `logged_out`, delete the auth directory and relink.
- Only state `open` allows sends.

### App says WhatsApp channel not configured

- Make sure the YAML key is `channels.whatsapp`.
- Make sure `enabled: true` is set.
- Make sure `bridge_ws_url` is not empty.

### Bluestacks or emulator linking issues

Scanning from a physical phone is usually more reliable than emulator camera passthrough.

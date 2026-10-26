# International Channels

JiuwenSwarm supports integration with multiple international chat platforms. Below are detailed configuration instructions for each channel.

## Telegram

### 1. Create a Telegram Bot

Use [@BotFather](https://t.me/BotFather) to create a bot and get a **Bot Token**.

**Step 1:** Search for `@BotFather` in Telegram and open the conversation.

![BotFather](../assets/images/TelegramBotFather.png)

**Step 2:** Send `/newbot` and follow the prompts.

BotFather will ask you to enter:
- **Bot display name** (e.g. `JiuwenSwarm Bot`)
- **Bot username** (must end with `bot`, e.g. `jiuwenswarm_bot`)

![BotFather token](../assets/images/Telegram获取BotToken.png)

**Step 3:** **Save the Bot Token**

After creation, BotFather returns a token in the format `123456789:ABCDefGhIJKlmN...`. Save it securely — you'll need it for configuration.

> ⚠️ **Note**: The Bot Token is equivalent to the bot's password — do not leak it. If the token is exposed, use `/revoke` in BotFather to regenerate it.

### 2. Bind the Channel

#### Option A: Web UI (recommended)

In JiuwenSwarm's frontend, click the **Agent** / **Channels** card, fill in the Bot Token in the Telegram channel module, enable it, and save.

![Telegram channel](../assets/images/Telegram频道配置.png)

#### Option B: Edit `config.yaml`

Edit `~/.jiuwenswarm/config/config.yaml`:

``````yaml
channels:
  telegram:
    bot_token: "<your Bot Token>"
    allow_from: []
    parse_mode: Markdown
    group_chat_mode: mention
    enabled: true
``````

If the service is already running it will auto-reload; otherwise run `jiuwenswarm-start`.

### 3. Configuration

| Field | Description | Default |
|:------|:------------|:--------|
| `bot_token` | Bot Token from @BotFather (**required**) | empty |
| `allow_from` | Allowed Telegram `user_id` whitelist; empty = all users | `[]` |
| `parse_mode` | Message parse mode: `Markdown`, `HTML`, or `None` | `Markdown` |
| `group_chat_mode` | Group chat response mode | `mention` |
| `enabled` | Enable Telegram channel | `false` |

#### Group Chat Modes

When the bot is added to a Telegram group, `group_chat_mode` controls how the bot responds:

| Mode | Description |
|:-----|:------------|
| `mention` | **Only respond to @mentions** (recommended) |
| `reply` | **Only respond to replies** |
| `all` | **Respond to all messages** |
| `off` | **Disable group chat** |

### 4. Start Chatting

**Option 1:** Search for your bot's username in Telegram and send a message to start chatting.

![Telegram DM](../assets/images/Telegram对话界面.png)

**Option 2:** Add the bot to a group and interact based on the `group_chat_mode` setting.

![Telegram group](../assets/images/Telegram群组对话界面.png)

### 5. Get `user_id` (Whitelist)

To configure the `allow_from` whitelist, you need to obtain the user's Telegram `user_id`:

1. Search for `@userinfobot` in Telegram and send any message to get your `user_id`.

2. Add the obtained `user_id` to the `allow_from` list:

``````yaml
channels:
  telegram:
    bot_token: "<your Bot Token>"
    allow_from:
      - "123456789"
      - "987654321"
    enabled: true
``````

> 💡 **Tip**: When `allow_from` is an empty list, all users can use the bot. After setting a whitelist, only users in the list can chat with the bot.

---

## Discord

Discord channel integration is supported in the current version. Configure and enable the Discord Bot in **Channel Management**, or manually edit `config.yaml`.

### Configuration fields

- `bot_token`
- `application_id`
- `guild_id`
- `channel_id`
- `block_dm`
- `allow_from`
- `enabled`

Configure in `~/.jiuwenswarm/config/config.yaml` as follows:

``````yaml
channels:
  discord:
    bot_token: "Discord Bot Token"
    application_id: "Application ID"
    guild_id: "Target server Guild ID"
    channel_id: "Target channel Channel ID"
    block_dm: false
    allow_from: []
    enabled: true
``````

### Quick Start Guide

1. Create a Bot in the Discord Developer Portal and get the `bot_token`
2. On the Bot tab, enable **Message Content Intent**
3. Invite the bot to the target server and grant read/write channel permissions
4. Fill in the configuration in JiuwenSwarm and enable `enabled: true`

### Fields

| Field | Description | Default |
|:------|:------------|:--------|
| `bot_token` | Discord Bot Token (required) | empty |
| `application_id` | Application ID (optional, recommended) | empty |
| `guild_id` | Listen only to the specified server | empty |
| `channel_id` | Listen/reply only to the specified channel | empty |
| `block_dm` | When `true`, DMs are not processed | `false` |
| `allow_from` | Allowed Discord user ID list | `[]` |
| `enabled` | Enable Discord channel | `false` |

### Detailed Setup Steps

#### What This Repo Uses

- Python channel: `jiuwenswarm/channel/discord_channel.py` (discord.py)
- Runtime config: `channels/discord` in your `config.yaml` (or the web UI **Channel Management** → Discord)

The bot connects with your **Bot Token**, receives messages in configured guild channels and/or DMs (unless you turn off DMs), and can add a 👀 reaction while processing, similar to other channels.

#### Prerequisites

- A Discord account
- Permission to add bots to a server (or users can install the app for DMs, depending on your install settings)

#### 1. Create an application

1. Open [https://discord.com/developers/applications](https://discord.com/developers/applications).
2. Click **New Application**, choose a name, and create it.

![Create a new Discord application](../assets/images/discord/1_create_new_app.png)

You will use this application for both **OAuth2 / installation** and the **Bot** user.

#### 2. Get the Bot Token (reset and copy)

1. In the left sidebar, open **Bot**.
2. Under **Token**, use **Reset Token** and confirm.
3. **Copy the token immediately** and store it somewhere safe. Discord shows it only once after a reset.

![Reset and copy Bot Token](../assets/images/discord/2_reset_bot_token.png)

**Security**

- Treat the token like a password. Anyone with it can control your bot.
- Paste it into JiuwenSwarm’s Discord settings or `config.yaml`; do not commit it to git.
- If it leaks, reset the token again in the same place.

You will map this value to **`bot_token`** in JiuwenSwarm.

#### 3. Enable Message Content Intent

JiuwenSwarm reads the text of messages your bot receives. Discord requires an explicit privileged intent for that.

1. Stay on the **Bot** page.
2. Under **Privileged Gateway Intents**, turn on **MESSAGE CONTENT INTENT**.
3. Save changes if the portal prompts you.

![Enable Message Content Intent](../assets/images/discord/3_set_required_intent.png)

Without this, the bot may connect but will not see normal message text content.

#### 4. Guild install: scopes and bot permissions

Configure how the bot is installed into servers and what it can do there. Typical needs for JiuwenSwarm:

- **Read** messages in the channels you care about
- **Send** messages (replies)
- **Read message history** (context)
- **Add reactions** (e.g. 👀 while processing)
- **Use slash commands** (optional, if you use them elsewhere)

In the Developer Portal, use the installation / OAuth2 tools (e.g. **Installation** or **OAuth2 → URL Generator**, depending on the current UI) to select:

- Scopes such as **`bot`** (and **`applications.commands`** if you use application commands).
- Bot permissions that match the list above.
- Screenshot attached below shows the recommended permissions, the mandatory ones are marked.

![Guild install scopes and bot permissions](../assets/images/discord/4_set_guild_install_scope_and_permissions.png)

Exact labels may move between **Installation**, **OAuth2**, and **Bot** sections as Discord updates the portal; align your choices with the screenshot and the capabilities above.

#### 5. Install methods and setup / invite link

Choose how users or admins can add the app:

- **Guild install** — add the bot to a server (you need a shareable install or invite URL).
- **User install** — allows users to add the app for **direct messages** (useful if you want DM-only usage without a fixed guild channel).

Copy the **generated URL** or **Install link** from the portal and open it in a browser to complete authorization.

![Install methods and copy setup link](../assets/images/discord/5_select_install_methods_and_copy_setup_link.png)

After installation:

- For **server** use: place the bot in a channel JiuwenSwarm will listen to (see `guild_id` / `channel_id` below).
- For **DM** use: users can open a private message with the bot (if not blocked by **`block_dm`** in JiuwenSwarm).

#### 6. IDs you need for JiuwenSwarm

| Value | Where to find it |
|--------|------------------|
| **Application ID** | **General Information** → **APPLICATION ID** (copy). Maps to **`application_id`** (optional but recommended). |
| **Guild ID** | Discord: enable **Developer Mode** (Settings → App Settings → Advanced). Right‑click the **server icon** → **Copy Server ID**. Maps to **`guild_id`**. Leave empty if you only use DMs and do not restrict to one server. |
| **Channel ID** | Right‑click the **text channel** → **Copy Channel ID**. Maps to **`channel_id`**. Leave empty to rely on DM-only or broader routing per your deployment. |

If both **`guild_id`** and **`channel_id`** are set, the bot handles messages **only in that channel** on that server, while **DMs can still work** unless **`block_dm`** is enabled.

A trick to get `guild_id` and `channel_id` easily is to check the url to a channel, since the format would be:
```
https://discord.com/channels/<guild_id>/<channel_id>
```

#### 7. Configure Discord in JiuwenSwarm (Channel Management)

Open the JiuwenSwarm web UI → **Channel Management** → **Discord**, or edit:

`~/.jiuwenswarm/config/config.yaml` (path may differ on your machine)

Example:

```yaml
channels:
  discord:
    bot_token: "YOUR_BOT_TOKEN"
    application_id: "YOUR_APPLICATION_ID"
    guild_id: "YOUR_GUILD_ID"        # optional if DM-only
    channel_id: "YOUR_CHANNEL_ID"    # optional if DM-only
    block_dm: false                  # set true to ignore DMs
    allow_from: []                   # empty = all users; else list Discord user IDs
    enabled: true
```

| Field | Purpose |
|--------|---------|
| `bot_token` | **Required.** From step 2. |
| `application_id` | Application ID from **General Information**. |
| `guild_id` | Restrict handling to one server; optional for DM-focused setups. |
| `channel_id` | Restrict handling to one channel in that server; optional with DMs. |
| `block_dm` | If `true`, ignore direct messages. |
| `allow_from` | Allow-list of Discord user IDs; empty allows everyone who can message the bot. |
| `enabled` | Turn the Discord channel on. |

#### 8. Verify

1. Ensure the bot is online in the server or available in DMs.
2. Send a short message in the configured channel or in DM.
3. You should see a 👀 reaction on the user message (if the bot has **Add Reactions** in that context), then a reply from your agent pipeline when the model and tools are configured correctly.

#### Troubleshooting

**Bot online but no replies**

- Confirm **MESSAGE CONTENT INTENT** is enabled.
- Confirm **`enabled: true`** and **`bot_token`** are correct.
- Check **`guild_id` / `channel_id`**: messages outside the configured channel are ignored when those are set.
- If using **`allow_from`**, your Discord user ID must be listed (or leave the list empty).

**Cannot add 👀 reaction**

- Grant the bot **Add Reactions** in the channel (channel overrides or role permissions).

**DMs not working**

- Perform **User install** in Discord.
- In JiuwenSwarm, ensure **`block_dm`** is `false`.
- Users may need to open the bot’s profile and **Message** after installing.

**LLM or downstream errors**

Discord delivery is separate from model configuration. If you see HTTP errors from the model API, fix `.env` / model settings and restart the app (same idea as other channels).

---

## Slack

JiuwenSwarm connects to Slack through the asynchronous Slack Bolt Socket Mode adapter. Socket Mode receives events over WebSocket, so JiuwenSwarm does not need a public HTTP callback URL.

### 1. Create a Slack App

1. Open [Slack API Apps](https://api.slack.com/apps), create an app, and select the target workspace.
2. Enable **Socket Mode**.
3. Create an App-Level Token with the `connections:write` scope and save the generated `xapp-...` token.
4. Under **OAuth & Permissions**, add these Bot Token Scopes:
   - `chat:write`
   - `app_mentions:read`
   - `im:history`
5. Under **Event Subscriptions**, subscribe to these Bot Events:
   - `app_mention`
   - `message.im`
6. Install the app to the workspace and save the generated `xoxb-...` Bot Token.
7. Add the bot to every Slack channel where it should respond.

> Treat both `xoxb-...` and `xapp-...` tokens as secrets. Never commit them or print them in logs.

### 2. Configure JiuwenSwarm

Use **Channel Management** → **Slack** in the Web UI, or edit:

`~/.jiuwenswarm/config/config.yaml`

```yaml
channels:
  slack:
    bot_token: "xoxb-your-bot-token"
    app_token: "xapp-your-app-token"
    allow_from: []
    allowed_channel_ids: []
    default_channel_id:
    reply_in_thread: true
    enabled: true
```

| Field | Purpose | Default |
|:------|:--------|:--------|
| `bot_token` | Required Slack Bot Token in `xoxb-...` format | empty |
| `app_token` | Required Socket Mode App Token in `xapp-...` format | empty |
| `allow_from` | Allow-list of Slack user IDs; empty allows all users | `[]` |
| `allowed_channel_ids` | Channel IDs allowed to mention the bot; empty allows all channels and does not restrict DMs | `[]` |
| `default_channel_id` | Fallback channel for outbound messages without request context | empty |
| `reply_in_thread` | Reply in the thread containing the triggering channel message | `true` |
| `enabled` | Enable the Slack channel | `false` |

### 3. Use the Bot

- In a channel, send `@bot your message`. JiuwenSwarm processes it and replies in the message thread by default.
- Send the bot a direct message without mentioning it.
- Channel conversations are isolated by Slack thread, so separate threads do not share a JiuwenSwarm session.
- The current integration sends final text replies and suppresses token-level `chat.delta` events to avoid channel noise and Slack rate limits.

Slack user IDs and channel IDs are available from the member profile and channel details menus via **Copy ID**.

### 4. Troubleshooting

**The bot does not connect**

- Confirm Socket Mode is enabled.
- Confirm `app_token` is an `xapp-...` token with `connections:write`.
- Confirm `bot_token` is the `xoxb-...` token generated after installing the app.

**Channel mentions get no reply**

- Confirm the app subscribes to `app_mention`.
- Confirm the bot is a member of the channel.
- If `allowed_channel_ids` is configured, include the current channel ID.
- If `allow_from` is configured, include the current user ID.

**Direct messages get no reply**

- Confirm the app has `im:history` and subscribes to `message.im`.
- Reinstall the app to the workspace after changing scopes or event subscriptions.

---

## WhatsApp

This guide describes the WhatsApp integration currently implemented in this repo.

### Architecture

JiuwenSwarm does not talk to WhatsApp directly from Python.

`WhatsApp app` <-> `Baileys bridge (Node.js)` <-> `WhatsAppChannel (Python)`

- Bridge WebSocket default: `ws://127.0.0.1:19600/ws`
- Bridge script: `jiuwenswarm/scripts/whatsapp-bridge.js`
- Python channel: `jiuwenswarm/channel/whatsapp_channel.py`
- Runtime config: `channels.whatsapp` in `config.yaml`

### What This Repo Implements

- The Node bridge uses Baileys to log in to WhatsApp Web and send/receive messages.
- The Python channel connects to the local bridge over WebSocket and exchanges JSON frames.
- The Python side now tracks connection state from bridge events and only sends messages when WhatsApp is actually connected.
- `allow_from` filters inbound WhatsApp senders by JID or by the number part.

### What This Repo Does Not Implement

- There is no Python-to-bridge auth handshake or shared-secret token.
- The bridge is local-only by default because it binds to `127.0.0.1`.
- Media download and attachment forwarding are not implemented.
- Voice-message transcription is not implemented.
- Python-side inbound message deduplication is not implemented.

### WebSocket Protocol

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

### Connection States

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

### Channel Metadata

`WhatsAppChannel.get_metadata()` now exposes runtime state in `extra`:

- `bridge_state`
- `bridge_ws_connected`
- `whatsapp_connected`
- `qr_pending`
- `last_status_ts_ms`
- `last_status_code`

### Prerequisites

- Python environment that can run `python -m jiuwenswarm.app`
- Node.js 20+ and npm
- A WhatsApp account with Linked Devices enabled

### 1. Install Bridge Dependencies

Run inside the inner project folder that contains `jiuwenswarm/package.json`:

```powershell
cd <project-root>/jiuwenswarm
npm install
```

If you only want the bridge dependencies:

```powershell
npm install @whiskeysockets/baileys ws pino qrcode-terminal
```

### 2. Configure WhatsApp

Edit your runtime config file:

`~/.jiuwenswarm/config/config.yaml`

Under `channels:` use:

```yaml
  whatsapp:
    bridge_ws_url: ws://127.0.0.1:19600/ws
    default_jid:
    allow_from: []
    enable_streaming: true
    auto_start_bridge: false
    bridge_command: node scripts/whatsapp-bridge.js
    bridge_workdir: <project-root>/jiuwenswarm
    enabled: true
```

Notes:

- `enable_streaming: true` forwards intermediate events such as token deltas and tool progress.
- `enable_streaming: false` suppresses `chat.delta` events and keeps output more final-only.
- `default_jid` is used as a fallback target for outbound sends.
- `allow_from` accepts either a full sender JID or the number part before `@`.
- `auto_start_bridge: true` lets Python start the Node bridge process automatically.

### 3. Start the Services

Open two terminals unless you use `auto_start_bridge: true`.

Terminal A, bridge:

```powershell
cd <project-root>/jiuwenswarm
npm run whatsapp:bridge
```

Expected line:

`[whatsapp-bridge] ws://127.0.0.1:19600/ws`

Terminal B, app:

```powershell
cd <project-root>
python -m jiuwenswarm.app
```

Expected behavior:

- The app logs that `WhatsAppChannel` was registered.
- The channel first reaches `bridge_connected`.
- It then moves to `connecting`, `qr_pending`, or `open` depending on login state.

### 4. Link WhatsApp

When the bridge has no valid auth state, it prints a QR code in the bridge terminal.

In WhatsApp:

`Settings` -> `Linked devices` -> `Link a device`

Then scan the QR code from Terminal A.

Auth state is stored at:

`jiuwenswarm/jiuwenswarm/workspace/.whatsapp-auth`

If the account is already linked, QR may not appear.

### 5. Sending and Receiving

- Inbound text messages are forwarded from the bridge to the Python channel as `inbound`.
- Outbound messages are sent from Python as `send`.
- Python now blocks sends unless channel state is `open`.
- If the bridge replies with `send_result.ok=false`, the Python channel logs the failure.

### 6. Security Notes

- The bridge listens on `127.0.0.1` by default, which limits access to the local machine.
- This repo currently does not implement bridge token auth.
- Do not expose the bridge port directly to other hosts without adding authentication or a trusted tunnel boundary.

### 7. LLM Config Still Matters

If inbound WhatsApp messages fail downstream with errors such as `405 Not Allowed`, the model config is probably placeholder or invalid.

Example `.env` values:

```env
MODEL_PROVIDER=OpenAI
MODEL_NAME=your-real-model
API_BASE=https://your-real-openai-compatible-endpoint/v1
API_KEY=your-real-key
```

Restart `python -m jiuwenswarm.app` after updating `.env`.

### Troubleshooting

**`Missing script: "whatsapp:bridge"`**

You ran `npm` in the wrong folder. Use:

`<project-root>/jiuwenswarm`

**Bridge starts but no QR**

1. Stop any old bridge processes that may still be holding the port or auth session.
2. Delete auth state to force relinking:
   `jiuwenswarm/jiuwenswarm/workspace/.whatsapp-auth`
3. Start the bridge again and wait for `QR received`.

**App connects to bridge but cannot send**

Check the connection state in logs:

- If state is `bridge_connected` or `connecting`, the local bridge is reachable but WhatsApp is not ready yet.
- If state is `qr_pending`, scan the QR code.
- If state is `logged_out`, delete the auth directory and relink.
- Only state `open` allows sends.

**App says WhatsApp channel not configured**

- Make sure the YAML key is `channels.whatsapp`.
- Make sure `enabled: true` is set.
- Make sure `bridge_ws_url` is not empty.

**Bluestacks or emulator linking issues**

Scanning from a physical phone is usually more reliable than emulator camera passthrough.

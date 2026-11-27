# OpenJiuwen Web Frontend

AI coding assistant web UI built with React + TypeScript + Tailwind CSS, with design inspired by JiuwenClaw.

## Features

### Implemented

#### 💬 Chat
- **Real-time conversation**: WebSocket bidirectional communication with streaming output
- **Markdown rendering**: Code highlighting, lists, links, etc.
- **Thinking indicator**: Animated indicator while the AI is thinking
- **Message history**: User and assistant conversation history

#### 🛠 Tool Calls
- **Tool execution display**: Shows tool names and parameters invoked by the AI
- **Execution results**: Success/failure status and return values
- **Available tools list**: Right panel lists currently available tools

#### 📋 Task Management
- **Todo list**: Displays tasks created by the AI
- **Status grouping**: Grouped by in progress, pending, completed
- **Live updates**: Task status changes sync in real time

#### 📂 Session Management
- **Session list**: Sidebar shows past sessions
- **Switch sessions**: Click to switch between sessions
- **Delete session**: Hover to show delete button
- **Session persistence**: Last session restored on page refresh

#### ⚙️ Mode Switching
- **BUILD mode**: Default coding mode
- **PLAN mode**: Planning mode
- **REVIEW mode**: Review mode

#### 🎨 Theme
- **Light theme**: Default, blue accent
- **Dark theme**: Dark background, optimized blue visibility
- **System follow**: Optional system theme sync

#### ⏯ Flow Control
- **Pause/Resume**: Pause and resume AI processing
- **Interrupt**: Stop current task, optionally with new instructions

#### 🎤 Voice
- **Voice input**: Click mic button for speech input (STT)
- **Voice read-out**: Hover on AI reply to show read-aloud button (TTS)
- **Barge-in**: Interrupt AI while speaking

## Tech Stack

- **Framework**: React 18 + TypeScript
- **Styling**: Tailwind CSS + CSS Variables
- **State**: Zustand
- **Build**: Vite
- **Communication**: WebSocket + REST API

## Quick Start

### Requirements

- Node.js 18+
- npm or pnpm

### Install Dependencies

```bash
cd jiuwenclaw/web
npm install
```

### Configure Backend URL

Edit the proxy in `vite.config.ts`:

```typescript
proxy: {
  '/api': {
    target: 'http://127.0.0.1:19000',  // Your backend URL
    changeOrigin: true,
  },
  '/ws': {
    target: 'http://127.0.0.1:19000',  // Your backend URL
    ws: true,
    changeOrigin: true,
  },
}
```

### Start Dev Server

```bash
npm run dev
```

Open http://localhost:5173

### Start Backend

```bash
cd jiuwenclaw
PORT=19000 python -m jiuwenclaw.api.app
```

## Backend API Requirements

The frontend expects the following backend APIs:

### REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config` | GET | Get service config (provider, model, etc.) |
| `/api/sessions` | GET | Get session list |
| `/api/sessions/:id` | DELETE | Delete session |

### WebSocket

Endpoint: `/ws`

Notes:
- The frontend uses a single WS endpoint for RPC (`req` / `res` / `event`)
- `session_id` is sent in the request body as `params.session_id`, not in the URL path
- `provider` is determined by backend config by default; pass via query only when overriding

#### Client → Server

Unified request frame (example):

```json
{
  "type": "req",
  "id": "req_xxx",
  "method": "chat.send",
  "params": {
    "session_id": "sess_xxx",
    "content": "hello"
  }
}
```

Common `method` values:

| method | Description |
|--------|-------------|
| `chat.send` | Send chat message |
| `chat.interrupt` | Interrupt/pause task |
| `chat.resume` | Resume task |
| `chat.user_answer` | Submit user Q&A result |
| `session.list` | Get session list |
| `config.get` | Get service config |

#### Server → Client

Response frame (`res`):

```json
{
  "type": "res",
  "id": "req_xxx",
  "ok": true,
  "payload": {}
}
```

Event frame (`event`):

```json
{
  "type": "event",
  "event": "chat.delta",
  "payload": {
    "session_id": "sess_xxx"
  }
}
```

Common events:
- `connection.ack`
- `chat.delta`
- `chat.final`
- `chat.tool_call`
- `chat.tool_result`
- `todo.updated`
- `chat.processing_status`
- `chat.interrupt_result`
- `chat.subtask_update`
- `chat.ask_user_question`

## Dev Mode WS Logging

When running `npm run dev`, the frontend writes `/ws` requests and responses to a local log file for debugging:

- Log file: `jiuwenclaw/web/logs/ws-dev.log`
- One JSON object per line (JSONL)
- Directions:
  - `payload.direction = "outgoing"`: Frontend `req` messages
  - `payload.direction = "incoming"`: Backend `res` / `event` messages
  - `payload.direction = "lifecycle"`: Connection lifecycle (open/error/close)

Example:

```json
{"ts":"2026-02-12T08:10:05.120Z","payload":{"direction":"outgoing","messageType":"req","data":{"type":"req","id":"req_xxx","method":"chat.send","params":{"session_id":"sess_001","content":"你好"}},"at":"2026-02-12T08:10:05.119Z"}}
{"ts":"2026-02-12T08:10:05.150Z","payload":{"direction":"incoming","messageType":"res","data":{"type":"res","id":"req_xxx","ok":true,"payload":{"accepted":true}},"at":"2026-02-12T08:10:05.149Z"}}
{"ts":"2026-02-12T08:10:05.300Z","payload":{"direction":"incoming","messageType":"event","data":{"type":"event","event":"chat.delta","payload":{"session_id":"sess_001","content":"..."}},"at":"2026-02-12T08:10:05.299Z"}}
```

If you only see `lifecycle error/close (code=1006)`, the backend is likely not running or the WS port is unreachable.

## Project Structure

```
jiuwenclaw/web/
├── public/
│   └── logo.png           # App logo
├── src/
│   ├── components/
│   │   ├── ChatPanel/     # Chat panel
│   │   ├── SessionSidebar/# Session sidebar
│   │   ├── StatusBar/     # Status bar
│   │   ├── TodoList/      # Todo list
│   │   └── ToolPanel/     # Tool panel
│   ├── hooks/
│   │   └── useWebSocket.ts# WebSocket hook
│   ├── stores/
│   │   ├── chatStore.ts   # Chat state
│   │   ├── sessionStore.ts# Session state
│   │   └── todoStore.ts   # Todo state
│   ├── types/             # TypeScript types
│   ├── utils/             # Utilities
│   ├── App.tsx            # Main app
│   ├── index.css          # Global styles + CSS variables
│   └── main.tsx           # Entry
├── index.html
├── tailwind.config.js
├── vite.config.ts
└── package.json
```

## Customization

### Branding

1. Replace `public/logo.png`
2. Change `<title>` in `index.html`
3. Change brand text in `src/App.tsx`

### Theme Colors

Edit CSS variables in `src/index.css`:

```css
:root {
  --accent: #60a5fa;        /* Dark mode accent */
  --accent-hover: #93c5fd;  /* Hover */
}

:root[data-theme="light"] {
  --accent: #2563eb;        /* Light mode accent */
  --accent-hover: #3b82f6;  /* Hover */
}
```

## License

MIT

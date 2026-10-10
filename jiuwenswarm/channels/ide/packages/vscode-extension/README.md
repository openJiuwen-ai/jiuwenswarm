# JiuwenSwarm

Bring the full power of **JiuwenSwarm** — an autonomous, multi-agent AI orchestration system — directly into Visual Studio Code.

## What is JiuwenSwarm?

JiuwenSwarm is a locally-running AI engine that coordinates a swarm of specialized agents to handle complex software engineering tasks end-to-end: reading and editing files, running shell commands, searching codebases, calling external tools, and reasoning across multiple steps — all without leaving your IDE.

## Core Features

- **Streaming Chat Panel** — conversational interface with real-time token streaming, markdown rendering, and code block highlighting. Each response arrives word-by-word so you never wait for the full answer.
- **Tool Call Cards** — every agent action (file read/write, bash command, web search, MCP tool) is displayed inline as a collapsible card with live status, inputs, and outputs — full transparency into what the swarm is doing.
- **Session Management** — create, switch between, and resume named sessions. Each session preserves its full conversation history and agent context so you can pick up exactly where you left off.
- **Send Selection** — select any code in the editor and send it to JiuwenSwarm with **Ctrl+Shift+E** (Win/Linux) / **⌘⇧E** (Mac) for instant review, explanation, or refactoring.
- **New Session Shortcut** — open a fresh chat at any time with **Ctrl+Shift+J** / **⌘⇧J**.
- **Connection Status Bar** — status-bar indicator shows live WebSocket connection state (connected / reconnecting / disconnected). Click to reconnect.
- **Multi-model Support** — JiuwenSwarm supports Claude (Anthropic), GPT-4o (OpenAI), Gemini, and other providers. Switch models in the JiuwenSwarm settings without changing your IDE plugin configuration.
- **Skills & MCP** — invoke built-in slash-command skills (`/commit`, `/review`, `/init`, …) and any MCP server tools registered with your JiuwenSwarm instance.

## Requirements

JiuwenSwarm must be running locally before the extension connects. By default the extension connects to `ws://localhost:19000`. Host and port are configurable under **Settings → Extensions → JiuwenSwarm**.

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `jiuwenswarm.host` | `localhost` | JiuwenSwarm server hostname |
| `jiuwenswarm.port` | `19000` | JiuwenSwarm WebSocket port |
| `jiuwenswarm.defaultMode` | `code.plan` | Default agent mode: Plan & Explore / Execute / Team Coding |
| `jiuwenswarm.channelId` | `ide` | Channel ID reported to JiuwenSwarm |
| `jiuwenswarm.autoConnect` | `true` | Automatically connect on startup |

## Getting Started

1. Start your local JiuwenSwarm server (`jiuwenswarm start`).
2. Click the **JiuwenSwarm** icon in the Activity Bar (left sidebar).
3. A session is created automatically on first connect.
4. Type a message or select code and press **Ctrl+Shift+E** to begin.

## Commands

| Command | Keybinding | Description |
|---------|------------|-------------|
| `JiuwenSwarm: Open Chat` | `Ctrl+Shift+J` / `⌘⇧J` | Open the JiuwenSwarm chat panel |
| `JiuwenSwarm: New Session` | — | Start a new chat session |
| `JiuwenSwarm: Send Selection to Chat` | `Ctrl+Shift+E` / `⌘⇧E` | Send selected code to the chat |
| `JiuwenSwarm: Reconnect` | — | Force-reconnect to the WebSocket gateway |

## Feedback & Source

JiuwenSwarm is open-source. Report issues and contribute at [github.com/openjiuwen](https://github.com/openjiuwen).

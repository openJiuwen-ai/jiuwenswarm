# Harness Workbench (Gateway)

Remember the Harness choice per browser connection, preview Claude / Codex themes, and inject the matching expert template on the first message of a new session.

## Features

- `application.background` shows brand background previews for Claude or Codex. Normal chat restores the host default look.
- `ops.appearance` applies controlled accent colors without injecting host page CSS.
- After a new session is created, only the **next** first `chat.send` receives `agent_template_name`.

## How it works

1. `gateway.request` handles inbound frames: mirror the AgentServer footer choice from `webui.dispatch`, arm the next first send after `session.create`, and rewrite eligible `chat.send` payloads once.
2. `gateway.disconnect` drops per-connection choice and arm state when the browser disconnects.
3. `ui.ready` on `application.background` restores the current connection preview and any persisted session theme.

Install alongside the AgentServer package `sample.harness-workbench` for the footer switch and session chrome.

## Advantages

- Theme preview works before and after the first-message bind.
- Session themes persist across reload and reopen.
- Sessions that already have a template or prior messages are left unchanged.

# Harness Workbench Bundle

Install both Harness Workbench system plugin members together.

## Members

- `sample.harness-workbench`: the sidebar Normal / Claude / Codex switcher, expert installation, and session chrome for bound sessions.
- `sample.harness-workbench.gateway`: per-connection Claude / Codex theme previews and one-shot expert template injection on the first message of a new session.

## How it works

1. The AgentServer member renders the sidebar switcher and session chrome, and mirrors the selection to the Gateway through `webui.dispatch`.
2. The Gateway member handles `gateway.request`, arms the next `chat.send` after `session.create`, and rewrites the template field once.
3. The Gateway member handles `ui.ready` and restores the current connection's theme preview in `application.background`.

## Notes

- Each member keeps its own package id and version, and is installed and loaded by its own host.
- The bundle only changes distribution; member UI and event behavior stay unchanged.

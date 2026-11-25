# Harness Workbench

Pick the Harness for your next task from the sidebar footer and render themed conversation chrome for bound Claude / Codex sessions.

## Features

- `application.sidebar.footer` offers Normal / Claude Code / Codex switching, including on the welcome view before any session exists.
- On activation, installs fixed experts `harness-claude-expert` and `harness-codex-expert`. Harness type conflicts do not overwrite existing experts; the switch stays available.
- Bound sessions show composer status, title badges, Claude tool stream with a right sandbox panel, or Codex activity cards. Normal chat clears Harness-only decoration.

## How it works

1. The footer or composer selector records the next-task Harness mode through `ui.click` / `ui.select`.
2. Conversation slots render on `ui.ready` according to the session binding.
3. `context.usage.updated` refreshes bound-session chrome after each turn.
4. `session.append` refreshes the activity area after transcript rows are committed.

For theme preview and first-message expert injection, install the Gateway package `sample.harness-workbench.gateway` alongside this AgentServer package.

## Advantages

- Set the next-task Harness before creating a session.
- Animated assets ship with static PNG fallbacks for reduced-motion environments.
- Uninstalling this package removes slot contributions but does not delete experts or historical bindings.

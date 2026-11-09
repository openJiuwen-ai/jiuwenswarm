# Desktop automation (cua_agent)

`cua_agent` is JiuwenSwarm's desktop automation (computer-use) subagent. It observes the
host through UI observations and **window screenshots**, and operates native application windows with **real
mouse and keyboard input**: Notepad, Office, IDEs, chat clients, system dialogs, settings
panels, and so on.

The subagent and legacy single-step tools live in agent-core
(`openjiuwen.harness.subagents.cua_agent` and `openjiuwen.harness.tools.cua`). Windows/macOS
desktop installers also embed the Computer Use SDK's persistent Python REPL through
JiuwenSwarm's `CuaExtensionRail`. Actual capabilities depend on the installed version and bundled driver.

> **Important:** input is real. While it runs, it moves your mouse and types into whatever
> window has keyboard focus. Do not use the machine during a task. On platforms with input
> detection, you can also enable `pause_on_user_input` (see section 5).

## 1. When to use it

| Target | Use |
|---|---|
| Web pages (URLs, websites, web apps, logging in to a site) | `browser_agent`, see [Browser tools](Browser.md) |
| Native windows on this machine (installed apps, system dialogs, the desktop) | `cua_agent` |
| Electron desktop apps such as Discord, Slack, VS Code, Spotify | `cua_agent` (they are native windows; the browser agent cannot reach them) |
| File or shell work that needs no GUI | The main agent's own tools |

When a task spans both (for example, copying a value from a website into a desktop app),
the main agent runs the web part with `browser_agent` first, then the desktop part with
`cua_agent`.

## 2. Prerequisites

- **OS and installation**: Windows, Linux on X11 (`DISPLAY` pointing at an X11 server),
  or macOS 14 and later. macOS requires a desktop installer containing the native driver.
  Official macOS packages support [Apple Silicon (ARM64) only](../../README.md), not Intel.
  Wayland is not supported on Linux.
- **Interactive desktop session**: a real display/desktop session is required; headless
  services, locked screens, or plain SSH sessions will not work.
- **The `cua` extra for Windows/Linux source or wheel installations (not installed by default)**: the desktop packages (`pyautogui`,
  `psutil`, plus `pygetwindow` on Windows and `python-xlib` on Linux) ship as an optional
  extra. Install it into the environment JiuwenSwarm runs in:

  ```bash
  pip install "workswarm[cua]"
  # or (repo/dev environment)
  uv sync --extra cua
  ```

  In a repo checkout, a plain `uv sync` removes these packages again; keep passing
  `--extra cua`. This does not enable the native REPL on macOS. The macOS desktop installer
  must include the Computer Use SDK and driver, and needs the Accessibility, Screen Recording,
  and other permissions requested by the OS.

- **A vision-capable (multimodal) model**: desktop tasks require understanding screenshots;
  do not rely on a text-only model. At startup JiuwenSwarm sends each model in
  `models.defaults` a small test image. When the current model is known to reject image
  input, `cua_agent` is not mounted and the main agent tells you to switch to a model that
  supports images. If the test was inconclusive, `cua_agent` is mounted as usual. To skip
  the test, declare the answer on the model entry with
  `model_client_config.supports_vision: true` (or `false`).

## 3. Enable

Use **Settings → Experimental → Computer control** when the backend's installation
prerequisites are present. Unsupported platforms, missing optional dependencies or
missing bundled driver files hide the section without changing saved preferences.
The check does not start a driver or request OS permissions; visibility does not imply permission grants.
The persisted `react.subagents.cua_agent.enabled` defaults to `null`, following the
release default (currently off). Only changing the switch saves `true` or `false`;
reading settings and upgrading preserve `null` and any explicit user preference.

Meet the installation and permission requirements for your platform first (see section 2).
Then edit `~/.jiuwenswarm/config/config.yaml` (template: `jiuwenswarm/resources/config.yaml`):

```yaml
react:
  subagents:
    cua_agent:
      enabled: true              # explicit opt-in; null follows the release default (currently off)
      # max_iterations: 100      # subagent iteration cap, default 100
      # snapshot_keep_last_k: 3  # screenshots kept in full in context, default 3
      pause_on_user_input: false # hold actions while you use mouse/keyboard, resume when idle
```

The same `enabled` flag applies to single-agent (deep) mode, code mode, and team (swarm)
members (provider `swarm.cua_agent`); it does not depend on the mode. Restart JiuwenSwarm
after changing it.

## 4. Usage

Describe the outcome you want on the desktop; you do not need to mention a subagent. Once
enabled, the main agent:

- delegates desktop work through the synchronous `task_tool` with `subagent_type`
  `"cua_agent"`, keeping your full objective in `task_description` rather than reducing it
  to steps;
- shows an "操作确认: cua_agent" card before each delegation, with the task and a note that
  the subagent may start apps (`launch_app`) and type text (`type_text`). The options are
  Allow once, Session allow, Always allow, and Reject; Always allow sets
  `require_approval` to `false`, so no session asks again (see section 5);
- asks you to confirm irreversible actions (sending messages, deleting files, submitting
  forms, payments) before they happen;
- does not script the GUI itself via Bash, PowerShell, pyautogui, AutoHotkey, etc.; if
  `cua_agent` is unavailable it says so.

### 4.1 Examples

- "Open Notepad, type hello, and save it to the Desktop as hello.txt."
- "Which windows are open right now?"
- "Compute 1234 × 5678 in Calculator and tell me the result."
- "Open Settings and switch to dark mode."
- "In VS Code, open the command palette and run Format Document."

### 4.2 How it works

The legacy single-step backend on Windows/Linux finds the target with `list_windows` / `list_apps`, calls `get_window_state`
to bring that window to the front and capture it, then clicks, types, or scrolls at pixel
coordinates read off the screenshot. After each input it captures again to verify the
effect, and it only reports completion when the result is visible on screen.

Legacy single-step tools (this backend is not available on macOS):

| Category | Tools |
|---|---|
| Observe (read-only) | `list_windows`, `list_apps`, `get_window_state`, `zoom`, `get_screen_size`, `get_cursor_position` |
| Input | `click`, `double_click`, `right_click`, `move_cursor`, `drag`, `scroll`, `type_text`, `press_key`, `hotkey` |
| Apps | `launch_app`, `bring_to_front` |

`kill_app` (force-terminate a process) is not in the default tool set.

Windows/macOS desktop installers also provide `cua_repl`, which runs Python with top-level
`await` through the Computer Use SDK. It preloads `computer`, `print`, and `print_image`,
and preserves variables within a delegation. macOS uses this entry point for desktop operations.
Windows can use either backend, but their references and screenshot coordinates must not be mixed.
The agent should await operations sequentially and read the UI to verify results; use
`print_image` to inspect screenshots. A successful tool result does not replace checking
task postconditions such as filenames and file counts.

### 4.3 Recovering an invalid REPL session

When the native session is retired (`Native session is retired`) or the SDK requests a
session reset, the agent can call `cua_repl_reset` with `{}`. This separate recovery tool
does not accept or execute task code.

- A successful reset confirms cleanup of the old execution session and clears Python variables
  and old UI references. Recreate application variables and observe the current UI before
  deciding the next action. Earlier clicks or input may already have taken effect; reset
  neither undoes nor automatically replays them.
- Ordinary Python errors such as `NameError` do not require a reset. Correct the code while
  preserving existing variables.
- If cleanup cannot be confirmed, further REPL execution is blocked. Only the recovery entry
  point owning that session can explicitly retry cleanup; another session cannot clear the
  blocker. Reset does not bypass task approval or macOS permissions.
- Reset applies only to an active delegation. It does not reopen a cancelled or closed runtime.
  Executing again requires a new delegation and confirmed cleanup of the old resources.

Reset repairs the execution session. It does not establish task completion or guarantee
that repeated actions without progress or model latency are resolved.

## 5. Configuration

| Key | Type | Default | Description |
|---|---|---|---|
| `react.subagents.cua_agent.enabled` | boolean / null | `null` (currently off) | Mount `cua_agent`; `null` follows the release default, while explicit `true` / `false` survives upgrades. |
| `react.subagents.cua_agent.max_iterations` | int | `100` | Subagent iteration cap. The capture-act-verify loop uses many iterations; avoid setting it too low. |
| `react.subagents.cua_agent.snapshot_keep_last_k` | int ≥ 1 | `3` | Number of recent screenshots kept in full in context. 3 suits tasks that move between two windows; `1` cuts context considerably for single-window tasks. Invalid values are ignored with a warning and the default applies. |
| `react.subagents.cua_agent.pause_on_user_input` | boolean | `false` | Hold desktop actions while you use the mouse/keyboard and resume after about 5 s idle. If you keep using the machine for about 60 s, the subagent ends the task and reports what it completed. Detection works on Windows and Linux X11 only; elsewhere the option has no effect. |
| `react.subagents.cua_agent.require_approval` | boolean | `true` | Show a confirmation card each time the main agent hands a task to `cua_agent` (also in full-access mode). Choosing "Always allow" on the card writes `false`, so no session asks again; set it back to `true` to be asked again. |

Environment variable:

| Variable | Default | Description |
|---|---|---|
| `OPENJIUWEN_SUBAGENT_TASK_TIMEOUT_S` | `720` | Timeout (seconds) for one `task_tool` delegation. Raise it for long desktop tasks; non-positive values are ignored. `cua_agent` wraps up about 20 s before the limit and returns a resumable result. |

## 6. Safety

- Do not use the mouse or keyboard during a run; your input and the subagent's interfere.
  On platforms with input detection, you can enable `pause_on_user_input`.
- **Stopping a task**: use the client's stop action. The legacy pyautogui single-step backend
  also has a screen-corner fail-safe. Native `cua_repl` does not use that mechanism; moving
  the pointer to a corner is not a stop switch for it.
- Close or minimize unrelated windows that show sensitive information; screenshots are sent
  to the model.
- The subagent can start local programs with `launch_app` and type into any focused window
  with `type_text`. Enable it only for trusted tasks and models.
- Check carefully when the main agent asks you to confirm an irreversible action.
- The delegation card is the last check before the subagent takes over your desktop. After
  Always allow, no session asks again; set `react.subagents.cua_agent.require_approval` back
  to `true` to restore it.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Main agent never calls `cua_agent`, or says it is unavailable | Not enabled | Set `react.subagents.cua_agent.enabled: true` and restart |
| Main agent says the desktop agent is unavailable because the model cannot read images | The current model rejected the startup image test, so `cua_agent` was not mounted | Switch to a model that supports image input, or set `supports_vision: true` on its entry if the test was wrong |
| `No module named 'pyautogui'` / `cua desktop preflight failed` | `cua` extra not installed, or removed by a plain `uv sync` | `pip install "workswarm[cua]"`, or `uv sync --extra cua` in a repo checkout, then restart |
| `no backend for platform 'darwin'` | A legacy single-step tool was called on macOS, or the installation lacks the native REPL | Use a macOS desktop installer with its driver and operate through `cua_repl`; the `cua` extra does not provide a macOS backend |
| `Native session is retired` / SDK requests `computer_use_reset` | Native admission or cleanup could not be confirmed | Call `cua_repl_reset` in WorkSwarm, then observe again; do not directly replay earlier actions (see 4.3) |
| Reset reports unconfirmed cleanup | The old worker or native resources have not been confirmed to exit | Keep REPL execution blocked and explicitly retry recovery through the same session; stop the task and inspect runtime logs if it keeps failing |
| `DISPLAY is not set` | No X11 display on Linux, or Wayland session | Run in an X11 desktop session with `DISPLAY` set |
| Window reported as minimized | Target window is minimized (position near -32000) | The subagent calls `bring_to_front` first; if it still fails, restore the window manually and retry |
| `could not bring window ... to the foreground` | OS refused the focus change (fullscreen app, system prompt) | Close the blocking fullscreen app or prompt and retry |
| Clicks miss, repeated actions have no effect | Weak vision model or very small targets | Use a stronger vision model; describe where the target is |
| Subagent cannot see the UI; delegation quickly ends with "Max iterations reached" | Model does not accept images, but the startup test was inconclusive so `cua_agent` was still mounted | Use a multimodal model |
| Delegation result has `cua_result.status: "partial"` with an "iteration limit" recovery hint | The run hit `max_iterations` before finishing | Resume the task, or raise `max_iterations` for long tasks |
| Task ends with a timeout | Hit the 720 s delegation ceiling | Set `OPENJIUWEN_SUBAGENT_TASK_TIMEOUT_S`, or split the task |

## 8. Known limitations

- Platform and installation restrictions in section 2 apply. macOS source/wheel installations
  do not provide the native REPL, and official macOS packages do not support Intel.
- There is one foreground window and one mouse/keyboard, so desktop tasks cannot run in
  parallel with your own use or with another desktop task.
- Stop or cancellation cannot undo input already sent. The REPL cleans up its execution
  session on cancellation; unconfirmed cleanup blocks further REPL execution. Before resuming,
  check the UI and the business steps already completed.

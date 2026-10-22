# Desktop automation (cua_agent)

`cua_agent` is JiuwenSwarm's desktop automation (computer-use) subagent. It observes the
host through **window screenshots** and operates native application windows with **real
mouse and keyboard input**: Notepad, Office, IDEs, chat clients, system dialogs, settings
panels, and so on.

The implementation lives in agent-core (`openjiuwen.harness.subagents.cua_agent` and
`openjiuwen.harness.tools.cua`); this page covers enabling, using, and troubleshooting it
in JiuwenSwarm. Actual behavior depends on the installed JiuwenSwarm and agent-core versions.

> **Important:** input is real. While it runs, it moves your mouse and types into whatever
> window has keyboard focus. Do not use the machine during a task, or enable
> `pause_on_user_input` (see section 5).

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

- **OS**: Windows, or Linux on X11 (`DISPLAY` pointing at an X11 server).
  **macOS and Wayland are not supported.**
- **Interactive desktop session**: a real display/desktop session is required; headless
  services, locked screens, or plain SSH sessions will not work.
- **The `cua` extra (not installed by default)**: the desktop packages (`pyautogui`,
  `psutil`, plus `pygetwindow` on Windows and `python-xlib` on Linux) ship as an optional
  extra. Install it into the environment JiuwenSwarm runs in:

  ```bash
  pip install "workswarm[cua]"
  # or (repo/dev environment)
  uv sync --extra cua
  ```

  In a repo checkout, a plain `uv sync` removes these packages again; keep passing
  `--extra cua`. Do not install it on macOS: there is no macOS backend.

- **A vision-capable (multimodal) model**: perception is screenshots only; a text-only
  model cannot complete desktop tasks. At startup JiuwenSwarm sends each model in
  `models.defaults` a small test image. When the current model is known to reject image
  input, `cua_agent` is not mounted and the main agent tells you to switch to a model that
  supports images. If the test was inconclusive, `cua_agent` is mounted as usual. To skip
  the test, declare the answer on the model entry with
  `model_client_config.supports_vision: true` (or `false`).

## 3. Enable

Install the `cua` extra first (see section 2); enabling `cua_agent` without it fails on
the first desktop task. Then edit `~/.jiuwenswarm/config/config.yaml` (template: `jiuwenswarm/resources/config.yaml`):

```yaml
react:
  subagents:
    cua_agent:
      enabled: true              # default false; explicit opt-in
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

The subagent finds the target with `list_windows` / `list_apps`, calls `get_window_state`
to bring that window to the front and capture it, then clicks, types, or scrolls at pixel
coordinates read off the screenshot. After each input it captures again to verify the
effect, and it only reports completion when the result is visible on screen.

Tools available by default:

| Category | Tools |
|---|---|
| Observe (read-only) | `list_windows`, `list_apps`, `get_window_state`, `zoom`, `get_screen_size`, `get_cursor_position` |
| Input | `click`, `double_click`, `right_click`, `move_cursor`, `drag`, `scroll`, `type_text`, `press_key`, `hotkey` |
| Apps | `launch_app`, `bring_to_front` |

`kill_app` (force-terminate a process) is not in the default tool set; `cua_agent` does not
force-kill applications.

## 5. Configuration

| Key | Type | Default | Description |
|---|---|---|---|
| `react.subagents.cua_agent.enabled` | boolean | `false` | Mount `cua_agent`. |
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
  Enable `pause_on_user_input` if you need to share the machine.
- **Emergency stop**: fling the mouse into any screen corner. This trips pyautogui's
  fail-safe and aborts the subagent's next action.
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
| `no backend for platform 'darwin'` | Running on macOS | macOS is not supported |
| `DISPLAY is not set` | No X11 display on Linux, or Wayland session | Run in an X11 desktop session with `DISPLAY` set |
| Window reported as minimized | Target window is minimized (position near -32000) | The subagent calls `bring_to_front` first; if it still fails, restore the window manually and retry |
| `could not bring window ... to the foreground` | OS refused the focus change (fullscreen app, system prompt) | Close the blocking fullscreen app or prompt and retry |
| Clicks miss, repeated actions have no effect | Weak vision model or very small targets | Use a stronger vision model; describe where the target is |
| Subagent cannot see the UI; delegation quickly ends with "Max iterations reached" | Model does not accept images, but the startup test was inconclusive so `cua_agent` was still mounted | Use a multimodal model |
| Delegation result has `cua_result.status: "partial"` with an "iteration limit" recovery hint | The run hit `max_iterations` before finishing | Resume the task, or raise `max_iterations` for long tasks |
| Task ends with a timeout | Hit the 720 s delegation ceiling | Set `OPENJIUWEN_SUBAGENT_TASK_TIMEOUT_S`, or split the task |

## 8. Known limitations

- Windows and Linux X11 only.
- There is one foreground window and one mouse/keyboard, so desktop tasks cannot run in
  parallel with your own use or with another desktop task.
- Stopping a run cancels the main flow immediately, but an in-flight `cua_agent` model call
  may still complete and keep acting until its iteration cap (same as `browser_agent`).
  Parking the mouse in a screen corner meanwhile makes each of its input actions fail via
  the fail-safe.

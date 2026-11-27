## CLI / channel control commands

JiuwenClaw supports **special prefix commands** to control sessions and modes. Common ones:

- `/new_session`: start a new `session_id` for the current channel
- `/mode plan`, `/mode fast`, or `/mode team`: switch the channel’s working mode
- `/ls [path]`: list files/directories under the current session workspace
- `/view <path> [-f N] [-l N|-n N]`: view file content in the current session workspace (with line range support)

These are handled in the Gateway **`MessageHandler`** and **are not** sent to the agent.

---

### 1. `/new_session` — new session id

**Behavior**

- For supported channels (`feishu` / `xiaoyi` / `dingtalk`), generates a new `session_id`, e.g.:  
  - `feishu_<ms hex>_<random hex>`
  - `xiaoyi_<ms hex>_<random hex>`
  - `dingtalk_<ms hex>_<random hex>`
- Later messages on that channel use this id, so a new folder appears under `workspace/session/`.

**Usage**

Send in the channel (Feishu / Xiaoyi / DingTalk):

  ```text
  /new_session
  ```
![](../assets/images/命令行解析.jpg)

The Gateway will:

  1. Intercept (not forwarded to the agent)
  2. Generate a new `session_id` for that `channel_id`
  3. Reply with a system message, e.g.  
     `session_id updated to feishu_17f2b4b32e0_ab12cd`

**Notes**

- `/new_session` only changes **future** message binding; the directory is created when the session is actually used (todo, files, etc.).

---

### 2. `/mode` — channel mode (`plan` / `fast` / `team`)

**Behavior**

- Sets a logical **mode** for the channel:
  - `plan`: planning, explanation, decomposition (default)
  - `fast`: more hands-on execution (same internal semantics as the historical `agent` mode)
  - `team`: team mode
- Mode is passed in `params["mode"]` for prompt construction.

**Usage**

  ```text
  /mode plan
  ```

  or

  ```text
  /mode fast
  ```

The Gateway will:

  1. Treat as control, not forward to the agent
  2. Update `ChannelControlState.mode`
  3. Reply e.g. `mode updated to fast`

**Scope**

- Stored **per channel** (`channel_id` → `mode`). All later messages on that channel use the current mode.
- Initial value can come from `default_mode` in config; `MessageHandler` reads it on startup.

---

### 3. `/ls` — list current session workspace directory

**Behavior**

- Lists files/subdirectories under the workspace root resolved for the current session (`session_id`).
- Useful for quickly checking session artifacts in IM controlled channels (generated outputs, logs, drafts, etc.).

**Usage**

- List current directory:

  ```text
  /ls
  ```

- List a subdirectory:

  ```text
  /ls outputs
  ```

**Response**

- On success, returns directory entries (directories first, then files).
- On failure, returns an explicit error message (e.g., invalid path, out-of-workspace access).

**Security boundary**

- Access is restricted to the current `session_id` workspace.
- Path traversal outside workspace (for example `../`) is rejected with `Path outside workspace`.

---

### 4. `/view` — view current session workspace file content

**Behavior**

- Displays file content as text from the current session workspace.
- Designed for quick debugging/verification in channel without switching tools.

**Usage**

- View full file:

  ```text
  /view output/result.md
  ```

- Start from a specific line:

  ```text
  /view output/result.md -f 120
  ```

- Limit number of lines (`-l` and `-n` are equivalent):

  ```text
  /view output/result.md -f 120 -l 80
  ```

**Arguments**

- `-f N`: starting line number (default `1`).
- `-l N` / `-n N`: number of lines to read; if omitted, reads to EOF.

**Limits and errors**

- Only text files inside workspace are supported.
- Directories, binary files, and out-of-workspace paths return explicit errors.

---

### 5. TUI: `/workspace_dir` — workspace path for outbound requests

**Scope:** terminal UI (`jiuwenclaw-cli`) only; parsed locally, not by the Gateway control pipeline.

**Behavior**

- **`/workspace_dir`** or **`/workspace_dir get`**: show the saved workspace directory (if any).
- **`/workspace_dir set <path>`**: save a path (spaces allowed). Example: `/workspace_dir set C:\Projects\my-app`
- **`/workspace_dir clear`**: clear the saved value.
- Alias: **`/workspace-dir`**.

**Persistence**

- Stored as a single-line file: **`~/.jiuwenclaw/tui-workspace-dir`**.

**Gateway / Agent**

- When a non-empty path is set, TUI includes **`workspace_dir`** in the WebSocket **`params`** for fire-and-forget requests built by `sendEventOnly` (e.g. `chat.send`), so Gateway and AgentServer can read it from `Message.params` / `AgentRequest.params`. Downstream usage depends on the agent and extensions.


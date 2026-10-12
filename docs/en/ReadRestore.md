# Read restore (Code mode)

`ReadRestoreRail` stops `edit_file` / `write_file` being refused with "File must be read before editing" when a Code session continues in a new process.

## 1. Why

agent-core lets the agent edit a file only after `read_file` has read it, and it keeps that record in process memory. A Code session that continues in another process loses every read:

* **The headless implement driver** (`jiuwenswarm-process --implement`) starts a fresh process for each continuation round. In 20-task SWE-bench Pro runs, `edit_file` was refused 33 to 80 times per run, and over 95% of those refusals were for files the agent had read in an earlier round. Each refusal cost a re-read and a retry (50–67 extra model calls, 3.7M–5.6M input tokens, 6.5–9% of all input per run). Up to 19 times per run the round ended before the retry, and up to 10 times the model gave up on `edit_file` and changed the file through the shell (`sed -i`, a Python write), which the edit tools, mutation tracking and file-path hooks do not see.
* **The desktop and other channels** hit the same refusal when a session is resumed after the AgentServer restarts.

The driver already tells the model to call `read_file` first, and the `edit_file` description and the Code prompt say so too; the refusals happened anyway.

## 2. How it works

* **Recording.** After every successful `read_file`, `edit_file` or `write_file`, the rail records the file for the current session (`session.get_session_id()`): its size, modification time, SHA-256 and line count, and for reads the line ranges that were read. Records live in `<agent root>/read_restore/<session>.json` (by default `~/.jiuwenswarm/agent/read_restore/`), never inside a project. At most 400 files per session and 500 session files are kept; the oldest are dropped first.
* **Restoring.** Before an `edit_file` or `write_file` on an existing file that agent-core has not seen in this process, the rail looks up this session's record. If the file still has exactly the content the agent saw (same size and hash), the rail calls the agent's own registered `read_file` tool, so agent-core records the read itself and the edit goes ahead. For `edit_file` one line is read (agent-core accepts any read for an edit). For `write_file` the whole file is read in 500-line windows, and only if the session had read the whole file through `read_file`: agent-core only lets `write_file` overwrite a fully read file, and an edit or write does not count as a full read in agent-core either.
* **Not restored**, so agent-core's refusal stands and the model reads the file as before:
  * the file changed since the session saw it (another process, the user, a shell command);
  * this session never read, edited or wrote it (another session's records are never used);
  * the read policy now denies or asks for it: the deny-only project policy (`.jiuwenswarm/permissions.yaml`, when that package is installed) and, when `permissions.enabled` is true, the permission engine's `read_file` decision for the path (rules and file guard). A policy error also blocks the restore;
  * the file is larger than 8 MiB, or is a PDF, notebook or image;
  * the call was already refused by an earlier rail.

The rail never returns file content to the model and never changes agent-core: the restore is an ordinary call of agent-core's public `read_file` tool, with the agent's own `SysOperation`.

## 3. Configuration

```yaml
modes:
  code:
    read_restore:
      enabled: true   # false = never record or restore (agent-core's behaviour)
```

The switch is read on every call, so a configuration reload applies without rebuilding the agent.

## 4. How to confirm it is working

Each restore logs at INFO:

```
[ReadRestore] restored read state of /path/to/file.py for edit_file (session <id> saw it earlier)
```

and a skipped restore because the file changed logs `[ReadRestore] not restoring … changed since session … saw it`.

## 5. Limits

* agent-core's read registry is shared by every session in one process. In a long-running AgentServer, session B can already edit a file that only session A read; this rail does not change that (it is agent-core's behaviour and is reported upstream).
* A file that changed and was changed back to the same bytes counts as unchanged.
* Restoring is per session. A new session (for example a new desktop conversation on the same project) starts without records, as before.

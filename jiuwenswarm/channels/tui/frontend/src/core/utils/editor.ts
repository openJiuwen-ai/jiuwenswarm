import { spawnSync, type SpawnSyncOptions, type SpawnSyncReturns } from "node:child_process";
import { basename } from "node:path";
import { existsSync, mkdirSync } from "node:fs";
import type { TUI } from "@mariozechner/pi-tui";

const GUI_EDITORS = [
  "code",
  "cursor",
  "windsurf",
  "codium",
  "subl",
  "atom",
  "notepad",
  "notepad++",
  "gedit",
  "kate",
  "mousepad",
];

const GUI_EDITOR_WAIT_FLAGS: Record<string, string[]> = {
  code: ["-w"],
  cursor: ["-w"],
  windsurf: ["-w"],
  codium: ["-w"],
  subl: ["--wait"],
  atom: ["--wait"],
};

export function getExternalEditor(): string {
  if (process.env.VISUAL?.trim()) return process.env.VISUAL.trim();
  if (process.env.EDITOR?.trim()) return process.env.EDITOR.trim();
  if (process.platform === "win32") return "start /wait notepad";
  return "vi";
}

export function getEditorInfo(): { source: string; value: string } {
  if (process.env.VISUAL) return { source: "$VISUAL", value: process.env.VISUAL };
  if (process.env.EDITOR) return { source: "$EDITOR", value: process.env.EDITOR };
  return {
    source: "default",
    value: process.platform === "win32" ? "start /wait notepad" : "vi",
  };
}

export function isGuiEditor(editor: string): boolean {
  // Check all parts of the command, not just the first word.
  // On Windows, the default editor is "start /wait notepad" — the actual
  // editor name ("notepad") is in the arguments, not the command itself.
  return editor.split(/\s+/).some((token) => {
    if (!token) return false;
    const base = basename(token);
    return GUI_EDITORS.some((gui) => base.includes(gui));
  });
}

export function parseEditorCommand(editor: string): { cmd: string; args: string[] } {
  const parts = editor.split(/\s+/);
  const cmd = parts[0];
  const baseArgs = parts.slice(1);

  const waitArgs = GUI_EDITOR_WAIT_FLAGS[cmd];
  if (waitArgs && !baseArgs.some((a) => waitArgs.includes(a))) {
    return { cmd, args: [...waitArgs, ...baseArgs] };
  }

  return { cmd, args: baseArgs };
}

function spawnFailed(result: SpawnSyncReturns<string | Buffer>): boolean {
  return result.status !== 0 || result.error != null;
}

/**
 * Open a file in the user's external editor.
 *
 * GUI editors (notepad, VS Code, Sublime, etc.) AND terminal editors both
 * use a synchronous blocking spawn (spawnSync). The TUI is stopped first —
 * tui.stop() removes stdin 'data' listeners and pauses stdin, so the TUI
 * is non-operable (frozen) while the editor holds the file open. This
 * mirrors Claude Code's editFileInEditor, which pauses Ink + suspends stdin
 * and runs execSync until the editor window closes.
 *
 * Why synchronous (not the old detached-async spawn): GUI editor launchers
 * like Windows code.cmd exit immediately when run WITHOUT a wait flag, so
 * a detached spawn's child.on('exit') fires while the editor window is still
 * open — the freeze is released prematurely (the bug with editor=code).
 * Forcing -w/--wait (EDITOR_OVERRIDES, see spawnGuiEditorSync) plus a
 * synchronous spawnSync makes onExit fire only after the window actually
 * closes, exactly like CC's `code -w` + execSync.
 *
 * @param onExit Called synchronously after the editor exits and tui.start()
 *               has restored input. Runs for both GUI and terminal editors.
 */
export function openFileInEditor(tui: TUI, filePath: string, onExit?: () => void): void {
  const editor = getExternalEditor();
  const gui = isGuiEditor(editor);

  if (gui) {
    spawnGuiEditorSync(tui, editor, filePath, onExit);
    return;
  }

  // Terminal editor: spawnSync + tui.stop/start (blocks until editor exits)
  const { cmd, args } = parseEditorCommand(editor);

  tui.stop();

  try {
    // Enter alt screen + clear + show cursor.
    // The editor (vim/nano) will manage its own alt screen on top of ours.
    process.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?25h");

    if (process.stdin.setRawMode) {
      process.stdin.setRawMode(false);
    }
    process.stdin.resume();

    const result = spawnEditorSync(cmd, args, filePath);
    if (spawnFailed(result)) {
      spawnFallbackSync(filePath);
    }
  } finally {
    // ── Terminal recovery (mirrors claude-code's exitAlternateScreen) ──
    //
    // Terminal editors (vim, nano, less) write smcup/rmcup (?1049h/?1049l).
    // On exit, vim's rmcup drops us back to the MAIN screen — our alt screen
    // is already gone. Simply writing ?1049l is a no-op here.
    //
    // We re-enter alt screen → clear → exit alt screen, which gives a clean
    // main screen without wiping the user's scrollback. This is the key fix
    // for the "Win10 + git bash + vim → TUI stuck" bug.
    process.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?1049l\x1b[?25l");

    // Drain any buffered stdin data left by the editor process.
    // vim may leave escape sequences in the stdin buffer; if these reach
    // tui.start()'s Kitty protocol query handler, they can confuse input
    // parsing and cause the TUI to appear frozen.
    try {
      while (process.stdin.read() !== null) {
        // discard all buffered data
      }
    } catch {
      // stdin not readable or already destroyed — ignore
    }

    tui.start();
    tui.requestRender(true);
    onExit?.();
  }
}

// ---------------------------------------------------------------------------
// GUI editor: synchronous spawn (blocks until editor window closes)
// ---------------------------------------------------------------------------

/**
 * Spawn a GUI editor SYNCHRONOUSLY (blocking), mirroring Claude Code's
 * editFileInEditor (execSync + Ink pause/suspendStdin).
 *
 * The editor runs in its own window/process. Before spawning we call
 * tui.stop(), which removes stdin 'data' listeners and pauses stdin — so
 * the TUI is non-operable (frozen) for as long as spawnSync blocks. The
 * editor window closing is what unblocks spawnSync, so onExit fires exactly
 * when the user is done editing. This is the fix for editor=code on Windows:
 * code.cmd exits immediately without -w, so a detached-async spawn released
 * the freeze prematurely; the synchronous path with the forced wait flag
 * does not.
 *
 * Unlike terminal editors, GUI editors do NOT switch to the alt screen — the
 * editor opens in a separate window, so there is nothing for us to hand the
 * terminal off to. We only stop/restart stdin around the spawn, exactly as
 * CC only pauses Ink + suspends stdin for GUI editors.
 *
 * Wait flags (EDITOR_OVERRIDES equivalent): for editors whose launcher exits
 * immediately without a wait flag (code/cursor/windsurf/codium → -w,
 * subl/atom → --wait), parseEditorCommand prepends the wait flag from
 * GUI_EDITOR_WAIT_FLAGS unless the user already supplied it. This matches
 * CC's EDITOR_OVERRIDES = { code: 'code -w', subl: 'subl --wait' }.
 */
function spawnGuiEditorSync(tui: TUI, editor: string, filePath: string, onExit?: () => void): void {
  // Resolve cmd + args, forcing a wait flag for editors that need one.
  const { cmd, args } = parseEditorCommand(editor);

  tui.stop();

  try {
    const result = spawnEditorSync(cmd, args, filePath);
    if (spawnFailed(result)) {
      // GUI editor failed to launch — fall back to notepad/vi (still blocking).
      spawnFallbackSync(filePath);
    }
  } finally {
    // Drain any stdin the editor may have left buffered (defensive — GUI
    // editors shouldn't touch our stdin, but a shell-wrapped one might).
    try {
      while (process.stdin.read() !== null) {
        // discard buffered data
      }
    } catch {
      // stdin not readable — ignore
    }

    tui.start();
    tui.requestRender(true);
    onExit?.();
  }
}

// ---------------------------------------------------------------------------
// Terminal editor: synchronous spawn (blocking)
// ---------------------------------------------------------------------------

function spawnEditorSync(cmd: string, args: string[], filePath: string): SpawnSyncReturns<string | Buffer> {
  const spawnOptions: SpawnSyncOptions = { stdio: "inherit" };
  const fullArgs = [...args, filePath];

  if (process.platform === "win32") {
    if (cmd === "start") {
      const waitFlag = fullArgs[0] === "/wait" ? "/wait " : "";
      const programArgs = waitFlag ? fullArgs.slice(1) : fullArgs;
      const quoted = programArgs.map((a) => `"${a}"`).join(" ");
      return spawnSync(`start ${waitFlag}"" ${quoted}`, { ...spawnOptions, shell: true });
    }
    const quoted = fullArgs.map((a) => `"${a}"`).join(" ");
    return spawnSync(`${cmd} ${quoted}`, { ...spawnOptions, shell: true });
  }

  return spawnSync(cmd, fullArgs, spawnOptions);
}

function spawnFallbackSync(filePath: string): SpawnSyncReturns<string | Buffer> {
  const spawnOptions: SpawnSyncOptions = { stdio: "inherit" };

  if (process.platform === "win32") {
    return spawnSync(`start /wait "" notepad "${filePath}"`, { ...spawnOptions, shell: true });
  }

  return spawnSync("vi", [filePath], spawnOptions);
}

// ---------------------------------------------------------------------------
// Folder opening (file explorer)
// ---------------------------------------------------------------------------

/**
 * Open a folder in the system file explorer (not an editor).
 * - Windows: explorer
 * - macOS: open -R (reveals in Finder)
 * - Linux: xdg-open (requires a GUI/Display; falls back gracefully on headless servers)
 *
 * Returns true if an explorer was (likely) launched, false if the platform
 * has no GUI explorer available (e.g. a headless Linux server). Callers can
 * use the false return to show a copyable path hint instead.
 */
export function openFolderInExplorer(folderPath: string): boolean {
  const spawnOptions: SpawnSyncOptions = { stdio: "inherit" };

  // Ensure folder exists before opening (explorer opens Documents if path doesn't exist)
  if (!existsSync(folderPath)) {
    try {
      mkdirSync(folderPath, { recursive: true });
    } catch {
      // Ignore errors - just try to open anyway
    }
  }

  if (process.platform === "win32") {
    spawnSync(`explorer "${folderPath}"`, { ...spawnOptions, shell: true });
    return true;
  } else if (process.platform === "darwin") {
    spawnSync("open", ["-R", folderPath], spawnOptions);
    return true;
  } else {
    // Headless Linux server: no xdg-open, no DISPLAY → don't block on a
    // spawnSync that will just hang or error out. Tell the caller to fall
    // back to a path hint.
    const hasDisplay = !!process.env.DISPLAY || !!process.env.WAYLAND_DISPLAY;
    if (!hasDisplay) {
      return false;
    }
    try {
      const res = spawnSync("xdg-open", [folderPath], spawnOptions);
      return res.status === 0 || res.status === null;
    } catch {
      return false;
    }
  }
}

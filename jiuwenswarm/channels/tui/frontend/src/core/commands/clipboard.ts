import { spawn } from "node:child_process";
import { writeFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

/**
 * Async clipboard helper using spawn — never blocks the event loop.
 * Pipes `text` to the child's stdin, ignores stdout/stderr.
 * Resolves to true on exit code 0, false on any error/timeout/non-zero exit.
 */
function tryClipboardAsync(
  command: string,
  args: string[],
  text: string,
  timeout = 5000,
): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;

    const child = spawn(command, args, {
      stdio: ["pipe", "ignore", "ignore"],
    });

    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        child.kill();
        resolve(false);
      }
    }, timeout);

    child.stdin.write(text, () => {
      child.stdin.end();
    });

    child.on("exit", (code) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve(code === 0);
      }
    });

    child.on("error", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve(false);
      }
    });
  });
}

/**
 * Async clipboard helper for commands that don't need stdin input.
 * Used by PowerShell file-based approach (no stdin needed).
 */
function tryCommandAsync(
  command: string,
  args: string[],
  timeout = 5000,
): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;

    const child = spawn(command, args, {
      stdio: ["ignore", "ignore", "ignore"],
    });

    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        child.kill();
        resolve(false);
      }
    }, timeout);

    child.on("exit", (code) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve(code === 0);
      }
    });

    child.on("error", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve(false);
      }
    });
  });
}

/**
 * Windows UTF-8 clipboard via PowerShell.
 *
 * Strategy:
 *  1. Write text to a uniquely-named temp file (Node.js fs, guaranteed UTF-8)
 *  2. Run a simple PowerShell command to read the file and pipe to Set-Clipboard
 *  3. Retry up to `retries` times on transient failures (clipboard locked etc.)
 *  4. Always clean up the temp file in the end
 *
 * This replaces the old approach that:
 *  - Used execFileSync (synchronous, blocks the event loop for 1-5s)
 *  - Had no retry logic (any transient failure = immediate `false`)
 *  - Used a fixed temp file name (clip-tmp.txt) risking conflicts
 *  - Ran a complex inline PowerShell base64-decode→temp→read→clip→delete script
 */
async function tryClipboardUtf8OnWindowsAsync(
  text: string,
  retries = 2,
): Promise<boolean> {
  // Unique temp file per call to avoid conflicts between successive /export invocations
  const tmpId = `${process.pid}-${Date.now()}`;
  const tmpFile = join(tmpdir(), `jiuwenclip-${tmpId}.txt`);

  // Step 1: Write text to temp file using Node.js — guaranteed UTF-8, no PowerShell encoding dance
  try {
    writeFileSync(tmpFile, text, { encoding: "utf-8" });
  } catch {
    // Can't even write the temp file — disk issue, bail out
    return false;
  }

  // Step 2: PowerShell reads the temp file and sets clipboard
  // Use -LiteralPath to safely handle any special chars in the path
  const psCommand = `Get-Content -LiteralPath '${tmpFile}' -Encoding UTF8 -Raw | Set-Clipboard`;

  for (let attempt = 0; attempt <= retries; attempt++) {
    const ok = await tryCommandAsync(
      "powershell",
      ["-NoProfile", "-NonInteractive", "-Command", psCommand],
      5000,
    );

    if (ok) {
      // Success — clean up temp file and return
      try {
        unlinkSync(tmpFile);
      } catch {}
      return true;
    }

    // Transient failure (clipboard may be locked by another app) — wait before retrying
    if (attempt < retries) {
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }

  // All retries exhausted — clean up temp file
  try {
    unlinkSync(tmpFile);
  } catch {}
  return false;
}

function isTmux(): boolean {
  return !!process.env.TMUX;
}

function isScreen(): boolean {
  return !!process.env.STYLE;
}

/**
 * OSC 52 clipboard escape sequence.
 *
 * Works on terminals that support it (iTerm2, Windows Terminal, Kitty, etc).
 * For tmux/screen, wraps in DCS passthrough so the sequence reaches the outer terminal.
 *
 * Returns true if the sequence was written to stdout (best-effort; terminal
 * may silently ignore it). This is a synchronous fire-and-forget fallback.
 */
function tryOsc52(text: string): boolean {
  try {
    const base64 = Buffer.from(text, "utf-8").toString("base64");
    let osc52 = `\x1b]52;c;${base64}\x07`;
    if (isTmux()) {
      osc52 = `\x1bPtmux;${osc52}\x1b\\`;
    } else if (isScreen()) {
      osc52 = `\x1bP${osc52}\x1b\\`;
    }
    process.stdout.write(osc52);
    return true;
  } catch {
    return false;
  }
}

/**
 * Copy text to the system clipboard.
 *
 * Async version — does NOT block the Node.js event loop.
 * Multi-layered fallback strategy inspired by Claude Code's setClipboard:
 *
 *  macOS:  pbcopy (fast, reliable, UTF-8 native)
 *  Windows: PowerShell UTF-8 (primary, with retry) → clip.exe (fallback) → OSC52 (final)
 *  Linux:  wl-copy (Wayland) → xclip → xsel → OSC52
 *
 * The Windows fallback chain ensures that even if PowerShell intermittently
 * fails (clipboard locked by another app, slow startup), the user still
 * gets their content copied via clip.exe or OSC52.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;

  // macOS — pbcopy is native, fast, and handles UTF-8 correctly
  if (process.platform === "darwin") {
    return tryClipboardAsync("pbcopy", [], text);
  }

  // Windows — multi-layered fallback
  if (process.platform === "win32") {
    // Primary: PowerShell with retry (UTF-8 correct for Chinese text)
    if (await tryClipboardUtf8OnWindowsAsync(text)) {
      return true;
    }
    // Fallback 1: clip.exe — always available, very fast, but encoding is
    // imperfect (uses system locale; may mangle non-ASCII on older Windows).
    // Still better than failing entirely.
    if (await tryClipboardAsync("clip", [], text)) {
      return true;
    }
    // Fallback 2: OSC 52 escape sequence — works on Windows Terminal and
    // other modern terminals. Best-effort, no process spawn needed.
    return tryOsc52(text);
  }

  // Linux — Wayland first (modern), then X11, then OSC52
  if (process.env.WAYLAND_DISPLAY) {
    if (await tryClipboardAsync("wl-copy", [], text)) return true;
  }

  if (process.env.DISPLAY) {
    if (await tryClipboardAsync("xclip", ["-selection", "clipboard"], text)) return true;
    if (await tryClipboardAsync("xsel", ["--clipboard", "--input"], text)) return true;
  }

  // Final fallback everywhere: OSC52
  return tryOsc52(text);
}
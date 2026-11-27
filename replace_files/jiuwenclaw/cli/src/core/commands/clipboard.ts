import { execFileSync } from "node:child_process";

function tryClipboard(command: string, args: string[], text: string): boolean {
  try {
    execFileSync(command, args, { input: text, stdio: ["pipe", "ignore", "ignore"] });
    return true;
  } catch {
    return false;
  }
}

export function copyToClipboard(text: string): boolean {
  if (!text) return false;

  if (process.platform === "darwin") {
    return tryClipboard("pbcopy", [], text);
  }

  if (process.platform === "win32") {
    return tryClipboard("clip", [], text);
  }

  if (process.env.WAYLAND_DISPLAY && tryClipboard("wl-copy", [], text)) {
    return true;
  }

  if (process.env.DISPLAY && tryClipboard("xclip", ["-selection", "clipboard"], text)) {
    return true;
  }

  if (process.env.DISPLAY && tryClipboard("xsel", ["--clipboard", "--input"], text)) {
    return true;
  }

  return false;
}

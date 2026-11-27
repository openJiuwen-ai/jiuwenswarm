import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const STATE_DIR = join(homedir(), ".jiuwenclaw");
const STATE_FILE = join(STATE_DIR, "tui-workspace-dir");

export function loadTuiWorkspaceDir(): string {
  try {
    if (!existsSync(STATE_FILE)) {
      return "";
    }
    return readFileSync(STATE_FILE, "utf8").trim();
  } catch {
    return "";
  }
}

export function saveTuiWorkspaceDir(value: string): void {
  mkdirSync(STATE_DIR, { recursive: true });
  const trimmed = value.trim();
  writeFileSync(STATE_FILE, trimmed ? `${trimmed}\n` : "", "utf8");
}

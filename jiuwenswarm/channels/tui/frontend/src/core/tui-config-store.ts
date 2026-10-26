import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import type { ThemeName } from "../ui/theme.js";

const CONFIG_DIR = join(homedir(), ".jiuwenswarm-tui");
const CONFIG_FILE = join(CONFIG_DIR, "config.json");

export interface StatusLineSetting {
  type: "command";
  command: string;
  padding?: number;
}

export interface TuiConfig {
  theme?: ThemeName;
  /**
   * Project-scoped trusted directories.
   * Key = normalized project cwd path, value = list of trusted dir paths for that project.
   * Legacy flat array (string[]) is migrated on first load.
   */
  trustedDirs?: Record<string, string[]> | string[];
  statusLine?: StatusLineSetting;
}

export function loadTuiConfig(): TuiConfig {
  try {
    if (!existsSync(CONFIG_FILE)) {
      mkdirSync(CONFIG_DIR, { recursive: true });
      writeFileSync(CONFIG_FILE, "{}\n", "utf8");
      return {};
    }
    const raw = readFileSync(CONFIG_FILE, "utf8").trim();
    if (!raw) {
      return {};
    }
    return JSON.parse(raw) as TuiConfig;
  } catch (error) {
    // 仅在文件存在且错误为 SyntaxError 时记录（JSON 解析失败）
    if (error instanceof SyntaxError && _configParseError === null) {
      _configParseError = error.message;
    }
    return {};
  }
}

export function saveTuiConfig(partial: TuiConfig): void {
  mkdirSync(CONFIG_DIR, { recursive: true });
  const existing = loadTuiConfig();
  const merged = { ...existing, ...partial };
  writeFileSync(CONFIG_FILE, JSON.stringify(merged, null, 2) + "\n", "utf8");
}

/** 如果 config.json 包含无效 JSON，保存 JSON 解析错误信息。 */
let _configParseError: string | null = null;

/**
 * 消费并清除 config 文件解析错误信息。
 * 如果没有检测到解析错误或错误已被消费，则返回 null。
 * 供 AppScreen 在启动时用于显示一次性通知。
 */
export function consumeParseError(): string | null {
  const msg = _configParseError;
  _configParseError = null;
  return msg;
}

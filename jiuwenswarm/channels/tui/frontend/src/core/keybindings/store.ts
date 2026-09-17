import { existsSync, mkdirSync, readFileSync, statSync, watch } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import {
  isKeybindingAction,
  isKeybindingContext,
  type KeybindingAction,
} from "./actions.js";
import { DEFAULT_BINDINGS } from "./defaultBindings.js";
import { isReservedKey, validateKeyId } from "./reserved.js";
import type {
  KeybindingBlock,
  KeybindingWarning,
  KeybindingsFile,
  LoadResult,
  ResolvedBindings,
} from "./types.js";

const CONFIG_DIR = join(homedir(), ".jiuwenswarm-tui");
const KEYBINDINGS_FILE = join(CONFIG_DIR, "keybindings.json");

export function getKeybindingsPath(): string {
  return KEYBINDINGS_FILE;
}

export function keybindingsFileExists(): boolean {
  return existsSync(KEYBINDINGS_FILE);
}

export function getKeybindingsMtimeMs(): number | undefined {
  try {
    return statSync(KEYBINDINGS_FILE).mtimeMs;
  } catch {
    return undefined;
  }
}

/** Build a resolved map from a list of blocks (no validation). */
function buildResolved(blocks: KeybindingBlock[]): ResolvedBindings {
  const resolved: ResolvedBindings = new Map();
  for (const block of blocks) {
    if (!isKeybindingContext(block.context)) continue;
    let ctxMap = resolved.get(block.context);
    if (!ctxMap) {
      ctxMap = new Map<string, KeybindingAction>();
      resolved.set(block.context, ctxMap);
    }
    for (const [key, action] of Object.entries(block.bindings)) {
      if (action === null) {
        ctxMap.delete(key);
        continue;
      }
      if (isKeybindingAction(action)) {
        ctxMap.set(key, action);
      }
    }
  }
  return resolved;
}

/**
 * Apply a user block on top of an already-resolved map, collecting warnings.
 * Returns true if the block contains duplicate keys (fatal — the entire file
 * should be rejected).
 */
function applyUserBlock(
  resolved: ResolvedBindings,
  block: KeybindingBlock,
  warnings: KeybindingWarning[],
): boolean {
  let hasDuplicate = false;
  if (!isKeybindingContext(block.context)) {
    warnings.push({ context: String(block.context), message: `未知 context："${block.context}"` });
    return false;
  }
  if (typeof block.bindings !== "object" || block.bindings === null) {
    warnings.push({ context: block.context, message: "bindings 必须是对象" });
    return false;
  }
  let ctxMap = resolved.get(block.context);
  if (!ctxMap) {
    ctxMap = new Map<string, KeybindingAction>();
    resolved.set(block.context, ctxMap);
  }
  const seenKeys = new Set<string>();
  for (const [key, action] of Object.entries(block.bindings)) {
    if (seenKeys.has(key)) {
      warnings.push({
        context: block.context,
        key,
        message: `快捷键 "${key}" 重复定义`,
      });
      hasDuplicate = true;
      continue;
    }
    seenKeys.add(key);

    const keyError = validateKeyId(key);
    if (keyError) {
      warnings.push({ context: block.context, key, message: keyError });
      continue;
    }
    if (isReservedKey(key)) {
      warnings.push({
        context: block.context,
        key,
        message: `"${key}" 是保留键，不可重绑`,
      });
      continue;
    }
    if (action === null) {
      ctxMap.delete(key);
      continue;
    }
    if (typeof action !== "string" || !isKeybindingAction(action)) {
      warnings.push({
        context: block.context,
        key,
        message: `未知 action："${String(action)}"`,
      });
      continue;
    }
    ctxMap.set(key, action);
  }
  return hasDuplicate;
}

/**
 * Scan raw JSON string for duplicate keys within each "bindings" block.
 * JSON.parse silently drops duplicates, so we must check at the string level.
 */
function detectDuplicateKeysInRaw(
  raw: string,
  warnings: KeybindingWarning[],
): boolean {
  let hasDuplicates = false;

  // Find each "bindings" object in the raw text
  const bindingsRegex = /"bindings"\s*:\s*\{/g;
  let match: RegExpExecArray | null;
  while ((match = bindingsRegex.exec(raw)) !== null) {
    const openPos = match.index + match[0].length;
    // Track brace depth to find the matching closing brace
    let depth = 1;
    let closePos = openPos;
    let inString = false;
    for (let i = openPos; i < raw.length && depth > 0; i++) {
      const ch = raw[i];
      if (ch === "\\") {
        i++; // skip escaped char
        continue;
      }
      if (ch === '"') {
        inString = !inString;
        continue;
      }
      if (inString) continue;
      if (ch === "{") depth++;
      else if (ch === "}") {
        depth--;
        if (depth === 0) closePos = i;
      }
    }

    const blockContent = raw.slice(openPos, closePos);
    // Extract all quoted keys before `:` in this block
    const keyRegex = /"([^"\\]*(?:\\.[^"\\]*)*)"\s*:/g;
    const seen = new Set<string>();
    let keyMatch: RegExpExecArray | null;
    while ((keyMatch = keyRegex.exec(blockContent)) !== null) {
      const key = keyMatch[1];
      if (seen.has(key)) {
        warnings.push({ key, message: `快捷键 "${key}" 重复定义` });
        hasDuplicates = true;
      } else {
        seen.add(key);
      }
    }
  }

  return hasDuplicates;
}

/**
 * Load keybindings: start from defaults, merge the user's keybindings.json on
 * top. Always succeeds — on any error it falls back to defaults and reports a
 * warning, so the TUI can never fail to start because of a bad config.
 */
export function loadKeybindings(): LoadResult {
  const resolved = buildResolved(DEFAULT_BINDINGS);
  const warnings: KeybindingWarning[] = [];

  if (!existsSync(KEYBINDINGS_FILE)) {
    return { resolved, warnings, userFileLoaded: false };
  }

  let raw: string;
  try {
    raw = readFileSync(KEYBINDINGS_FILE, "utf8").trim();
  } catch (err) {
    warnings.push({ message: `读取 keybindings.json 失败：${(err as Error).message}` });
    return { resolved, warnings, userFileLoaded: false };
  }
  if (!raw) {
    return { resolved, warnings, userFileLoaded: false };
  }

  // Detect duplicate keys at the raw string level before JSON.parse,
  // because JSON.parse silently drops duplicate object keys.
  if (detectDuplicateKeysInRaw(raw, warnings)) {
    warnings.push({ message: "keybindings.json 中存在重复快捷键，已回退到默认快捷键" });
    return { resolved: buildResolved(DEFAULT_BINDINGS), warnings, userFileLoaded: true };
  }

  let parsed: KeybindingsFile;
  try {
    parsed = JSON.parse(raw) as KeybindingsFile;
  } catch (err) {
    warnings.push({ message: `解析 keybindings.json 失败：${(err as Error).message}` });
    return { resolved, warnings, userFileLoaded: false };
  }

  if (!parsed || !Array.isArray(parsed.bindings)) {
    warnings.push({ message: 'keybindings.json 必须包含 "bindings" 数组' });
    return { resolved, warnings, userFileLoaded: true };
  }

  let hasFatalError = false;
  for (const block of parsed.bindings) {
    if (!block || typeof block !== "object") {
      warnings.push({ message: "bindings 数组中存在无效的 block" });
      continue;
    }
    if (applyUserBlock(resolved, block as KeybindingBlock, warnings)) {
      hasFatalError = true;
    }
  }

  if (hasFatalError) {
    warnings.push({ message: "配置中存在重复快捷键，已回退到默认快捷键，请修复 keybindings.json" });
    return { resolved: buildResolved(DEFAULT_BINDINGS), warnings, userFileLoaded: true };
  }

  return { resolved, warnings, userFileLoaded: true };
}

// ---------------------------------------------------------------------------
// File watcher — detect external changes (edit / delete / create) to
// keybindings.json at runtime and reload immediately, mirroring Claude Code.
// ---------------------------------------------------------------------------

let _watcher: ReturnType<typeof watch> | null = null;
let _reloadTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Start watching the keybindings.json file for external changes.
 *
 * Calls `onReload` whenever the file is created, changed, or deleted so the
 * caller can reload the resolver and reflect the change immediately.
 *
 * 不对 filename 做过滤：编辑器（vi/nano 等）保存时采用「写临时文件→rename
 * 覆盖」，在 Linux 5.4 等旧内核上 inotify 的 rename 事件回调 filename 会是
 * 临时文件名（如 vim 的 "4913"）而非 null，basename 过滤会误杀这些事件导致
 * 热更新失效。Windows 上一次保存可能触发多次事件，用 200ms 防抖合并。
 * reloadResolver 内部用 mtime 比对兜底，keybindings.json 未变时直接跳过。
 */
export function startKeybindingsWatcher(onReload: () => void): void {
  if (_watcher) return;

  const dirPath = dirname(KEYBINDINGS_FILE);

  try {
    mkdirSync(dirPath, { recursive: true });
    _watcher = watch(dirPath, { persistent: false }, () => {
      if (_reloadTimer) clearTimeout(_reloadTimer);
      _reloadTimer = setTimeout(() => {
        _reloadTimer = null;
        onReload();
      }, 200);
    });
    _watcher.on("error", () => {
      stopKeybindingsWatcher();
    });
  } catch {
    stopKeybindingsWatcher();
  }
}

/**
 * Stop the file watcher. Idempotent.
 */
export function stopKeybindingsWatcher(): void {
  if (_reloadTimer) {
    clearTimeout(_reloadTimer);
    _reloadTimer = null;
  }
  if (_watcher) {
    _watcher.close();
    _watcher = null;
  }
}

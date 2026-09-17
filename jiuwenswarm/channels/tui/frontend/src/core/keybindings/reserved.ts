/**
 * Shortcuts that cannot be rebound — they are hardcoded in keymap.ts with
 * special double-press semantics, mirroring Claude Code's reserved keys.
 */
export interface ReservedShortcut {
  key: string;
  reason: string;
}

export const NON_REBINDABLE: ReservedShortcut[] = [
  { key: "ctrl+c", reason: "中断/退出语义，硬编码（连按两次退出）" },
  { key: "ctrl+d", reason: "退出语义，硬编码（连按两次退出）" },
  { key: "ctrl+m", reason: "终端中等同于 Enter" },
];

/**
 * Normalize a key id for comparison the same way pi-tui's matchesKey does:
 * lowercase, sort modifiers, keep the last segment as the main key.
 * Only `ctrl` / `shift` / `alt` are valid modifiers.
 */
const VALID_MODIFIERS = new Set(["ctrl", "shift", "alt"]);

/**
 * Base key names understood by pi-tui's matchesKey (lowercased — matchesKey
 * lowercases the key id before matching). Mirrors the SpecialKey/Letter/Digit/
 * SymbolKey unions in @mariozechner/pi-tui's KeyId type.
 */
const SPECIAL_KEYS = new Set([
  "escape", "esc", "enter", "return", "tab", "space", "backspace", "delete",
  "insert", "clear", "home", "end", "pageup", "pagedown", "up", "down", "left",
  "right", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
]);

const SYMBOL_KEYS = new Set("`-=[]\\;',./!@#$%^&*()_+|~{}:<>?".split(""));

/** Whether `main` (already lowercased) is a key name matchesKey can match. */
function isKnownBaseKey(main: string): boolean {
  if (main.length === 1) {
    return (main >= "a" && main <= "z") || (main >= "0" && main <= "9") || SYMBOL_KEYS.has(main);
  }
  return SPECIAL_KEYS.has(main);
}

export function normalizeKey(key: string): string {
  const parts = key.trim().toLowerCase().split("+");
  const main = parts[parts.length - 1] ?? "";
  const mods = parts
    .slice(0, -1)
    .filter((m) => VALID_MODIFIERS.has(m))
    .sort();
  return [...mods, main].join("+");
}

const RESERVED_SET = new Set(NON_REBINDABLE.map((r) => normalizeKey(r.key)));

export function isReservedKey(key: string): boolean {
  return RESERVED_SET.has(normalizeKey(key));
}

/**
 * Validate that a key id is something matchesKey can understand:
 * a single (non-chord) key with only ctrl/shift/alt modifiers and a non-empty
 * main key. Chords (space-separated) are rejected because pi-tui's matchesKey
 * does not support them.
 */
export function validateKeyId(key: string): string | null {
  const trimmed = key.trim();
  if (!trimmed) return "按键不能为空";
  if (/\s/.test(trimmed)) return `不支持组合键（chord）："${key}"`;
  const parts = trimmed.toLowerCase().split("+");
  const main = parts[parts.length - 1];
  if (!main) return `按键格式无效："${key}"`;
  for (const mod of parts.slice(0, -1)) {
    if (!VALID_MODIFIERS.has(mod)) {
      return `不支持的修饰键 "${mod}"（仅支持 ctrl/shift/alt）："${key}"`;
    }
  }
  if (!isKnownBaseKey(main)) {
    return `未知按键 "${main}"（无法被识别，请检查拼写）："${key}"`;
  }
  return null;
}

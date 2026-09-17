import { type KeyId, matchesKey } from "@mariozechner/pi-tui";

import type { KeybindingAction, KeybindingContextName } from "./actions.js";
import { getKeybindingsMtimeMs, loadKeybindings, startKeybindingsWatcher } from "./store.js";
import type { KeybindingWarning, ResolvedBindings } from "./types.js";

// Initialized at module load from defaults + user file. /keybindings triggers
// reloadResolver() after the user edits the file.
let current: ResolvedBindings = loadKeybindings().resolved;
let lastMtimeMs: number | undefined = getKeybindingsMtimeMs();

/** Reload bindings from disk. Returns any validation warnings. */
export function reloadResolver(): KeybindingWarning[] {
  const mtimeMs = getKeybindingsMtimeMs();
  if (mtimeMs === lastMtimeMs) return [];
  const result = loadKeybindings();
  current = result.resolved;
  lastMtimeMs = mtimeMs;
  return result.warnings;
}

// Start watching keybindings.json for external changes (edit / delete / create).
// Automatically reloads to defaults when the file is deleted at runtime.
startKeybindingsWatcher(() => reloadResolver());

/**
 * Resolve a terminal input string to an action within a context.
 * Returns null if no binding in this context matches.
 */
export function resolveAction(
  context: KeybindingContextName,
  data: string,
): KeybindingAction | null {
  const ctxMap = current.get(context);
  if (!ctxMap) return null;
  for (const [key, action] of ctxMap) {
    // Keys in the map are either authored defaults or user keys that passed
    // validateKeyId() (structure + known base key), so they are valid KeyIds.
    // matchesKey is also total over arbitrary strings (unknown ids return
    // false), so this cast can never cause a runtime error.
    if (matchesKey(data, key as KeyId)) {
      return action;
    }
  }
  return null;
}

/** Effective bindings for a context as [key, action] pairs (for /keybindings list). */
export function getContextBindings(
  context: KeybindingContextName,
): Array<{ key: string; action: KeybindingAction }> {
  const ctxMap = current.get(context);
  if (!ctxMap) return [];
  return [...ctxMap.entries()].map(([key, action]) => ({ key, action }));
}

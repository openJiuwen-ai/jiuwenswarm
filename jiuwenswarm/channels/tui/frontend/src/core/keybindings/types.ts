import type { KeybindingAction, KeybindingContextName } from "./actions.js";

/**
 * A block of bindings for a single context, as stored in keybindings.json.
 * `null` unbinds a default shortcut.
 */
export interface KeybindingBlock {
  context: KeybindingContextName;
  bindings: Record<string, KeybindingAction | string | null>;
}

export interface KeybindingsFile {
  bindings: KeybindingBlock[];
}

/** Merged, ready-to-query bindings: context -> (keyId -> action). */
export type ResolvedBindings = Map<KeybindingContextName, Map<string, KeybindingAction>>;

export interface KeybindingWarning {
  context?: string;
  key?: string;
  message: string;
}

export interface LoadResult {
  resolved: ResolvedBindings;
  warnings: KeybindingWarning[];
  /** True when a user keybindings.json was found and parsed (even if partially invalid). */
  userFileLoaded: boolean;
}

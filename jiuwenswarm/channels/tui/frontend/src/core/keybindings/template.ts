import { DEFAULT_BINDINGS } from "./defaultBindings.js";
import type { KeybindingsFile } from "./types.js";

/**
 * Generate the initial keybindings.json content: a valid file pre-filled with
 * all default bindings, so users can edit in place. Mirrors Claude Code's
 * template approach.
 */
export function generateKeybindingsTemplate(): string {
  const file: KeybindingsFile = {
    bindings: DEFAULT_BINDINGS.map((block) => ({
      context: block.context,
      bindings: { ...block.bindings },
    })),
  };
  return JSON.stringify(file, null, 2) + "\n";
}

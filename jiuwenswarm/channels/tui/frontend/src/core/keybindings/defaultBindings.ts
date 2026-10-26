import type { KeybindingBlock } from "./types.js";

/**
 * Default keybindings that match the current hardcoded TUI behavior.
 * Loaded first; the user's keybindings.json is merged on top.
 *
 * Key ids must be understood by pi-tui's matchesKey (lowercase modifiers
 * ctrl/shift/alt; special keys like pageUp/home/end are case-insensitive).
 *
 * Note: ctrl+c (app:interrupt) and ctrl+d (app:exit) are intentionally NOT
 * listed here — they keep their double-press logic in keymap.ts and are
 * non-rebindable (see reserved.ts).
 */
export const DEFAULT_BINDINGS: KeybindingBlock[] = [
  {
    context: "Global",
    bindings: {
      "ctrl+l": "app:redraw",
      "ctrl+t": "app:toggleTodos",
      "ctrl+g": "app:toggleTeamPanel",
      "ctrl+o": "app:toggleTranscript",
      escape: "app:cancelWork",
    },
  },
  {
    context: "Scroll",
    bindings: {
      pageUp: "scroll:pageUp",
      "shift+pageUp": "scroll:pageUp",
      pageDown: "scroll:pageDown",
      "shift+pageDown": "scroll:pageDown",
      "ctrl+home": "scroll:top",
      "ctrl+end": "scroll:bottom",
    },
  },
  {
    context: "FileViewer",
    bindings: {
      escape: "fileViewer:exit",
      left: "fileViewer:exit",
      q: "fileViewer:exit",
      up: "fileViewer:lineUp",
      k: "fileViewer:lineUp",
      down: "fileViewer:lineDown",
      j: "fileViewer:lineDown",
      pageUp: "fileViewer:pageUp",
      pageDown: "fileViewer:pageDown",
      home: "fileViewer:top",
      g: "fileViewer:top",
      end: "fileViewer:bottom",
      "shift+g": "fileViewer:bottom",
    },
  },
  {
    context: "Confirmation",
    bindings: {
      y: "confirm:yes",
      n: "confirm:no",
    },
  },
  {
    context: "TeamPanel",
    bindings: {
      left: "team:back",
      up: "team:prev",
      down: "team:next",
      return: "team:viewMember",
    },
  },
  {
    context: "SwarmWorkflows",
    bindings: {
      escape: "swarm:back",
      left: "swarm:left",
      right: "swarm:nextFocus",
      l: "swarm:logs",
      p: "swarm:viewPrompt",
      o: "swarm:viewOutcome",
      e: "swarm:viewError",
      r: "swarm:refresh",
    },
  },
  {
    context: "StatusView",
    bindings: {
      escape: "status:close",
      left: "status:prevTab",
      right: "status:nextTab",
    },
  },
  {
    context: "MemoryView",
    bindings: {
      escape: "memory:close",
      left: "memory:prevTab",
      right: "memory:nextTab",
      "ctrl+o": "memory:toggleFullPath",
    },
  },
  {
    context: "ResumeList",
    bindings: {
      escape: "resume:close",
      "ctrl+a": "resume:toggleAllProjects",
      "ctrl+b": "resume:toggleBranchFilter",
      space: "resume:preview",
      "ctrl+r": "resume:rename",
    },
  },
  {
    context: "Overlay",
    bindings: {
      escape: "overlay:close",
    },
  },
];

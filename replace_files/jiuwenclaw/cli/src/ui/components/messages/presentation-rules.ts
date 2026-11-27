import type { HistoryItem } from "../../../core/types.js";

const COMPACT_EXPANDED_TOOL_NAMES = new Set([
  "write",
  "write_file",
  "write_text_file",
  "write_memory",
  "edit",
  "edit_file",
  "edit_memory",
  "search_replace",
]);

export function shouldExpandCompactToolGroup(
  entry: Extract<HistoryItem, { kind: "tool_group" }>,
): boolean {
  return entry.tools.some((tool) =>
    COMPACT_EXPANDED_TOOL_NAMES.has(tool.name.trim().toLowerCase()),
  );
}

export function shouldGapAfterEntry(entry: HistoryItem, compact: boolean): boolean {
  switch (entry.kind) {
    case "user":
    case "assistant":
    case "error":
      return true;
    case "tool_group":
    case "collapsed_tool_group":
      return true;
    case "thinking":
      return !compact;
    case "system":
    case "info":
    case "diff":
      return false;
  }
}

export function shouldEmphasizeAssistantTransition(
  entry: HistoryItem,
  nextEntry: HistoryItem | undefined,
  compact: boolean,
): boolean {
  if (!nextEntry || nextEntry.kind !== "assistant") {
    return false;
  }
  if (entry.kind === "thinking") {
    return !compact;
  }
  return entry.kind === "tool_group" || entry.kind === "collapsed_tool_group";
}

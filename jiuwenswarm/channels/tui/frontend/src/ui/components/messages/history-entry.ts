import type { HistoryItem } from "../../../core/types.js";
import { renderCompactEntry } from "./render-compact-entry.js";
import { renderDetailedEntry } from "./render-detailed-entry.js";
import type { MessageRenderOptions, RenderedHistoryEntry } from "./types.js";

export function renderHistoryEntry(
  entry: HistoryItem,
  width: number,
  options: MessageRenderOptions,
): RenderedHistoryEntry {
  if (options.compact) {
    return renderCompactEntry(entry, width, options);
  }
  return renderDetailedEntry(entry, width, options);
}

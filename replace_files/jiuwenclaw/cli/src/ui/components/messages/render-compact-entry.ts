import type { HistoryItem } from "../../../core/types.js";
import {
  CompactAssistantMessageComponent,
  CompactMessageComponent,
  DiffComponent,
  InfoMessageComponent,
  UserMessageComponent,
} from "./basic-message-components.js";
import { CollapsedToolGroupMessageComponent, ToolGroupMessageComponent } from "../tools/index.js";
import type { MessageRenderOptions, RenderedHistoryEntry } from "./types.js";
import { shouldExpandCompactToolGroup, shouldGapAfterEntry } from "./presentation-rules.js";

function shouldRenderInfoExpanded(entry: Extract<HistoryItem, { kind: "info" }>): boolean {
  if (entry.mediaItems?.length) return true;
  const meta = entry.meta;
  if (!meta) return false;
  if (meta.view) return true;
  return Boolean(meta.items && meta.items.length > 0);
}

export function renderCompactEntry(
  entry: HistoryItem,
  width: number,
  options: MessageRenderOptions,
): RenderedHistoryEntry {
  switch (entry.kind) {
    case "user":
      return {
        lines: new UserMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, true),
      };
    case "assistant":
      return {
        lines: new CompactAssistantMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, true),
      };
    case "tool_group":
      return {
        lines: new ToolGroupMessageComponent(
          entry,
          false,
          shouldExpandCompactToolGroup(entry),
          options.animationPhase,
        ).render(width),
        gapAfter: shouldGapAfterEntry(entry, true),
      };
    case "collapsed_tool_group":
      return {
        lines: new CollapsedToolGroupMessageComponent(
          entry,
          true,
          false,
          options.animationPhase,
        ).render(width),
        gapAfter: shouldGapAfterEntry(entry, true),
      };
    case "info":
      return {
        lines: new InfoMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, true),
      };
    case "diff":
      return {
        lines: new DiffComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, true),
      };
    default:
      return {
        lines: new CompactMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, true),
      };
  }
}

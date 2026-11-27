import type { HistoryItem } from "../../../core/types.js";
import {
  AssistantMessageComponent,
  DiffComponent,
  ErrorMessageComponent,
  InfoMessageComponent,
  SystemMessageComponent,
  ThinkingMessageComponent,
  UserMessageComponent,
} from "./basic-message-components.js";
import { CollapsedToolGroupMessageComponent, ToolGroupMessageComponent } from "../tools/index.js";
import type { MessageRenderOptions, RenderedHistoryEntry } from "./types.js";
import { shouldGapAfterEntry } from "./presentation-rules.js";

export function renderDetailedEntry(
  entry: HistoryItem,
  width: number,
  options: MessageRenderOptions,
): RenderedHistoryEntry {
  switch (entry.kind) {
    case "user":
      return {
        lines: new UserMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
    case "assistant":
      return {
        lines: new AssistantMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
    case "thinking":
      return {
        lines: new ThinkingMessageComponent(
          entry,
          options.thinkingExpanded,
          options.activeThinkingId === entry.id,
          options.animationPhase,
        ).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
    case "tool_group":
      return {
        lines: new ToolGroupMessageComponent(
          entry,
          options.collapsed,
          options.toolDetailsExpanded,
          options.animationPhase,
        ).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
    case "collapsed_tool_group":
      return {
        lines: new CollapsedToolGroupMessageComponent(
          entry,
          options.collapsed,
          options.toolDetailsExpanded,
          options.animationPhase,
        ).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
    case "system":
      return {
        lines: new SystemMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
    case "error":
      return {
        lines: new ErrorMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
    case "info":
      return {
        lines: new InfoMessageComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
    case "diff":
      return {
        lines: new DiffComponent(entry).render(width),
        gapAfter: shouldGapAfterEntry(entry, false),
      };
  }
}

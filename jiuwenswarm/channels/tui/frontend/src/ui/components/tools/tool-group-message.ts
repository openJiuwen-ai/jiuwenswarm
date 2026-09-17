import type { Component } from "@mariozechner/pi-tui";
import type { HistoryItem } from "../../../core/types.js";
import { renderCompactToolLines, renderHiddenToolsLine } from "./compact-tool-renderers.js";
import { renderDetailedToolLines } from "./detailed-tool-renderers.js";
import { MAX_VISIBLE_TOOLS } from "./tool-render-shared.js";

export class ToolGroupMessageComponent implements Component {
  constructor(
    private readonly entry: Extract<HistoryItem, { kind: "tool_group" }>,
    private readonly collapsed: boolean,
    private readonly showDetails: boolean,
    private readonly animationPhase: number,
  ) {}

  invalidate(): void {}

  render(width: number): string[] {
    const lines: string[] = [];
    const allTools = this.entry.tools;

    if (this.showDetails) {
      const tools = this.collapsed ? allTools.slice(-1) : allTools;
      for (const [index, tool] of tools.entries()) {
        lines.push(
          ...renderDetailedToolLines(tool, width, {
            showDetails: this.showDetails,
            animationPhase: this.animationPhase,
          }),
        );
        if (index < tools.length - 1) {
          lines.push(" ".repeat(width));
        }
      }
    } else {
      const visibleTools = this.collapsed ? allTools.slice(-1) : allTools.slice(-MAX_VISIBLE_TOOLS);
      const hiddenCount = allTools.length - visibleTools.length;

      for (const tool of visibleTools) {
        lines.push(...renderCompactToolLines(tool, width, this.animationPhase));
      }

      if (hiddenCount > 0) {
        lines.push(...renderHiddenToolsLine(width, hiddenCount));
      }
    }

    return lines;
  }
}

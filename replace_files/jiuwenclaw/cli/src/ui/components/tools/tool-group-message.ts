import type { Component } from "@mariozechner/pi-tui";
import type { HistoryItem } from "../../../core/types.js";
import { renderCompactToolLines } from "./compact-tool-renderers.js";
import { renderDetailedToolLines } from "./detailed-tool-renderers.js";

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
    const tools = this.collapsed ? this.entry.tools.slice(-1) : this.entry.tools;

    for (const [index, tool] of tools.entries()) {
      lines.push(
        ...(this.showDetails
          ? renderDetailedToolLines(tool, width, {
              showDetails: this.showDetails,
              animationPhase: this.animationPhase,
            })
          : renderCompactToolLines(tool, width, this.animationPhase)),
      );

      if (this.showDetails && index < tools.length - 1) {
        lines.push(" ".repeat(width));
      }
    }

    return lines;
  }
}

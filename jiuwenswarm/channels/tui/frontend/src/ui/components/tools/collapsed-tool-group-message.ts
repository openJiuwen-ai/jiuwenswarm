import type { Component } from "@mariozechner/pi-tui";
import type { HistoryItem } from "../../../core/types.js";
import { renderCollapsedCompactSummaryLines } from "./compact-tool-renderers.js";
import { ToolGroupMessageComponent } from "./tool-group-message.js";

export class CollapsedToolGroupMessageComponent implements Component {
  constructor(
    private readonly entry: Extract<HistoryItem, { kind: "collapsed_tool_group" }>,
    private readonly collapsed: boolean,
    private readonly showDetails: boolean,
    private readonly animationPhase: number,
  ) {}

  invalidate(): void {}

  render(width: number): string[] {
    if (!this.showDetails) {
      return renderCollapsedCompactSummaryLines(this.entry, width, this.animationPhase);
    }
    return new ToolGroupMessageComponent(
      {
        kind: "tool_group",
        id: this.entry.id,
        sessionId: this.entry.sessionId,
        requestId: this.entry.requestId,
        tools: this.collapsed ? this.entry.tools.slice(-1) : this.entry.tools,
        at: this.entry.at,
      },
      false,
      true,
      this.animationPhase,
    ).render(width);
  }
}

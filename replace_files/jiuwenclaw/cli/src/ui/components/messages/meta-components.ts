import type { Component } from "@mariozechner/pi-tui";
import type { HistoryItem } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { prefixedLines, renderWrappedText, summarize } from "../../rendering/text.js";
import { renderClaudeResponseLines, renderMediaItems } from "./shared.js";

export class SystemMessageComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "system" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    return renderWrappedText(width, `· ${this.entry.content}`, palette.text.system);
  }
}

export class ErrorMessageComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "error" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    return prefixedLines(
      renderWrappedText(Math.max(1, width - 2), this.entry.content, palette.status.error),
      width,
      "! ",
      palette.status.error,
      "  ",
    );
  }
}

export class InfoMessageComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "info" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    const meta = this.entry.meta;
    const lines: string[] = [];
    const innerWidth = Math.max(1, width);
    const title = meta?.title ?? this.entry.content;
    lines.push(...renderWrappedText(innerWidth, `· ${title}`, palette.text.info));
    if (this.entry.mediaItems?.length) {
      lines.push(...renderMediaItems(width, this.entry.mediaItems));
    }
    for (const item of meta?.items ?? []) {
      const value = item.value ? `: ${item.value}` : "";
      lines.push(
        ...renderClaudeResponseLines(
          width,
          renderWrappedText(
            Math.max(1, width - 4),
            `${item.label}${value}`,
            palette.text.assistant,
          ),
          palette.text.assistant,
        ),
      );
      if (item.description) {
        lines.push(
          ...renderClaudeResponseLines(
            width,
            renderWrappedText(Math.max(1, width - 4), item.description, palette.text.dim),
            palette.text.dim,
          ),
        );
      }
    }
    return lines;
  }
}

export class CompactMessageComponent implements Component {
  constructor(
    private readonly entry: Exclude<HistoryItem, { kind: "tool_group" | "collapsed_tool_group" }>,
  ) {}

  invalidate(): void {}

  render(width: number): string[] {
    const content =
      this.entry.kind === "assistant" || this.entry.kind === "thinking"
        ? summarize(this.entry.content, 120)
        : this.entry.content;
    const prefix =
      this.entry.kind === "assistant"
        ? "• "
        : this.entry.kind === "thinking"
          ? "· "
          : this.entry.kind === "user"
            ? "> "
            : this.entry.kind === "error"
              ? "! "
              : "· ";
    const color =
      this.entry.kind === "error"
        ? palette.status.error
        : this.entry.kind === "assistant"
          ? palette.text.assistant
          : this.entry.kind === "user"
            ? palette.text.user
            : this.entry.kind === "thinking"
              ? palette.text.thinking
              : palette.text.dim;
    return renderWrappedText(width, `${prefix}${content}`, color);
  }
}

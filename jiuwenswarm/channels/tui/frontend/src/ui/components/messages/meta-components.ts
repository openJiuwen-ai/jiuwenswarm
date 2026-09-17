import { visibleWidth } from "@mariozechner/pi-tui";
import type { Component } from "@mariozechner/pi-tui";
import type { HistoryItem } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { padToWidth, prefixedLines, renderWrappedText, summarize } from "../../rendering/text.js";
import { renderClaudeResponseLines, renderMediaItems } from "./shared.js";

function renderGroupedHelpView(
  width: number,
  meta: Extract<HistoryItem, { kind: "info" }>["meta"],
): string[] {
  const lines: string[] = [];
  const innerWidth = Math.max(1, width);

  const version = meta?.version || "";
  const versionText = version ? `jiuwenswarm CLI v${version}` : "jiuwenswarm CLI";
  lines.push(...renderWrappedText(innerWidth, `· ${versionText} — ${meta?.title ?? "Slash Commands"}`, palette.text.info));
  lines.push("");

  for (const group of meta?.groups ?? []) {
    const groupTitle = `── ${group.name} `;
    const groupPadding = Math.max(0, innerWidth - visibleWidth(groupTitle));
    const fullGroupTitle = groupTitle + "─".repeat(groupPadding);
    lines.push(padToWidth(palette.text.secondary(fullGroupTitle), innerWidth));

    for (const item of group.items) {
      const value = item.value ? ` ${item.value}` : "";
      const labelLine = `  ${item.label}${value}`;
      lines.push(padToWidth(palette.text.accent(labelLine), innerWidth));
      if (item.description) {
        lines.push(padToWidth(palette.text.dim(`      ${item.description}`), innerWidth));
      }
    }
    lines.push("");
  }

  lines.push(padToWidth(palette.text.dim("Press Esc to close"), innerWidth));

  return lines;
}

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

export class CommandEchoComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "command_echo" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    return [padToWidth(palette.surface.user(`❯ ${this.entry.content}`), width)];
  }
}

export class InfoMessageComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "info" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    const meta = this.entry.meta;

    if (meta?.view === "compact_boundary") {
      const lines = renderWrappedText(
        Math.max(1, width),
        "· Conversation compacted (ctrl+o for summary)",
        palette.text.dim,
      );
      for (const item of meta.items ?? []) {
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

    if (meta?.view === "compact_summary") {
      const lines: string[] = [];
      lines.push(...renderWrappedText(Math.max(1, width), "· Compaction summary", palette.text.info));
      const isRewindCompact = meta.title && meta.title.startsWith("Summarized");
      if (!isRewindCompact) {
        lines.push(
          ...renderClaudeResponseLines(
            width,
            renderWrappedText(
              Math.max(1, width - 4),
              "The earlier conversation has been compacted into the following memory block, which will be used as historical context going forward:",
              palette.text.dim,
            ),
            palette.text.dim,
          ),
        );
      }
      lines.push(
        ...renderClaudeResponseLines(
          width,
          renderWrappedText(Math.max(1, width - 4), this.entry.content, palette.text.assistant),
          palette.text.assistant,
        ),
      );
      return lines;
    }

    if (meta?.view === "rewind_summary") {
      const lines: string[] = [];
      lines.push(
        ...renderWrappedText(Math.max(1, width), "⏺ Summarized conversation", palette.text.info),
      );
      lines.push(
        ...renderClaudeResponseLines(
          width,
          renderWrappedText(
            Math.max(1, width - 4),
            this.entry.content,
            palette.text.assistant,
          ),
          palette.text.assistant,
        ),
      );
      lines.push(
        ...renderWrappedText(
          Math.max(1, width),
          "   (ctrl+o to expand history)",
          palette.text.dim,
        ),
      );
      return lines;
    }

    if (meta?.view === "help" && meta.groups?.length) {
      return renderGroupedHelpView(width, meta);
    }

    const textColor = meta?.view === "dim" ? palette.text.dim : palette.text.info;
    const lines: string[] = [];
    const innerWidth = Math.max(1, width);
    const title = meta?.title ?? this.entry.content;
    lines.push(...renderWrappedText(innerWidth, `· ${title}`, textColor));
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

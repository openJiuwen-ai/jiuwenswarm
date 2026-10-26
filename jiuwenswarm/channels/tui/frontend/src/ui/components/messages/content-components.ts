import { truncateToWidth, type Component } from "@mariozechner/pi-tui";
import type { HistoryItem } from "../../../core/types.js";
import { chalk, palette } from "../../theme.js";
import {
  padToWidth,
  prefixedLines,
  renderStyledMarkdownLines,
  renderWrappedText,
} from "../../rendering/text.js";
import { renderMediaItems } from "./shared.js";

function renderAssistantLines(
  entry: Extract<HistoryItem, { kind: "assistant" }>,
  width: number,
): string[] {
  const body = entry.content.trim();
  if (!body && (!entry.mediaItems || entry.mediaItems.length === 0)) {
    return [];
  }
  const lines = body
    ? renderStyledMarkdownLines(
        width,
        body,
        {
          color: palette.text.assistant,
        },
        0,
        0,
      )
    : [];
  if (entry.streaming && lines.length > 0) {
    const lastIndex = lines.length - 1;
    lines[lastIndex] = truncateToWidth(lines[lastIndex], Math.max(1, width));
  }
  if (entry.mediaItems?.length) {
    lines.push(...renderMediaItems(width, entry.mediaItems));
  }
  return lines;
}

function renderThinkingLabel(active: boolean, animationPhase: number, width: number): string[] {
  const label = "thinking";
  if (!active) {
    return [padToWidth(chalk.italic(palette.text.dim(label)), width)];
  }

  const focus = (animationPhase % (label.length + 2)) - 1;
  const animated = label
    .split("")
    .map((char, index) => {
      const distance = Math.abs(index - focus);
      if (distance === 0) return palette.text.thinking(char);
      if (distance === 1) return palette.text.dim(char);
      return palette.text.subtle(char);
    })
    .join("");

  return [padToWidth(chalk.italic(animated), width)];
}

export class UserMessageComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "user" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    const lines = renderStyledMarkdownLines(
      Math.max(1, width - 2),
      this.entry.content,
      {
        color: palette.text.dim,
      },
      0,
      0,
    );
    return prefixedLines(lines, width, "> ", palette.text.user, "  ");
  }
}

export class AssistantMessageComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "assistant" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    return renderAssistantLines(this.entry, width);
  }
}

export class CompactAssistantMessageComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "assistant" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    return renderAssistantLines(this.entry, width);
  }
}

export class ThinkingMessageComponent implements Component {
  constructor(
    private readonly entry: Extract<HistoryItem, { kind: "thinking" }>,
    private readonly expanded: boolean,
    private readonly active: boolean,
    private readonly animationPhase: number,
  ) {}

  invalidate(): void {}

  render(width: number): string[] {
    if (!this.expanded) {
      return renderThinkingLabel(this.active, this.animationPhase, width);
    }
    const lines = renderThinkingLabel(this.active, this.animationPhase, width);
    const wrapped = renderStyledMarkdownLines(
      Math.max(1, width - 2),
      this.entry.content,
      {
        color: palette.text.subtle,
        italic: true,
      },
      0,
      0,
    );
    const previewLimit = 4;
    const preview = wrapped.slice(-previewLimit);
    const rendered = [...lines, ...prefixedLines(preview, width, "│ ", palette.text.subtle, "│ ")];
    if (wrapped.length > preview.length) {
      rendered.push(
        ...prefixedLines(
          renderWrappedText(
            Math.max(1, width - 2),
            `+ ${wrapped.length - preview.length} earlier lines`,
            palette.text.dim,
          ),
          width,
          "│ ",
          palette.text.subtle,
          "│ ",
        ),
      );
    }
    return rendered;
  }
}

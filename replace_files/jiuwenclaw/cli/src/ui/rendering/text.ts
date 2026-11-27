import {
  Markdown,
  truncateToWidth,
  visibleWidth,
  wrapTextWithAnsi,
  type DefaultTextStyle,
} from "@mariozechner/pi-tui";
import { markdownTheme } from "../theme.js";

export function emptyLine(width: number): string {
  return " ".repeat(Math.max(0, width));
}

export function padToWidth(value: string, width: number): string {
  const clipped = truncateToWidth(value, width, "");
  const padding = Math.max(0, width - visibleWidth(clipped));
  return clipped + " ".repeat(padding);
}

export function summarize(value: unknown, max = 88): string {
  const raw = typeof value === "string" ? value : JSON.stringify(value);
  if (!raw) return "";
  return raw.length > max ? `${raw.slice(0, max - 3)}...` : raw;
}

export function prefixedLines(
  lines: string[],
  width: number,
  prefix: string,
  prefixFn: (value: string) => string,
  continuationPrefix?: string,
): string[] {
  const activeContinuationPrefix =
    continuationPrefix === undefined ? " ".repeat(prefix.length) : continuationPrefix;
  const bodyWidth = Math.max(1, width - Math.max(prefix.length, activeContinuationPrefix.length));
  return lines.map(
    (line, index) =>
      prefixFn(index === 0 ? prefix : activeContinuationPrefix) + padToWidth(line, bodyWidth),
  );
}

export function renderWrappedText(
  width: number,
  text: string,
  colorFn?: (value: string) => string,
): string[] {
  const lines = wrapTextWithAnsi(text || "", Math.max(1, width));
  return (lines.length > 0 ? lines : [""]).map((line) =>
    colorFn ? colorFn(padToWidth(line, width)) : padToWidth(line, width),
  );
}

export function renderMarkdownLines(
  width: number,
  text: string,
  paddingX = 0,
  paddingY = 0,
): string[] {
  const markdown = new Markdown(text || "", paddingX, paddingY, markdownTheme);
  const lines = markdown.render(width);
  return lines.length > 0 ? lines : [emptyLine(width)];
}

export function renderStyledMarkdownLines(
  width: number,
  text: string,
  style: DefaultTextStyle,
  paddingX = 0,
  paddingY = 0,
): string[] {
  const markdown = new Markdown(text || "", paddingX, paddingY, markdownTheme, style);
  const lines = markdown.render(width);
  return lines.length > 0 ? lines : [emptyLine(width)];
}

export function renderIndentedBlock(
  width: number,
  text: string,
  colorFn?: (value: string) => string,
  prefix = "  ",
): string[] {
  return renderWrappedText(width, `${prefix}${text}`, colorFn);
}

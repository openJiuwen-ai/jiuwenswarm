import type { ToolCallDisplay } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { prefixedLines, renderWrappedText } from "../../rendering/text.js";
import { isToolRunning } from "./tool-kind-utils.js";
import { formatStructuredValue } from "./tool-structured-data.js";

const TOOL_BODY_PREFIX = "  ⎿ ";
const TOOL_BODY_CONTINUATION = "    ";
const TOOL_TAIL_PREFIX = "  ⎿ ";
const TOOL_TAIL_CONTINUATION = "    ";
export const TOOL_EXPAND_HINT = "ctrl+o to expand";
const MAX_STRUCTURED_LINES_COLLAPSED = 6;
const MAX_STRUCTURED_LINES_EXPANDED = 18;

function toolPrefix(tool: ToolCallDisplay, animationPhase = 0): string {
  if (tool.isError || tool.status === "error") return "! ";
  if (tool.status === "timeout") return "◌ ";
  if (isToolRunning(tool)) return ["◐ ", "◓ ", "◑ ", "◒ "][animationPhase % 4]!;
  return "● ";
}

function toolPrefixColor(tool: ToolCallDisplay): (value: string) => string {
  if (tool.isError || tool.status === "error") return palette.status.error;
  if (tool.status === "timeout") return palette.status.warning;
  if (isToolRunning(tool)) return palette.text.tool;
  return palette.status.success;
}

function toolLineColor(tool: ToolCallDisplay): (value: string) => string {
  if (tool.isError || tool.status === "error") return palette.status.error;
  if (tool.status === "timeout") return palette.status.warning;
  if (isToolRunning(tool)) return palette.text.tool;
  return palette.text.dim;
}

export function toolStateColor(tool: ToolCallDisplay): (value: string) => string {
  if (tool.isError || tool.status === "error") return palette.status.error;
  if (tool.status === "timeout") return palette.status.warning;
  if (isToolRunning(tool)) return palette.text.tool;
  return palette.text.dim;
}

export function renderToolTitle(
  width: number,
  tool: ToolCallDisplay,
  text: string,
  animationPhase = 0,
): string[] {
  return prefixedLines(
    renderWrappedText(Math.max(1, width - 2), text, toolLineColor(tool)),
    width,
    toolPrefix(tool, animationPhase),
    toolPrefixColor(tool),
    "  ",
  );
}

export function renderToolBranch(
  width: number,
  text: string,
  colorFn: (value: string) => string,
): string[] {
  return prefixedLines(
    renderWrappedText(Math.max(1, width - 2), text, colorFn),
    width,
    TOOL_BODY_PREFIX,
    palette.text.subtle,
    TOOL_BODY_CONTINUATION,
  );
}

export function renderToolBranchAnsi(width: number, text: string): string[] {
  return renderToolBranch(width, text, (value) => value);
}

export function renderToolTail(
  width: number,
  text: string,
  colorFn: (value: string) => string,
): string[] {
  return prefixedLines(
    renderWrappedText(Math.max(1, width - 2), text, colorFn),
    width,
    TOOL_TAIL_PREFIX,
    palette.text.subtle,
    TOOL_TAIL_CONTINUATION,
  );
}

export function renderDiffRow(
  width: number,
  sign: " " | "+" | "-",
  text: string,
  colorFn: (value: string) => string,
): string[] {
  return renderWrappedText(width, `${sign} ${text}`, colorFn);
}

export function renderSimpleDiff(width: number, beforeText: string, afterText: string): string[] {
  const beforeLines = beforeText.split("\n");
  const afterLines = afterText.split("\n");
  const total = Math.max(beforeLines.length, afterLines.length);
  const lines: string[] = [];
  for (let i = 0; i < total; i += 1) {
    const before = beforeLines[i];
    const after = afterLines[i];
    if (before === after && before !== undefined) {
      lines.push(...renderDiffRow(width, " ", before, palette.diff.context));
      continue;
    }
    if (before !== undefined) lines.push(...renderDiffRow(width, "-", before, palette.diff.remove));
    if (after !== undefined) lines.push(...renderDiffRow(width, "+", after, palette.diff.add));
  }
  return lines;
}

export function renderAddedLines(width: number, text: string): string[] {
  return text
    .split("\n")
    .filter((line) => line.length > 0)
    .flatMap((line) => renderDiffRow(width, "+", line, palette.diff.add));
}

export function renderStructuredBranch(
  width: number,
  value: unknown,
  showDetails: boolean,
  colorFn: (value: string) => string,
): string[] {
  const maxLines = showDetails ? MAX_STRUCTURED_LINES_EXPANDED : MAX_STRUCTURED_LINES_COLLAPSED;
  const { lines } = formatStructuredValue(value, maxLines, showDetails ? 4 : 2);
  return lines.flatMap((line) => renderToolBranch(width, line, colorFn));
}

export function renderSearchMatchBranch(width: number, line: string): string[] {
  const trimmed = line.trim();
  const match = /^(.+?):(\d+)(?::(\d+))?(?:(:|\s+-\s+|\s+)(.*))?$/.exec(trimmed);
  if (!match) return renderToolBranch(width, line, palette.text.assistant);
  const [, filePath, lineNumber, columnNumber, separatorRaw, remainderRaw] = match;
  if (!filePath || (!filePath.includes("/") && !filePath.includes("."))) {
    return renderToolBranch(width, line, palette.text.assistant);
  }
  const separator = separatorRaw ?? "";
  const remainder = remainderRaw ?? "";
  const formatted = `${palette.text.tool(filePath)}${palette.text.dim(`:${lineNumber}${columnNumber ? `:${columnNumber}` : ""}`)}${separator}${remainder ? palette.text.assistant(remainder) : ""}`;
  return renderToolBranchAnsi(width, formatted);
}

export function renderPreviewLines(
  width: number,
  lines: string[],
  colorFn: (value: string) => string,
  expandedLimit: number,
  collapsedLimit: number,
  showDetails: boolean,
  noun: string,
): string[] {
  const limit = showDetails ? expandedLimit : collapsedLimit;
  const shown = lines.slice(0, limit);
  const rendered = shown.flatMap((line) => renderToolBranch(width, line, colorFn));
  if (lines.length > shown.length) {
    rendered.push(
      ...renderToolBranch(width, `+ ${lines.length - shown.length} more ${noun}`, palette.text.dim),
    );
  }
  if (!showDetails && lines.length > 0) {
    rendered.push(...renderToolBranch(width, TOOL_EXPAND_HINT, palette.text.dim));
  }
  return rendered;
}

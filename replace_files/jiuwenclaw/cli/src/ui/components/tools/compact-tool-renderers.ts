import type { ToolCallDisplay } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { summarize } from "../../rendering/text.js";
import {
  getStringArg,
  getToolFilePath,
  isEditTool,
  isFetchTool,
  isGlobTool,
  isListTool,
  isMcpTool,
  isReadTool,
  isRunTool,
  isSearchTool,
  isToolRunning,
  isWriteTool,
  renderToolBranch,
  renderToolTitle,
  summarizeToolArguments,
  toolDisplayName,
  TOOL_EXPAND_HINT,
} from "./tool-render-shared.js";

function summarizeCompactHint(hint: string, maxWidth = 72): string {
  const normalized =
    hint
      .split("\n")
      .map((line) => line.trim())
      .find((line) => line.length > 0) ?? hint.trim();
  return summarize(normalized, maxWidth);
}

function compactActionLabel(label: string): string {
  return label;
}

export function compactToolTitle(tool: ToolCallDisplay): string {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const path = getToolFilePath(args);
  const query = getStringArg(args, "pattern", "query", "q", "prompt", "glob");
  const url = getStringArg(args, "url");
  const running = isToolRunning(tool);
  const hasError = tool.isError || tool.status === "error";
  const hasTimeout = tool.status === "timeout";

  if (hasError) {
    return `Failed ${toolDisplayName(tool).toLowerCase()} (${TOOL_EXPAND_HINT})`;
  }
  if (hasTimeout) {
    return `Timed out ${toolDisplayName(tool).toLowerCase()} (${TOOL_EXPAND_HINT})`;
  }
  if (isListTool(tool.name)) {
    return `${compactActionLabel(running ? "Listing directories" : "Listed directories")} (${TOOL_EXPAND_HINT})`;
  }
  if (isReadTool(tool.name)) {
    return `${compactActionLabel(`${running ? "Reading" : "Read"} ${path ?? "file"}`)} (${TOOL_EXPAND_HINT})`;
  }
  if (isSearchTool(tool.name) || isGlobTool(tool.name)) {
    const target = query ? summarize(query, 48) : toolDisplayName(tool).toLowerCase();
    return `${compactActionLabel(`${running ? "Searching" : "Searched"} ${target}`)} (${TOOL_EXPAND_HINT})`;
  }
  if (isWriteTool(tool.name)) {
    return `${compactActionLabel(`${running ? "Writing" : "Wrote"} ${path ?? "file"}`)} (${TOOL_EXPAND_HINT})`;
  }
  if (isEditTool(tool.name)) {
    return `${compactActionLabel(`${running ? "Editing" : "Edited"} ${path ?? "file"}`)} (${TOOL_EXPAND_HINT})`;
  }
  if (isFetchTool(tool.name)) {
    return `${compactActionLabel(`${running ? "Fetching" : "Fetched"} ${url ? summarize(url, 48) : "page"}`)} (${TOOL_EXPAND_HINT})`;
  }
  if (isRunTool(tool.name)) {
    return `${compactActionLabel(`${running ? "Running" : "Ran"} command`)} (${TOOL_EXPAND_HINT})`;
  }
  if (isMcpTool(tool.name)) {
    return `${compactActionLabel(
      `${running ? "Querying" : "Queried"} ${toolDisplayName(tool)
        .replace(/^Query\s+/i, "")
        .toLowerCase()}`,
    )} (${TOOL_EXPAND_HINT})`;
  }
  return `${compactActionLabel(running ? "Working" : toolDisplayName(tool))} (${TOOL_EXPAND_HINT})`;
}

export function compactToolHint(tool: ToolCallDisplay): string | undefined {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};

  if (isRunTool(tool.name)) {
    const command = getStringArg(args, "command", "cmd", "script", "input");
    return command ? `$ ${summarize(command, 120)}` : tool.summary;
  }
  if (isWriteTool(tool.name) || isEditTool(tool.name)) {
    return getToolFilePath(args) ?? summarizeToolArguments(tool.name, args) ?? tool.summary;
  }
  if (isListTool(tool.name) || isReadTool(tool.name)) {
    return getToolFilePath(args) ?? tool.summary;
  }
  if (isFetchTool(tool.name)) {
    return getStringArg(args, "url") ?? tool.summary;
  }
  if (isSearchTool(tool.name) || isGlobTool(tool.name)) {
    return summarizeToolArguments(tool.name, args) ?? tool.summary;
  }
  if (isMcpTool(tool.name)) {
    return summarizeToolArguments(tool.name, args) ?? tool.summary;
  }
  return summarizeToolArguments(tool.name, args) ?? tool.summary;
}

export function renderCompactToolLines(
  tool: ToolCallDisplay,
  width: number,
  animationPhase: number,
): string[] {
  const title = compactToolTitle(tool);
  const lines = renderToolTitle(width, tool, title, animationPhase);
  const hint = compactToolHint(tool);
  if (hint) {
    lines.push(...renderToolBranch(width, summarizeCompactHint(hint, 80), palette.text.dim));
  }
  return lines;
}

export function renderCollapsedCompactSummaryLines(
  entry: {
    id: string;
    counts: { read: number; search: number; list: number; fetch: number };
    tools: ToolCallDisplay[];
    latestHint?: string;
  },
  width: number,
  animationPhase: number,
): string[] {
  const readCount = entry.counts.read;
  const searchCount = entry.counts.search;
  const listCount = entry.counts.list;
  const fetchCount = entry.counts.fetch;
  const running = entry.tools.some((tool) => isToolRunning(tool));
  const hasError = entry.tools.some((tool) => tool.isError || tool.status === "error");
  const hasTimeout = entry.tools.some((tool) => tool.status === "timeout");
  const lastTool = entry.tools[entry.tools.length - 1];

  const actionLabel =
    listCount > 0
      ? `${running ? "Listing" : "Listed"} ${listCount} director${listCount === 1 ? "y" : "ies"}`
      : readCount > 0
        ? `${running ? "Reading" : "Read"} ${readCount} file${readCount === 1 ? "" : "s"}`
        : searchCount > 0
          ? `${running ? "Searching" : "Searched"} ${searchCount} match set${searchCount === 1 ? "" : "s"}`
          : fetchCount > 0
            ? `${running ? "Fetching" : "Fetched"} ${fetchCount} page${fetchCount === 1 ? "" : "s"}`
            : `${running ? "Working" : "Completed"} ${entry.tools.length} tool${entry.tools.length === 1 ? "" : "s"}`;

  const statusLabel = hasError
    ? `Failed ${entry.tools.length} tool${entry.tools.length === 1 ? "" : "s"}`
    : hasTimeout
      ? `Timed out after ${entry.tools.length} tool${entry.tools.length === 1 ? "" : "s"}`
      : compactActionLabel(actionLabel);

  const pseudoTool: ToolCallDisplay = {
    callId: entry.id,
    name: "collapsed",
    status: hasError ? "error" : hasTimeout ? "timeout" : running ? "running" : "completed",
    isError: hasError,
  };
  const lines = renderToolTitle(
    width,
    pseudoTool,
    `${statusLabel} (${TOOL_EXPAND_HINT})`,
    animationPhase,
  );

  const hint = entry.latestHint ?? lastTool?.summary;
  if (hint) {
    lines.push(...renderToolBranch(width, summarizeCompactHint(hint, 72), palette.text.dim));
  }

  return lines;
}

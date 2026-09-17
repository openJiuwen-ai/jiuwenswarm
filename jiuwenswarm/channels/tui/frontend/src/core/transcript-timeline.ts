import type { HistoryItem, ToolExecution } from "./types.js";

type TimelineItem =
  | {
      type: "message";
      timestampMs: number;
      sourceIndex: number;
      entry: Exclude<HistoryItem, { kind: "tool_group" }>;
    }
  | {
      type: "tool_execution";
      timestampMs: number;
      sourceIndex: number;
      execution: ToolExecution;
    };

function toTimestampMs(value: string | undefined): number {
  if (!value) return Number.NaN;
  const ts = Date.parse(value);
  return Number.isNaN(ts) ? Number.NaN : ts;
}

function compareTimelineItems(a: TimelineItem, b: TimelineItem): number {
  const aValid = Number.isFinite(a.timestampMs);
  const bValid = Number.isFinite(b.timestampMs);
  if (aValid && bValid && a.timestampMs !== b.timestampMs) {
    return a.timestampMs - b.timestampMs;
  }
  if (aValid !== bValid) {
    return aValid ? -1 : 1;
  }
  return a.sourceIndex - b.sourceIndex;
}

function buildTimelineItems(entries: HistoryItem[], executions: ToolExecution[]): TimelineItem[] {
  const messageItems: TimelineItem[] = entries
    .filter(
      (entry): entry is Exclude<HistoryItem, { kind: "tool_group" }> => entry.kind !== "tool_group",
    )
    .map((entry, index) => ({
      type: "message",
      timestampMs: toTimestampMs(entry.at),
      sourceIndex: index,
      entry,
    }));

  const executionItems: TimelineItem[] = executions.map((execution, index) => ({
    type: "tool_execution",
    timestampMs: toTimestampMs(execution.startedAt),
    sourceIndex: entries.length + index,
    execution,
  }));

  return [...messageItems, ...executionItems].sort(compareTimelineItems);
}

function isReadLikeTool(name: string): boolean {
  const normalized = name.trim().toLowerCase();
  return (
    normalized === "read" ||
    normalized === "read_file" ||
    normalized === "read_text_file" ||
    normalized === "read_memory" ||
    normalized === "memory_get" ||
    normalized === "view"
  );
}

function isSearchLikeTool(name: string): boolean {
  const normalized = name.trim().toLowerCase();
  return (
    normalized === "search" ||
    normalized === "grep" ||
    normalized === "rg" ||
    normalized === "ripgrep" ||
    normalized === "memory_search" ||
    normalized === "mcp_free_search" ||
    normalized === "mcp_paid_search" ||
    normalized === "glob" ||
    normalized === "glob_files" ||
    normalized === "glob_file_search"
  );
}

function isListLikeTool(name: string): boolean {
  const normalized = name.trim().toLowerCase();
  return normalized === "ls" || normalized === "list_files" || normalized === "list_dir";
}

function isFetchLikeTool(name: string): boolean {
  const normalized = name.trim().toLowerCase();
  return (
    normalized === "fetch" || normalized === "fetch_webpage" || normalized === "mcp_fetch_webpage"
  );
}

function isCollapsibleTool(name: string): boolean {
  return (
    isReadLikeTool(name) || isSearchLikeTool(name) || isListLikeTool(name) || isFetchLikeTool(name)
  );
}

type CollapsibleToolKind = "read" | "search" | "list" | "fetch" | null;

function getCollapsibleToolKind(name: string): CollapsibleToolKind {
  if (isReadLikeTool(name)) return "read";
  if (isSearchLikeTool(name)) return "search";
  if (isListLikeTool(name)) return "list";
  if (isFetchLikeTool(name)) return "fetch";
  return null;
}

function summarizePath(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined;
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts.slice(-3).join("/") || value;
}

function getLatestHintFromTool(execution: ToolExecution): string | undefined {
  const tool = execution.tool;
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  if (isReadLikeTool(tool.name) || isListLikeTool(tool.name)) {
    return summarizePath(args.path ?? args.file_path ?? args.file ?? args.dir_path);
  }
  if (isSearchLikeTool(tool.name)) {
    const pattern =
      typeof args.pattern === "string"
        ? args.pattern
        : typeof args.query === "string"
          ? args.query
          : typeof args.q === "string"
            ? args.q
            : undefined;
    return pattern ? `"${pattern}"` : undefined;
  }
  if (isFetchLikeTool(tool.name)) {
    return typeof args.url === "string" ? args.url : undefined;
  }
  return tool.formattedArgs ?? tool.description;
}

function buildCollapsedToolGroup(
  executions: ToolExecution[],
): Extract<HistoryItem, { kind: "collapsed_tool_group" }> {
  const first = executions[0]!;
  const counts = {
    read: 0,
    search: 0,
    list: 0,
    fetch: 0,
  };
  for (const execution of executions) {
    if (isReadLikeTool(execution.tool.name)) counts.read += 1;
    else if (isListLikeTool(execution.tool.name)) counts.list += 1;
    else if (isFetchLikeTool(execution.tool.name)) counts.fetch += 1;
    else if (isSearchLikeTool(execution.tool.name)) counts.search += 1;
  }
  const latestHint =
    [...executions]
      .reverse()
      .map((execution) => getLatestHintFromTool(execution))
      .find((hint) => typeof hint === "string" && hint.trim().length > 0) ?? undefined;
  return {
    kind: "collapsed_tool_group",
    id: `collapsed-tool-group-${first.toolCallId}`,
    sessionId: first.sessionId,
    requestId: first.requestId,
    tools: executions.map((execution) => execution.tool),
    at: first.startedAt,
    latestHint,
    counts,
  };
}

function pushExecutionChunk(renderEntries: HistoryItem[], executions: ToolExecution[]): void {
  if (executions.length === 0) {
    return;
  }
  const first = executions[0]!;
  if (
    executions.length >= 2 &&
    executions.every((execution) => isCollapsibleTool(execution.tool.name))
  ) {
    renderEntries.push(buildCollapsedToolGroup(executions));
    return;
  }
  renderEntries.push({
    kind: "tool_group",
    id: `tool-group-${first.toolCallId}`,
    sessionId: first.sessionId,
    requestId: first.requestId,
    tools: executions.map((execution) => execution.tool),
    at: first.startedAt,
  });
}

export function buildTranscriptEntries(
  entries: HistoryItem[],
  executions: ToolExecution[],
): HistoryItem[] {
  const timeline = buildTimelineItems(entries, executions);
  const renderEntries: HistoryItem[] = [];
  let bufferedExecutions: ToolExecution[] = [];

  const flushExecutions = () => {
    if (bufferedExecutions.length === 0) {
      return;
    }
    let chunk: ToolExecution[] = [];
    for (const execution of bufferedExecutions) {
      const previousKind =
        chunk.length > 0 ? getCollapsibleToolKind(chunk[chunk.length - 1]!.tool.name) : null;
      const currentKind = getCollapsibleToolKind(execution.tool.name);
      if (chunk.length > 0 && previousKind !== currentKind) {
        pushExecutionChunk(renderEntries, chunk);
        chunk = [];
      }
      chunk.push(execution);
    }
    pushExecutionChunk(renderEntries, chunk);
    bufferedExecutions = [];
  };

  for (const item of timeline) {
    if (item.type === "tool_execution") {
      bufferedExecutions.push(item.execution);
      continue;
    }
    flushExecutions();
    renderEntries.push(item.entry);
  }

  flushExecutions();
  return renderEntries;
}

export function getToolGroupIds(entries: HistoryItem[], executions: ToolExecution[]): string[] {
  return buildTranscriptEntries(entries, executions)
    .filter(
      (entry): entry is Extract<HistoryItem, { kind: "tool_group" | "collapsed_tool_group" }> =>
        entry.kind === "tool_group" || entry.kind === "collapsed_tool_group",
    )
    .map((entry) => entry.id);
}

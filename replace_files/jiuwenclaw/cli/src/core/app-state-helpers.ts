import { applyToolResult, createToolCallDisplay } from "./history-parser.js";
import type { HistoryItem, ToolCallDisplay, ToolExecution } from "./types.js";

export function createId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
}

export function findLastIndex<T>(
  items: T[],
  predicate: (item: T, index: number) => boolean,
): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item !== undefined && predicate(item, index)) return index;
  }
  return -1;
}

export function isIgnorableHistoryRestoreError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return message.includes("invalid page_idx or session history not found");
}

const TOOL_TIMEOUT_MS = 12_000_000;

export function computeTimeoutAt(baseIso: string): string {
  return new Date(Date.parse(baseIso) + TOOL_TIMEOUT_MS).toISOString();
}

export function upsertToolGroupDisplay(
  entries: HistoryItem[],
  sessionId: string,
  requestId: string | undefined,
  tool: ToolCallDisplay,
): HistoryItem[] {
  const groupIndex = findLastIndex(
    entries,
    (item) =>
      item.kind === "tool_group" &&
      item.sessionId === sessionId &&
      (item.tools.some((existingTool) => existingTool.callId === tool.callId) ||
        (Boolean(requestId) && item.requestId === requestId)),
  );

  if (groupIndex === -1) {
    return [
      ...entries,
      {
        kind: "tool_group",
        id: createId("tool-group"),
        sessionId,
        requestId,
        tools: [tool],
        at: new Date().toISOString(),
      },
    ];
  }

  return entries.map((item, index) => {
    if (index !== groupIndex || item.kind !== "tool_group") return item;
    const nextTools = [...item.tools];
    const toolIndex = nextTools.findIndex((existingTool) => existingTool.callId === tool.callId);
    if (toolIndex === -1) {
      nextTools.push(tool);
    } else {
      nextTools[toolIndex] = tool;
    }
    return { ...item, tools: nextTools };
  });
}

export function upsertToolGroup(
  entries: HistoryItem[],
  sessionId: string,
  requestId: string | undefined,
  toolPayload: Record<string, unknown>,
  isResult: boolean,
): HistoryItem[] {
  const nested =
    toolPayload[isResult ? "tool_result" : "tool_call"] &&
    typeof toolPayload[isResult ? "tool_result" : "tool_call"] === "object"
      ? (toolPayload[isResult ? "tool_result" : "tool_call"] as Record<string, unknown>)
      : toolPayload;
  const callId =
    typeof nested.id === "string"
      ? nested.id
      : typeof nested.tool_call_id === "string"
        ? nested.tool_call_id
        : typeof nested.toolCallId === "string"
          ? nested.toolCallId
          : typeof toolPayload.tool_call_id === "string"
            ? toolPayload.tool_call_id
            : undefined;
  const groupIndex = findLastIndex(
    entries,
    (item) =>
      item.kind === "tool_group" &&
      item.sessionId === sessionId &&
      ((Boolean(callId) && item.tools.some((tool) => tool.callId === callId)) ||
        (Boolean(requestId) && item.requestId === requestId)),
  );

  if (groupIndex === -1) {
    const baseTool = createToolCallDisplay(toolPayload);
    const nextTool = isResult ? applyToolResult(baseTool, toolPayload) : baseTool;
    return [
      ...entries,
      {
        kind: "tool_group",
        id: createId("tool-group"),
        sessionId,
        requestId,
        tools: [nextTool],
        at: new Date().toISOString(),
      },
    ];
  }

  return entries.map((item, index) => {
    if (index !== groupIndex || item.kind !== "tool_group") return item;
    const nextTools = [...item.tools];
    const toolIndex = callId ? nextTools.findIndex((tool) => tool.callId === callId) : -1;
    if (toolIndex === -1) {
      const baseTool = createToolCallDisplay(toolPayload);
      nextTools.push(isResult ? applyToolResult(baseTool, toolPayload) : baseTool);
    } else if (isResult) {
      nextTools[toolIndex] = applyToolResult(nextTools[toolIndex]!, toolPayload);
    }
    return { ...item, tools: nextTools };
  });
}

export function rebuildToolExecutionStateFromEntries(entries: HistoryItem[]): {
  toolExecutions: Map<string, ToolExecution>;
  toolExecutionOrder: string[];
} {
  const toolExecutions = new Map<string, ToolExecution>();
  const toolExecutionOrder: string[] = [];

  for (const entry of entries) {
    if (entry.kind !== "tool_group") {
      continue;
    }
    for (const tool of entry.tools) {
      if (!tool.callId || toolExecutions.has(tool.callId)) {
        continue;
      }
      toolExecutions.set(tool.callId, {
        toolCallId: tool.callId,
        sessionId: entry.sessionId,
        requestId: entry.requestId,
        tool,
        startedAt: entry.at,
        updatedAt: entry.at,
        timeoutAt: computeTimeoutAt(entry.at),
      });
      toolExecutionOrder.push(tool.callId);
    }
  }

  return { toolExecutions, toolExecutionOrder };
}

import type { AppSnapshot } from "../app-state.js";
import type { HistoryItem, ToolCallDisplay } from "../core/types.js";
import { buildTranscriptEntries } from "../core/transcript-timeline.js";

function isTodoTool(tool: ToolCallDisplay): boolean {
  const normalized = tool.name.trim().toLowerCase();
  return normalized === "todo" || normalized.startsWith("todo_");
}

function filterTodoToolEntry(entry: HistoryItem): HistoryItem | null {
  if (entry.kind !== "tool_group" && entry.kind !== "collapsed_tool_group") {
    return entry;
  }
  const tools = entry.tools.filter((tool) => !isTodoTool(tool));
  if (tools.length === 0) {
    return null;
  }
  return tools.length === entry.tools.length ? entry : { ...entry, tools };
}

export interface SelectedTranscriptEntries {
  entries: HistoryItem[];
  latestThinkingId?: string;
}

export function selectTranscriptEntries(snapshot: AppSnapshot): SelectedTranscriptEntries {
  let entries =
    snapshot.transcriptMode === "compact"
      ? buildTranscriptEntries(snapshot.entries, snapshot.toolExecutions)
          .filter((entry) => entry.kind !== "system")
          .map((entry) =>
            entry.kind === "collapsed_tool_group"
              ? { ...entry, tools: entry.tools.slice(-1) }
              : entry,
          )
      : buildTranscriptEntries(snapshot.entries, snapshot.toolExecutions);

  if (snapshot.transcriptFoldMode === "all") {
    // 「all」折叠工具链噪声时仍需保留 info：含 /config、/help、/fold 等本地指令反馈，否则界面会像无任何输出。
    entries = entries.filter(
      (entry) =>
        entry.kind === "user" ||
        entry.kind === "assistant" ||
        entry.kind === "thinking" ||
        entry.kind === "error" ||
        entry.kind === "info" ||
        entry.kind === "command_echo",
    );
  } else if (snapshot.transcriptFoldMode === "thinking") {
    entries = entries.filter((entry) => entry.kind !== "thinking");
  } else if (snapshot.transcriptFoldMode === "tools") {
    // 仅折叠 system；info 含 /config、/help 等反馈，隐藏会导致用户误以为命令无响应。
    entries = entries.filter((entry) => entry.kind !== "system");
  }

  entries = entries
    .map((entry) => filterTodoToolEntry(entry))
    .filter((entry): entry is HistoryItem => entry !== null);

  const latestUserIndex =
    [...entries]
      .map((entry, index) => ({ entry, index }))
      .reverse()
      .find(({ entry }) => entry.kind === "user")?.index ?? -1;
  const latestThinkingId =
    [...entries]
      .slice(latestUserIndex + 1)
      .reverse()
      .find((entry) => entry.kind === "thinking")?.id ?? undefined;

  const isLiveTurn = snapshot.isProcessing || snapshot.cancellableWork;
  // A thinking block is only "live" while the current turn is still in flight
  // AND no finalized assistant message has arrived after it. Once chat.final
  // lands, the assistant entry flips to streaming:false — at that point the
  // turn's thinking is settled history, not live, so it must NOT be pinned to
  // the bottom. Without this guard, during the brief window after chat.final
  // sets Idle but before isLiveTurn fully relaxes (or while cancellableWork
  // lingers), the PREVIOUS turn's already-finished thinking gets re-pinned and
  // rendered (e.g. "· The user is asking me to analyze..."), flashing above the
  // /status panel for a few seconds until isLiveTurn finally turns false.
  const liveThinking =
    isLiveTurn && latestThinkingId
      ? entries.find(
          (entry) => entry.kind === "thinking" && entry.id === latestThinkingId,
        )
      : undefined;
  const liveThinkingIndex = liveThinking
    ? entries.findIndex((entry) => entry.id === liveThinking.id)
    : -1;
  const hasFinalizedAssistantAfterLiveThinking =
    liveThinkingIndex !== -1 &&
    entries
      .slice(liveThinkingIndex + 1)
      .some(
        (entry) =>
          entry.kind === "assistant" && entry.streaming !== true,
      );
  const effectiveLiveThinking =
    liveThinking && !hasFinalizedAssistantAfterLiveThinking ? liveThinking : undefined;
  // compact 始终隐藏 thinking；detailed 仅在任务进行中把 live thinking 钉在底部，避免穿插在工具输出中间。
  if (snapshot.transcriptMode === "compact" || isLiveTurn) {
    entries = entries.filter((entry) => entry.kind !== "thinking");
    if (effectiveLiveThinking) {
      entries = [...entries, effectiveLiveThinking];
    }
  }
  if (snapshot.transcriptMode === "compact") {
    entries = entries.filter(
      (entry) => !(entry.kind === "info" && entry.transcriptOnly),
    );
  }

  return { entries, latestThinkingId };
}

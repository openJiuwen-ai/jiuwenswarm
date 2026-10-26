import type { AppSnapshot } from "../app-state.js";
import { isTeamMode } from "../core/modes.js";
import { renderTeamPanel } from "./components/team-panel.js";
import { isTeamWorking } from "./components/team-shared.js";
import { renderTodoList } from "./components/todo-list.js";
import { APP_SCREEN_KEY_BINDINGS } from "./keymap.js";
import { padToWidth, renderStyledMarkdownLines, renderWrappedText } from "./rendering/text.js";
import { palette } from "./theme.js";
import { buildTranscriptLines } from "./transcript-renderer.js";
import { loadTuiConfig } from "../core/tui-config-store.js";

export interface ScreenLayoutOptions {
  width: number;
  height?: number;
  questionLines: string[];
  editorLines: string[];
  composerPreviewLines: string[];
  pendingInput?: string;
  pendingInputBaseline?: number;
  showFullThinking: boolean;
  showToolDetails: boolean;
  showShortcutHelp: boolean;
  todosCollapsed: boolean;
  showTeamPanel: boolean;
  selectedTeamMemberId: string | null;
  viewedTeamMemberId: string | null;
  transientNotice: string | null;
  animationPhase: number;
  runningElapsedMs?: number;
  transcriptScrollOffset?: number;
  onTranscriptScrollOffsetChange?: (offset: number) => void;
  btwOverlayScrollOffset?: number;
  onBtwOverlayScrollOffsetChange?: (offset: number) => void;
  /** 当前 btw overlay 在历史中的下标（-1 无），用于提示 i/n */
  btwOverlayIndex?: number;
  /** btw 历史总数 */
  btwOverlayTotal?: number;
  /** 替换 transcript 区域的 overlay 内容（如 chat 内 H 打开的 pending 面板） */
  overlayTranscriptLines?: string[];
}

function formatSubtaskStatus(status: string): string {
  switch (status) {
    case "starting":
      return "starting";
    case "tool_call":
      return "tool";
    case "tool_result":
      return "result";
    case "completed":
      return "done";
    case "error":
      return "error";
    default:
      return status;
  }
}

function formatElapsed(ms: number | undefined): string {
  if (ms === undefined || !Number.isFinite(ms) || ms < 0) {
    return "0s";
  }
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function formatTokenCount(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens < 0) {
    return "0";
  }
  if (tokens < 1000) {
    return Math.floor(tokens).toLocaleString("en-US");
  }
  return `${(tokens / 1000).toFixed(1).replace(/\.0$/, "")}k`;
}

function renderRunningStatus(
  animationPhase: number,
  elapsedMs: number | undefined,
  usage: AppSnapshot["currentQueryUsage"],
): string {
  const label = "Working";
  const sweep = animationPhase % (label.length + 3);
  const focus = sweep - 1;
  const animatedLabel = label
    .split("")
    .map((char, index) => {
      const distance = Math.abs(index - focus);
      if (distance === 0) return palette.text.assistant(char);
      if (distance === 1) return palette.text.dim(char);
      return palette.text.subtle(char);
    })
    .join("");
  const totalTokens =
    usage.total_tokens > 0 ? usage.total_tokens : usage.input_tokens + usage.output_tokens;
  const tokenStatus =
    totalTokens > 0 ? ` • ${formatTokenCount(totalTokens)} tokens` : "";
  return `• ${animatedLabel} (${formatElapsed(elapsedMs)}${tokenStatus} • esc to interrupt)`;
}

function renderInterruptedStatus(): string {
  return "• Interrupted";
}

function renderReconnectingStatus(elapsedMs: number | undefined): string {
  return `retrying connection (${formatElapsed(elapsedMs)} · esc to interrupt)`;
}

function renderNetworkOfflineStatus(streamIdleMs: number | null, elapsedMs: number | undefined): string {
  const idle = streamIdleMs === null ? "0s" : formatElapsed(streamIdleMs);
  return `network offline? (${idle} since progress, ${formatElapsed(elapsedMs)} total · esc to interrupt)`;
}

function connectionStatusLabel(status: AppSnapshot["connectionStatus"]): string | null {
  switch (status) {
    case "connecting":
      return "connecting to backend";
    case "reconnecting":
      return "backend unavailable · retrying";
    case "auth_failed":
      return "auth failed";
    case "message_too_big":
      return "消息过大 · 连接被断开";
    case "idle":
      return "backend unavailable";
    case "connected":
    default:
      return null;
  }
}

function isPlanMode(mode: AppSnapshot["mode"]): boolean {
  return mode === "agent.plan" || mode === "code.plan" || mode === "team.plan";
}

function buildStatusLines(
  snapshot: AppSnapshot,
  width: number,
  transientNotice: string | null,
  animationPhase: number,
  runningElapsedMs: number | undefined,
): string[] {
  const left: string[] = [];
  const connectionLabel = connectionStatusLabel(snapshot.connectionStatus);
  if (connectionLabel) left.push(connectionLabel);
  if (snapshot.sessionTitle) {
    // Lowercase "(Branch)" / "(Branch N)" for the status bar — less
    // prominent than the uppercase metadata version used in /resume list.
    const raw = snapshot.sessionTitle.replace("(Branch", "(branch");
    const displayTitle = raw.length > 30 ? raw.slice(0, 30) + "..." : raw;
    left.push(displayTitle);
  }
  left.push(`mode:${snapshot.mode}`);
  if (isPlanMode(snapshot.mode)) left.push("使用 /mode 退出plan模式");
  if (snapshot.transcriptFoldMode !== "none") left.push(`fold:${snapshot.transcriptFoldMode}`);
  const teamWorking =
    isTeamMode(snapshot.mode) &&
    isTeamWorking(snapshot.teamMemberEvents, snapshot.teamMessageEvents);
  const right = snapshot.lastError
    ? `error:${snapshot.lastError.split("\n")[0].slice(0, 50)}`
    : snapshot.isInterrupted
      ? renderInterruptedStatus()
      : snapshot.isPaused
        ? "paused"
        : snapshot.isProcessing || teamWorking
          ? snapshot.connectionStatus === "reconnecting"
            ? renderReconnectingStatus(runningElapsedMs)
            : snapshot.streamStalled
              ? renderNetworkOfflineStatus(snapshot.streamIdleMs, runningElapsedMs)
              : renderRunningStatus(
                  animationPhase,
                  runningElapsedMs,
                  snapshot.currentQueryUsage,
                )
          : null;

  const lines = transientNotice ? [padToWidth(palette.status.warning(transientNotice), width)] : [];
  const leadSubtask = snapshot.activeSubtasks[0];

  const content = right ? [...left, right].join(" | ") : left.join(" | ");
  if (content) {
    lines.push(padToWidth(palette.text.dim(content), width));
  }

  if (leadSubtask) {
    const parts = [
      `subtask ${leadSubtask.index}/${leadSubtask.total || "?"}`,
      formatSubtaskStatus(leadSubtask.status),
      leadSubtask.description || leadSubtask.task_id,
    ];
    if (leadSubtask.tool_name) parts.push(leadSubtask.tool_name);
    if (leadSubtask.message) parts.push(leadSubtask.message);
    if (snapshot.activeSubtasks.length > 1)
      parts.push(`+${snapshot.activeSubtasks.length - 1} more`);
    lines.push(padToWidth(palette.text.dim(parts.join(" | ")), width));
  } else if (snapshot.evolutionStatus === "running") {
    lines.push(padToWidth(palette.text.dim("evolution | running"), width));
  }
  return lines;
}

function buildStatusLineBar(snapshot: AppSnapshot, width: number): string[] {
  if (!snapshot.statusLineText) return [];
  const config = loadTuiConfig();
  const sl = config.statusLine;
  const paddingX = sl?.padding ?? 0;
  const paddedWidth = width - paddingX * 2;
  if (paddedWidth <= 0) return [];
  return snapshot.statusLineText.split(/\r?\n/).map((line) => {
    const truncated = line.length > paddedWidth ? line.slice(0, paddedWidth) : line;
    const inner = padToWidth(palette.text.dim(truncated), paddedWidth);
    return " ".repeat(paddingX) + inner + " ".repeat(paddingX);
  });
}

function renderBtwOverlay(
  overlay: { question: string; answer: string },
  width: number,
  maxHeight: number,
  scrollOffset: number,
  overlayIndex?: number,
  overlayTotal?: number,
): { lines: string[]; offset: number } {
  const lines: string[] = [];
  const safeWidth = Math.max(1, width);
  const availableHeight = Math.max(0, Math.floor(maxHeight));
  if (availableHeight <= 0) return { lines: [], offset: 0 };
  const footerHeight = 2;
  // 确保与其他固定区块有视觉分隔
  lines.push(" ".repeat(safeWidth));

  // 标题行: 💡 /btw <question>
  const headerText = `💡 /btw ${overlay.question}`;
  lines.push(padToWidth(palette.text.accent(headerText), safeWidth));

  // 分隔线
  lines.push(padToWidth(palette.text.dim("─".repeat(Math.min(safeWidth, 80))), safeWidth));

  // 回答内容：完整展示，不折叠（btw 本身是单轮简短回答，不会过长）
  const bodyHeight = Math.max(0, availableHeight - lines.length - footerHeight);
  if (bodyHeight <= 0) {
    const totalEarly = overlayTotal ?? (overlayIndex !== undefined && overlayIndex >= 0 ? 1 : 0);
    const earlyHint =
      totalEarly > 1
        ? `Esc dismiss | ←/→ history ${(overlayIndex ?? 0) + 1}/${totalEarly} | c copy | x delete`
        : "Esc dismiss | c copy | x delete";
    return {
      lines: [
        padToWidth(palette.text.accent(headerText), safeWidth),
        padToWidth(palette.text.dim(earlyHint), safeWidth),
      ].slice(-availableHeight),
      offset: 0,
    };
  }

  const answerLines = renderStyledMarkdownLines(safeWidth, overlay.answer, {
    color: palette.text.secondary,
  });
  const maxOffset = Math.max(0, answerLines.length - bodyHeight);
  const offset = Math.min(maxOffset, Math.max(0, Math.floor(scrollOffset)));
  const visibleAnswerLines = answerLines.slice(offset, offset + bodyHeight);
  for (const line of visibleAnswerLines) {
    lines.push(line);
  }
  const rangeStart = answerLines.length === 0 ? 0 : offset + 1;
  const rangeEnd = Math.min(offset + visibleAnswerLines.length, answerLines.length);
  const total = overlayTotal ?? (overlayIndex !== undefined && overlayIndex >= 0 ? 1 : 0);
  const showHistory = total > 1;
  const posLabel = showHistory ? `${(overlayIndex ?? 0) + 1}/${total}` : "";
  // 用数组拼接避免尾部多余管道符（不可滚动分支末尾不再出现 " | "）
  const hintParts = ["Esc/Enter/Space/ctrl+c dismiss"];
  if (showHistory) hintParts.push(`←/→ history ${posLabel}`);
  hintParts.push("c copy");
  hintParts.push("x delete");
  if (answerLines.length > bodyHeight) {
    hintParts.push("↑/↓ scroll", "PgUp/PgDn·ctrl+p/n page", `${rangeStart}-${rangeEnd}/${answerLines.length}`);
  }
  const scrollHint = hintParts.join(" | ");

  // 提示行: Esc to dismiss
  lines.push(padToWidth(palette.text.dim(scrollHint), safeWidth));
  lines.push(" ".repeat(safeWidth));

  return { lines, offset };
}

function buildShortcutLines(width: number): string[] {
  const lines = [
    padToWidth(palette.text.secondary("Shortcuts"), width),
    ...APP_SCREEN_KEY_BINDINGS.map((binding) =>
      padToWidth(palette.text.dim(`${binding.label} | ${binding.description}`), width),
    ),
    padToWidth(palette.text.dim("/help | show slash commands"), width),
    " ".repeat(width),
  ];
  return lines;
}

function renderBtwLoading(width: number, question: string, animationPhase: number): string[] {
  const pulseTone = [
    palette.text.dim,
    palette.text.secondary,
    palette.text.accent,
    palette.text.secondary,
  ][animationPhase % 4]!;
  return renderWrappedText(
    width,
    `${pulseTone("●")} ${palette.text.dim(`Answering: ${question} (Esc to cancel)`)}`,
  );
}

export function buildAppScreenLines(snapshot: AppSnapshot, options: ScreenLayoutOptions): string[] {
  const statusLines = buildStatusLines(
    snapshot,
    options.width,
    options.transientNotice,
    options.animationPhase,
    options.runningElapsedMs,
  );
  const statusLineBarLines = buildStatusLineBar(snapshot, options.width);

  // When a custom statusline is active, hide shortcut hints (matching Claude Code).
  const suppressShortcuts = statusLineBarLines.length > 0;
  const shortcutLines = (!suppressShortcuts && options.showShortcutHelp)
    ? buildShortcutLines(options.width)
    : [];

  // Built-in status lines are always shown alongside the custom statusline,
  // matching Claude Code's approach (both render together).
  // Custom statusline is placed ABOVE the built-in status lines so that the
  // "Working" animation always stays at the screen bottom for visual prominence.
  const effectiveStatusLines = statusLines;

  const transcriptLines =
    options.overlayTranscriptLines ??
    buildTranscriptLines(
      snapshot,
      options.width,
      options.showFullThinking,
      options.showToolDetails,
      options.animationPhase,
      options.pendingInput,
      options.pendingInputBaseline,
    );
  const todoLines = renderTodoList(snapshot.todos, options.width, options.todosCollapsed, options.animationPhase);
  const hasTeamActivity =
    isTeamMode(snapshot.mode) ||
    snapshot.teamMemberEvents.length > 0 ||
    snapshot.teamTaskEvents.length > 0 ||
    snapshot.teamMessageEvents.length > 0;
  // Team events remain in the session after a task or mode switch. Keep them
  // out of the main composer area and render details only when the user opens
  // the Team panel explicitly with Ctrl+G.
  const teamPanelLines =
    options.showTeamPanel && hasTeamActivity
      ? renderTeamPanel(
          snapshot.teamMemberEvents,
          snapshot.teamTaskEvents,
          snapshot.teamMessageEvents,
          options.width,
          options.selectedTeamMemberId,
          options.viewedTeamMemberId,
        )
      : [];
  const btwLoadingLines = snapshot.btwPendingQuestion
    ? renderBtwLoading(options.width, snapshot.btwPendingQuestion, options.animationPhase)
    : [];
  const fixedLinesBeforeBtw = [
    ...todoLines,
    ...(todoLines.length > 0 && teamPanelLines.length > 0 ? [" ".repeat(options.width)] : []),
    ...teamPanelLines,
    ...options.questionLines,
    ...btwLoadingLines,
  ];
  const fixedLinesAfterBtw = [
    ...options.editorLines,
    ...options.composerPreviewLines,
    ...statusLineBarLines,
    ...effectiveStatusLines,
    ...shortcutLines,
  ];
  const height = Math.floor(options.height ?? 0);
  const btwMaxHeight =
    height > 0
      ? Math.max(0, height - fixedLinesBeforeBtw.length - fixedLinesAfterBtw.length)
      : Number.MAX_SAFE_INTEGER;
  const requestedBtwOverlayScrollOffset = options.btwOverlayScrollOffset ?? 0;
  const renderedBtwOverlay = snapshot.btwOverlay
    ? renderBtwOverlay(
        snapshot.btwOverlay,
        options.width,
        btwMaxHeight,
        requestedBtwOverlayScrollOffset,
        options.btwOverlayIndex,
        options.btwOverlayTotal,
      )
    : { lines: [], offset: 0 };
  if (renderedBtwOverlay.offset !== requestedBtwOverlayScrollOffset) {
    options.onBtwOverlayScrollOffsetChange?.(renderedBtwOverlay.offset);
  }
  const btwOverlayLines = renderedBtwOverlay.lines;
  const fixedLines = [...fixedLinesBeforeBtw, ...btwOverlayLines, ...fixedLinesAfterBtw];
  if (height <= 0) {
    return [...transcriptLines, ...fixedLines];
  }
  if (fixedLines.length >= height) {
    if ((options.transcriptScrollOffset ?? 0) !== 0) {
      options.onTranscriptScrollOffsetChange?.(0);
    }
    return fixedLines.slice(-height);
  }

  const transcriptHeight = height - fixedLines.length;
  if (transcriptLines.length <= transcriptHeight) {
    if ((options.transcriptScrollOffset ?? 0) !== 0) {
      options.onTranscriptScrollOffsetChange?.(0);
    }
    return [...transcriptLines, ...fixedLines];
  }

  const requestedOffset = Math.max(0, Math.floor(options.transcriptScrollOffset ?? 0));
  const maxOffset = transcriptLines.length - transcriptHeight;
  const offset = Math.min(maxOffset, requestedOffset);
  if (offset !== requestedOffset) {
    options.onTranscriptScrollOffsetChange?.(offset);
  }
  const start = transcriptLines.length - transcriptHeight - offset;
  return [...transcriptLines.slice(start, start + transcriptHeight), ...fixedLines];
}

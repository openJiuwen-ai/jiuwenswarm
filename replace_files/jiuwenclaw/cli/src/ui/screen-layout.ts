import type { AppSnapshot } from "../app-state.js";
import { renderMiniTeamTree, renderTeamPanel } from "./components/team-panel.js";
import { isTeamWorking } from "./components/team-shared.js";
import { renderTeamStatusPill } from "./components/team-status-pill.js";
import { renderTodoList } from "./components/todo-list.js";
import { APP_SCREEN_KEY_BINDINGS } from "./keymap.js";
import { padToWidth } from "./rendering/text.js";
import { palette } from "./theme.js";
import { buildTranscriptLines } from "./transcript-renderer.js";

export interface ScreenLayoutOptions {
  width: number;
  questionLines: string[];
  editorLines: string[];
  composerPreviewLines: string[];
  pendingInput?: string;
  pendingInputBaseline?: number;
  showFullThinking: boolean;
  showToolDetails: boolean;
  showShortcutHelp: boolean;
  showTodos: boolean;
  showTeamPanel: boolean;
  selectedTeamMemberId: string | null;
  viewedTeamMemberId: string | null;
  transientNotice: string | null;
  animationPhase: number;
  runningElapsedMs?: number;
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

function renderRunningStatus(animationPhase: number, elapsedMs: number | undefined): string {
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
  return `• ${animatedLabel} (${formatElapsed(elapsedMs)} • esc to interrupt)`;
}

function connectionStatusLabel(status: AppSnapshot["connectionStatus"]): string | null {
  switch (status) {
    case "connecting":
      return "connecting to backend";
    case "reconnecting":
      return "backend unavailable · retrying";
    case "auth_failed":
      return "auth failed";
    case "idle":
      return "backend unavailable";
    case "connected":
    default:
      return null;
  }
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
    const displayTitle = snapshot.sessionTitle.length > 30
      ? snapshot.sessionTitle.slice(0, 30) + "..."
      : snapshot.sessionTitle;
    left.push(displayTitle);
  }
  if (snapshot.mode !== "agent.plan") left.push(`mode:${snapshot.mode}`);
  if (snapshot.transcriptFoldMode !== "none") left.push(`fold:${snapshot.transcriptFoldMode}`);
  const teamWorking =
    snapshot.mode === "team" &&
    isTeamWorking(snapshot.teamMemberEvents, snapshot.teamMessageEvents);

  const right = snapshot.lastError
    ? `error:${snapshot.lastError}`
    : snapshot.isPaused
      ? "paused"
      : snapshot.isProcessing || teamWorking
        ? renderRunningStatus(animationPhase, runningElapsedMs)
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

export function buildAppScreenLines(snapshot: AppSnapshot, options: ScreenLayoutOptions): string[] {
  const statusLines = buildStatusLines(
    snapshot,
    options.width,
    options.transientNotice,
    options.animationPhase,
    options.runningElapsedMs,
  );
  const shortcutLines = options.showShortcutHelp ? buildShortcutLines(options.width) : [];

  const transcriptLines = buildTranscriptLines(
    snapshot,
    options.width,
    options.showFullThinking,
    options.showToolDetails,
    options.animationPhase,
    options.pendingInput,
    options.pendingInputBaseline,
  );
  const todoLines = options.showTodos ? renderTodoList(snapshot.todos, options.width) : [];
  const teamStatusLines =
    snapshot.mode === "team" ||
    snapshot.teamMemberEvents.length > 0 ||
    snapshot.teamTaskEvents.length > 0 ||
    snapshot.teamMessageEvents.length > 0
      ? renderTeamStatusPill(
          snapshot.teamMemberEvents,
          snapshot.teamTaskEvents,
          snapshot.teamMessageEvents,
          options.width,
        )
      : [];
  const teamPanelLines =
    options.showTeamPanel &&
    (snapshot.mode === "team" ||
      snapshot.teamMemberEvents.length > 0 ||
      snapshot.teamTaskEvents.length > 0 ||
      snapshot.teamMessageEvents.length > 0)
      ? renderTeamPanel(
          snapshot.teamMemberEvents,
          snapshot.teamTaskEvents,
          snapshot.teamMessageEvents,
          options.width,
          options.selectedTeamMemberId,
          options.viewedTeamMemberId,
        )
      : [];
  const miniTeamTreeLines =
    !options.showTeamPanel &&
    (snapshot.mode === "team" ||
      snapshot.teamMemberEvents.length > 0 ||
      snapshot.teamTaskEvents.length > 0 ||
      snapshot.teamMessageEvents.length > 0)
      ? renderMiniTeamTree(
          snapshot.teamMemberEvents,
          snapshot.teamTaskEvents,
          snapshot.teamMessageEvents,
          options.width,
        )
      : [];
  return [
    ...transcriptLines,
    ...todoLines,
    ...(todoLines.length > 0 &&
    (teamStatusLines.length > 0 || miniTeamTreeLines.length > 0 || teamPanelLines.length > 0)
      ? [" ".repeat(options.width)]
      : []),
    ...teamStatusLines,
    ...miniTeamTreeLines,
    ...teamPanelLines,
    ...options.questionLines,
    ...options.editorLines,
    ...options.composerPreviewLines,
    ...statusLines,
    ...shortcutLines,
  ];
}

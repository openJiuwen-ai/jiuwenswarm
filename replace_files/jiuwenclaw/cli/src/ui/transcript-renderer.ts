import type { AppSnapshot } from "../app-state.js";
import { renderHistoryEntry } from "./components/messages/index.js";
import { shouldEmphasizeAssistantTransition } from "./components/messages/presentation-rules.js";
import { prefixedLines, renderStyledMarkdownLines } from "./rendering/text.js";
import { palette } from "./theme.js";
import { selectTranscriptEntries } from "./transcript-entry-selection.js";
import { buildWelcomeLines } from "./welcome.js";

function renderPendingUserInput(width: number, content: string): string[] {
  const lines = renderStyledMarkdownLines(
    Math.max(1, width - 2),
    content,
    {
      color: palette.text.dim,
    },
    0,
    0,
  );
  return prefixedLines(lines, width, "> ", palette.text.user, "  ");
}

export function buildTranscriptLines(
  snapshot: AppSnapshot,
  width: number,
  showFullThinking: boolean,
  showToolDetails: boolean,
  animationPhase: number,
  pendingInput?: string,
  pendingInputBaseline?: number,
): string[] {
  const { entries: displayEntries, latestThinkingId } = selectTranscriptEntries(snapshot);

  const allLines: string[] = [...buildWelcomeLines(width, snapshot.connectionStatus, snapshot.modelInfo, snapshot.mode)];
  const showPendingInput =
    typeof pendingInput === "string" &&
    pendingInput.length > 0 &&
    typeof pendingInputBaseline === "number" &&
    snapshot.entries.length <= pendingInputBaseline;

  if (displayEntries.length === 0 && showPendingInput) {
    allLines.push(...renderPendingUserInput(width, pendingInput));
  }

  for (const [index, entry] of displayEntries.entries()) {
    const nextEntry = displayEntries[index + 1];
    const collapsed =
      (entry.kind === "tool_group" || entry.kind === "collapsed_tool_group") &&
      snapshot.collapsedToolGroupIds.has(entry.id);
    const rendered = renderHistoryEntry(entry, width, {
      compact: snapshot.transcriptMode === "compact",
      collapsed,
      thinkingExpanded: showFullThinking,
      activeThinkingId: snapshot.isProcessing ? latestThinkingId : undefined,
      toolDetailsExpanded: showToolDetails,
      animationPhase,
    });
    allLines.push(...rendered.lines);

    if (rendered.gapAfter) {
      allLines.push(" ".repeat(width));
    }
    if (
      shouldEmphasizeAssistantTransition(entry, nextEntry, snapshot.transcriptMode === "compact")
    ) {
      allLines.push(" ".repeat(width));
    }
  }

  if (displayEntries.length > 0 && showPendingInput) {
    allLines.push(...renderPendingUserInput(width, pendingInput));
  }

  return allLines;
}

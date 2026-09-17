import type { AppSnapshot } from "../app-state.js";
import type { HistoryItem } from "../core/types.js";
import { renderHistoryEntry } from "./components/messages/index.js";
import { shouldEmphasizeAssistantTransition } from "./components/messages/presentation-rules.js";
import { MAX_VISIBLE_TOOLS, TOOL_EXPAND_HINT, renderToolBranch } from "./components/tools/tool-render-shared.js";
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

function isToolEntry(entry: HistoryItem): boolean {
  return entry.kind === "tool_group" || entry.kind === "collapsed_tool_group";
}

function computeHiddenToolIndices(entries: HistoryItem[]): Set<number> {
  const hidden = new Set<number>();
  let runStart = -1;
  let runLength = 0;

  const flushRun = () => {
    if (runLength > MAX_VISIBLE_TOOLS && runStart >= 0) {
      const hideCount = runLength - MAX_VISIBLE_TOOLS;
      for (let j = runStart; j < runStart + hideCount; j++) {
        hidden.add(j);
      }
    }
    runStart = -1;
    runLength = 0;
  };

  for (let i = 0; i < entries.length; i++) {
    if (isToolEntry(entries[i]!)) {
      if (runStart === -1) runStart = i;
      runLength++;
    } else {
      flushRun();
    }
  }
  flushRun();

  return hidden;
}

function renderHiddenToolsSummary(width: number, hiddenCount: number): string[] {
  return renderToolBranch(
    width,
    `+${hiddenCount} earlier tool${hiddenCount === 1 ? "" : "s"} (${TOOL_EXPAND_HINT})`,
    palette.text.dim,
  );
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

  const allLines: string[] = [...buildWelcomeLines(width, snapshot.connectionStatus, snapshot.modelInfo, snapshot.mode, snapshot.memoryWarnings, snapshot.preferredLanguage)];
  const showPendingInput =
    typeof pendingInput === "string" &&
    pendingInput.length > 0 &&
    typeof pendingInputBaseline === "number" &&
    snapshot.entries.length <= pendingInputBaseline;

  if (displayEntries.length === 0 && showPendingInput) {
    allLines.push(...renderPendingUserInput(width, pendingInput));
  }

  const hiddenIndices = showToolDetails
    ? new Set<number>()
    : computeHiddenToolIndices(displayEntries);
  let pendingHiddenCount = 0;

  for (let i = 0; i < displayEntries.length; i++) {
    if (hiddenIndices.has(i)) {
      pendingHiddenCount++;
      continue;
    }

    if (pendingHiddenCount > 0) {
      allLines.push(...renderHiddenToolsSummary(width, pendingHiddenCount));
      pendingHiddenCount = 0;
    }

    const entry = displayEntries[i]!;
    let nextVisible: HistoryItem | undefined;
    for (let j = i + 1; j < displayEntries.length; j++) {
      if (!hiddenIndices.has(j)) {
        nextVisible = displayEntries[j]!;
        break;
      }
    }

    const collapsed =
      isToolEntry(entry) &&
      snapshot.collapsedToolGroupIds.has(entry.id);
    const rendered = renderHistoryEntry(entry, width, {
      compact: snapshot.transcriptMode === "compact",
      collapsed,
      thinkingExpanded: showFullThinking,
      activeThinkingId:
        snapshot.isProcessing || snapshot.cancellableWork ? latestThinkingId : undefined,
      toolDetailsExpanded: showToolDetails,
      animationPhase,
    });
    allLines.push(...rendered.lines);

    if (rendered.gapAfter) {
      allLines.push(" ".repeat(width));
    }
    if (
      shouldEmphasizeAssistantTransition(entry, nextVisible, snapshot.transcriptMode === "compact")
    ) {
      allLines.push(" ".repeat(width));
    }
  }

  if (pendingHiddenCount > 0) {
    allLines.push(...renderHiddenToolsSummary(width, pendingHiddenCount));
  }

  if (displayEntries.length > 0 && showPendingInput) {
    allLines.push(...renderPendingUserInput(width, pendingInput));
  }

  return allLines;
}

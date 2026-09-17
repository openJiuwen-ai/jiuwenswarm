import type { Component } from "@mariozechner/pi-tui";
import type { HistoryItem, FileDiff, GitDiffFile } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { renderWrappedText } from "../../rendering/text.js";

export class DiffComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "diff" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    return this._renderCompact(width);
  }

  // ── Compact view (default /diff) ──────────────────────────────────

  private _renderCompact(width: number): string[] {
    const lines: string[] = [];
    const turns = this.entry.meta.turns || [];
    const gitDiff = this.entry.meta.gitDiff || null;
    const innerWidth = Math.max(1, width);

    const hasTurns = turns.length > 0;
    const hasGitDiff = gitDiff && gitDiff.stats.filesChanged > 0;

    if (!hasTurns && !hasGitDiff) {
      lines.push(...renderWrappedText(innerWidth, "· No file changes in this session", palette.text.dim));
      return lines;
    }

    // Header
    lines.push("");
    lines.push(...renderWrappedText(innerWidth,
      `╭─ /diff ${"─".repeat(Math.max(0, innerWidth - 9))}`,
      palette.text.info));

    // --- Git working tree ---
    if (hasGitDiff) {
      const trackedFiles: GitDiffFile[] = [];
      const untrackedFiles: GitDiffFile[] = [];
      for (const f of Object.values(gitDiff!.files)) {
        (f.isUntracked ? untrackedFiles : trackedFiles).push(f);
      }

      lines.push(...renderWrappedText(innerWidth,
        `│ 🗂 Working Tree  +${gitDiff!.stats.linesAdded} -${gitDiff!.stats.linesRemoved}`,
        palette.text.accent));

      // Tracked
      for (const f of trackedFiles) {
        lines.push(...this._renderCompactFile(f, innerWidth));
      }

      // Untracked
      for (const f of untrackedFiles) {
        lines.push(...this._renderCompactFile(f, innerWidth));
      }

      // separator before turns
      if (hasTurns) {
        lines.push(...renderWrappedText(innerWidth, `│ ${"─".repeat(Math.max(0, innerWidth - 3))}`, palette.text.dim));
      }
    }

    // --- Per-turn sections ---
    for (const turn of turns) {
      const fileList = Object.values(turn.files);
      const promptPreview = turn.userPromptPreview.length > 40
        ? turn.userPromptPreview.slice(0, 40) + "..."
        : turn.userPromptPreview;

      lines.push(...renderWrappedText(innerWidth,
        `│ 📋 Turn ${turn.turnIndex}: "${promptPreview}"  +${turn.stats.linesAdded} -${turn.stats.linesRemoved}`,
        palette.text.accent));

      for (const f of fileList) {
        lines.push(...this._renderCompactFile(f, innerWidth));
      }
    }

    // Footer
    lines.push(...renderWrappedText(innerWidth,
      `╰${"─".repeat(innerWidth - 2)}`, palette.text.dim));
    lines.push(...renderWrappedText(innerWidth,
      "Press Esc or use /diff to see interactive diff viewer", palette.text.dim));
    lines.push("");

    return lines;
  }

  private _renderCompactFile(
    fileDiff: FileDiff | GitDiffFile,
    width: number,
  ): string[] {
    const fileName = fileDiff.filePath.split(/[/\\]/).pop() || fileDiff.filePath;

    // 构建右侧状态信息:
    // - untracked → "untracked"
    // - binary → "Binary file"
    // - large file → "Large file modified"
    // - normal/truncated → +N -N [ (truncated)]
    let statsText: string;
    if (fileDiff.isUntracked) {
      statsText = palette.text.dim("untracked");
    } else if (fileDiff.isBinary) {
      statsText = palette.text.dim("Binary file");
    } else if (fileDiff.isLargeFile) {
      statsText = palette.text.dim("Large file modified");
    } else {
      const added = palette.status.success(`+${fileDiff.linesAdded}`);
      const removed = palette.status.error(`-${fileDiff.linesRemoved}`);
      statsText = `${added} ${removed}`;
      if (fileDiff.isTruncated) {
        statsText += palette.text.dim(" (truncated)");
      }
    }

    const label = fileDiff.isNewFile && !fileDiff.isUntracked ? palette.text.dim("(new) ") : "";

    const lines: string[] = [];
    lines.push(...renderWrappedText(width,
      `│   ${fileName} ${label} ${statsText}`,
      palette.text.assistant));
    return lines;
  }
}

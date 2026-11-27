import type { Component } from "@mariozechner/pi-tui";
import type { HistoryItem, FileDiff } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { renderWrappedText } from "../../rendering/text.js";

export class DiffComponent implements Component {
  constructor(private readonly entry: Extract<HistoryItem, { kind: "diff" }>) {}

  invalidate(): void {}

  render(width: number): string[] {
    const lines: string[] = [];
    const turns = this.entry.meta.turns || [];
    const innerWidth = Math.max(1, width);

    if (turns.length === 0) {
      lines.push(...renderWrappedText(innerWidth, "· No file changes in this session", palette.text.dim));
      return lines;
    }

    const totalFiles = turns.reduce((sum, t) => sum + t.stats.filesChanged, 0);
    const totalAdded = turns.reduce((sum, t) => sum + t.stats.linesAdded, 0);
    const totalRemoved = turns.reduce((sum, t) => sum + t.stats.linesRemoved, 0);

    lines.push("");
    lines.push(...renderWrappedText(innerWidth, `╭─ Workspace Diff ─${"─".repeat(Math.max(0, innerWidth - 22))}`, palette.text.info));

    const statsLine = `│ Turns: ${turns.length}  Files: ${totalFiles}  +${totalAdded} -${totalRemoved}`;
    lines.push(...renderWrappedText(innerWidth, statsLine, palette.text.dim));

    for (const turn of turns) {
      lines.push(...renderWrappedText(innerWidth, `├${"─".repeat(innerWidth - 2)}`, palette.text.dim));

      const promptPreview = turn.userPromptPreview.length >= 30
        ? turn.userPromptPreview + "..."
        : turn.userPromptPreview;
      lines.push(...renderWrappedText(innerWidth, `│ Turn ${turn.turnIndex}: "${promptPreview}"`, palette.text.accent));

      for (const [, fileDiff] of Object.entries(turn.files)) {
        lines.push(...this._renderFileDiff(fileDiff, innerWidth));
      }
    }

    lines.push(...renderWrappedText(innerWidth, `╰${"─".repeat(innerWidth - 2)}`, palette.text.dim));
    lines.push("");

    return lines;
  }

  private _renderFileDiff(fileDiff: FileDiff, width: number): string[] {
    const lines: string[] = [];
    const fileName = fileDiff.filePath.split(/[/\\]/).pop() || fileDiff.filePath;
    
    let timeStr = "";
    if (fileDiff.lastEditTime) {
      const dt = new Date(fileDiff.lastEditTime);
      timeStr = ` [${dt.toLocaleDateString()} ${dt.toLocaleTimeString()}]`;
    }
    
    const header = `│   ${fileName} ${fileDiff.isNewFile ? "(new)" : ""} +${fileDiff.linesAdded} -${fileDiff.linesRemoved}${timeStr}`;
    lines.push(...renderWrappedText(width, header, palette.text.assistant));

    const maxHunkLines = 20;
    let totalLines = 0;

    for (const hunk of fileDiff.hunks) {
      if (totalLines >= maxHunkLines) {
        lines.push(...renderWrappedText(width, `│     ... (truncated)`, palette.text.dim));
        break;
      }

      const hunkHeader = `│     @@ -${hunk.oldStart},${hunk.oldLines} +${hunk.newStart},${hunk.newLines} @@`;
      lines.push(...renderWrappedText(width, hunkHeader, palette.text.dim));

      for (const line of hunk.lines) {
        if (totalLines >= maxHunkLines) break;
        totalLines++;

        if (line.startsWith("+")) {
          lines.push(...renderWrappedText(width, `│     ${palette.status.success(line)}`, palette.text.dim));
        } else if (line.startsWith("-")) {
          lines.push(...renderWrappedText(width, `│     ${palette.status.error(line)}`, palette.text.dim));
        } else {
          lines.push(...renderWrappedText(width, `│     ${palette.text.dim(line)}`, palette.text.dim));
        }
      }
    }

    return lines;
  }
}

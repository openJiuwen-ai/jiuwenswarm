import { Image as TuiImage } from "@mariozechner/pi-tui";
import type { MediaItem } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { prefixedLines, renderWrappedText, summarize } from "../../rendering/text.js";

export function renderClaudeResponseLines(
  width: number,
  lines: string[],
  colorFn: (value: string) => string,
): string[] {
  return prefixedLines(lines, width, "  ⎿ ", palette.text.subtle, "    ").map((line, index) =>
    index === 0 ? line : colorFn(line),
  );
}

export function renderMediaItems(width: number, items: MediaItem[]): string[] {
  const lines: string[] = [];
  for (const item of items) {
    const kind =
      item.type === "image"
        ? "Image"
        : item.type === "audio"
          ? "Audio"
          : item.type === "video"
            ? "Video"
            : "File";
    const label = `${kind}: ${item.filename}${item.url ? ` (${summarize(item.url, 72)})` : ""}`;
    lines.push(
      ...renderClaudeResponseLines(
        width,
        renderWrappedText(Math.max(1, width - 4), label, palette.text.dim),
        palette.text.dim,
      ),
    );
    if (item.type === "image" && item.base64Data) {
      const image = new TuiImage(
        item.base64Data,
        item.mimeType,
        { fallbackColor: palette.text.dim },
        { maxWidthCells: Math.max(16, Math.min(72, width - 4)), filename: item.filename },
      );
      lines.push(...image.render(width));
    }
  }
  return lines;
}

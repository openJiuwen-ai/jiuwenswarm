import { parseStoryboardClips, STORYBOARD_COLUMNS } from './storyboardClips';

export type MarkdownTablePreview = {
  headers: string[];
  rows: string[][];
};

export type StoryboardClipPreview = {
  clipNo: string;
  timeline: string;
  action: string;
  picture: string;
};

export function storyboardClipPreviews(
  text: string,
  maxClips = 4,
): StoryboardClipPreview[] {
  return parseStoryboardClips(text)
    .slice(0, maxClips)
    .map((clip) => {
      const camera = [clip.camera, clip.move].filter((part) => String(part || '').trim()).join(' / ');
      return {
        clipNo: String(clip.clip_no || '').trim(),
        timeline: String(clip.timeline || '').trim(),
        action: String(clip.character_action || '').trim(),
        picture: camera,
      };
    })
    .filter((clip) => clip.clipNo || clip.timeline || clip.action || clip.picture);
}

export const EMPTY_STORYBOARD_TABLE: MarkdownTablePreview = {
  headers: STORYBOARD_COLUMNS.map(([, header]) => header),
  rows: [STORYBOARD_COLUMNS.map(() => ''), STORYBOARD_COLUMNS.map(() => '')],
};

function splitMarkdownRow(line: string): string[] {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  return text.split('|').map((cell) => cell.trim());
}

function isSeparator(cells: string[]): boolean {
  return cells.length > 0 && cells.every((cell) => !cell || /^:?-{3,}:?$/.test(cell));
}

/** A cell can't hold a pipe or a line break without breaking the Markdown row. */
export function sanitizeMarkdownTableCell(value: string): string {
  return value.replace(/\|/g, '/').replace(/\r?\n/g, ' ');
}

export function serializeMarkdownTable(table: MarkdownTablePreview): string {
  const line = (cells: string[]) =>
    `| ${table.headers.map((_, index) => sanitizeMarkdownTableCell(cells[index] ?? '').trim()).join(' | ')} |`;
  const separator = `| ${table.headers.map(() => '---').join(' | ')} |`;
  return `${[line(table.headers), separator, ...table.rows.map(line)].join('\n')}\n`;
}

export function parseMarkdownTable(
  text: string,
  maxRows = Number.POSITIVE_INFINITY,
): MarkdownTablePreview | null {
  const rows: string[][] = [];
  for (const line of (text || '').split(/\r?\n/)) {
    if (!line.includes('|')) continue;
    const cells = splitMarkdownRow(line);
    if (!cells.some(Boolean)) continue;
    if (isSeparator(cells)) continue;
    rows.push(cells);
    if (Number.isFinite(maxRows) && rows.length >= maxRows + 1) break;
  }
  if (rows.length < 2) return null;
  const width = Math.max(...rows.map((row) => row.length));
  if (width < 2) return null;
  const [headers, ...body] = rows;
  return {
    headers: headers.concat(Array(Math.max(0, width - headers.length)).fill('')),
    rows: body.map((row) => row.concat(Array(Math.max(0, width - row.length)).fill(''))),
  };
}

import { parseStoryboardShots } from './storyboardShots';

export type MarkdownTablePreview = {
  headers: string[];
  rows: string[][];
};

export type StoryboardShotPreview = {
  shotNo: string;
  timeline: string;
  action: string;
  picture: string;
};

export function storyboardShotPreviews(
  text: string,
  maxShots = 4,
): StoryboardShotPreview[] {
  return parseStoryboardShots(text)
    .slice(0, maxShots)
    .map((shot) => {
      const camera = [shot.camera, shot.move].filter((part) => String(part || '').trim()).join(' / ');
      return {
        shotNo: String(shot.shot_no || '').trim(),
        timeline: String(shot.timeline || '').trim(),
        action: String(shot.character_action || '').trim(),
        picture: String(shot.comment || shot.scene_change || camera || '').trim(),
      };
    })
    .filter((shot) => shot.shotNo || shot.timeline || shot.action || shot.picture);
}

export const EMPTY_STORYBOARD_TABLE: MarkdownTablePreview = {
  headers: [
    'Shot',
    'Timeline',
    'Camera',
    'Move',
    'Character action',
    'Scene change',
    'Comment',
  ],
  rows: [
    ['', '', '', '', '', '', ''],
    ['', '', '', '', '', '', ''],
  ],
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

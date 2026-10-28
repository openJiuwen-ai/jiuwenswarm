import { designerAssetTextUrl } from './designerAssetUrl';
import {
  DESIGNER_NODE_ROLE_STORYBOARD,
  type DesignerExecutionGraph,
  type DesignerExecutionRun,
} from './executionGraphTypes';

export type StoryboardClip = {
  clip_no: string;
  timeline: string;
  camera: string;
  move: string;
  on_screen: string;
  character_action: string;
  speech: string;
};

export const STORYBOARD_COLUMNS: ReadonlyArray<readonly [keyof StoryboardClip, string]> = [
  ['clip_no', 'Shot'],
  ['timeline', 'Timeline'],
  ['camera', 'Camera'],
  ['move', 'Move'],
  ['on_screen', 'On screen'],
  ['character_action', 'Character action'],
  ['speech', 'Speech'],
];

const FIELD_BY_HEADER = new Map<string, keyof StoryboardClip>(
  STORYBOARD_COLUMNS.map(([field, header]) => [header.toLowerCase(), field]),
);

/** Storyboard field for a canonical table header, or null for any other header. */
export function storyboardColumnField(header: string): keyof StoryboardClip | null {
  return FIELD_BY_HEADER.get(header.trim().toLowerCase()) ?? null;
}

function emptyClip(): StoryboardClip {
  return {
    clip_no: '',
    timeline: '',
    camera: '',
    move: '',
    on_screen: '',
    character_action: '',
    speech: '',
  };
}

function splitMarkdownRow(line: string): string[] {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  return text.split('|').map((cell) => cell.trim());
}

function isSeparator(cells: string[]): boolean {
  return cells.every((cell) => !cell || /^:?-{3,}:?$/.test(cell));
}

function headerFieldMap(cells: string[]): Map<number, keyof StoryboardClip> | null {
  const mapping = new Map<number, keyof StoryboardClip>();
  cells.forEach((cell, index) => {
    const field = FIELD_BY_HEADER.get(cell.toLowerCase());
    if (field) mapping.set(index, field);
  });
  return [...mapping.values()].includes('clip_no') ? mapping : null;
}

export function parseStoryboardClips(text: string): StoryboardClip[] {
  const clips: StoryboardClip[] = [];
  let fieldMap: Map<number, keyof StoryboardClip> | null = null;
  for (const line of (text || '').split(/\r?\n/)) {
    if (!line.includes('|')) {
      if (fieldMap) break;
      continue;
    }
    const cells = splitMarkdownRow(line);
    if (!fieldMap) {
      fieldMap = headerFieldMap(cells);
      continue;
    }
    if (isSeparator(cells)) continue;
    const clip = emptyClip();
    fieldMap.forEach((field, index) => {
      if (index < cells.length) clip[field] = cells[index];
    });
    if (!clip.clip_no) clip.clip_no = String(clips.length + 1);
    clips.push(clip);
  }
  return clips;
}

export function clipGeneratePrompt(clip: StoryboardClip): string {
  const parts: string[] = [];
  const timeline = (clip.timeline || '').trim();
  if (timeline) parts.push(`Timeline ${timeline}`);
  const fields: Array<[string, keyof StoryboardClip]> = [
    ['Camera', 'camera'],
    ['Camera move', 'move'],
    ['On screen', 'on_screen'],
    ['Character action', 'character_action'],
    ['Speech', 'speech'],
  ];
  fields.forEach(([label, key]) => {
    const value = (clip[key] || '').trim();
    if (value) parts.push(`${label} ${value}`);
  });
  return parts.join('; ');
}

export function storyboardTextUrl(
  graph: DesignerExecutionGraph | null | undefined,
  run: DesignerExecutionRun | null | undefined,
): string | null {
  const node = graph?.nodes.find((item) => item.config?.role === DESIGNER_NODE_ROLE_STORYBOARD);
  if (!node) return null;
  const state = run?.node_states?.[node.id];
  const ref = state?.output_ref || node.output_ref;
  return designerAssetTextUrl(ref?.uri);
}

export async function fetchStoryboardText(url: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(String(response.status));
  return response.text();
}

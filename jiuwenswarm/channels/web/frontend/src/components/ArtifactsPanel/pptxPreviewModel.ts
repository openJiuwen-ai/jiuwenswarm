import { PRESENTATION_ARCHIVE_LIMITS } from './ooxmlArchiveLimits';

export const MAX_PRESENTATION_PREVIEW_BYTES = PRESENTATION_ARCHIVE_LIMITS.maxCompressedBytes;
export const MAX_PRESENTATION_UNCOMPRESSED_BYTES = PRESENTATION_ARCHIVE_LIMITS.maxTotalUncompressedBytes;

export type PresentationColor = string;

export type PresentationFill =
  | { kind: 'solid'; color: PresentationColor; transparency?: number }
  | { kind: 'gradient'; angle: number; stops: Array<{ offset: number; color: PresentationColor; transparency?: number }> }
  | { kind: 'none' };

export type PresentationStroke = {
  color: PresentationColor;
  width: number;
  dash?: string;
};

export type PresentationSpacing = { kind: 'absolute'; value: number } | { kind: 'relative'; value: number };

export function presentationLineHeight(spacing: PresentationSpacing | undefined): string | number {
  if (!spacing) return 'normal';
  return spacing.kind === 'absolute' ? `${spacing.value}px` : spacing.value;
}

export type PresentationRun = {
  text: string;
  fontSize?: number;
  fontFamily?: string;
  eastAsianFontFamily?: string;
  complexScriptFontFamily?: string;
  characterSpacing?: number;
  kerningThreshold?: number;
  color?: PresentationColor;
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  baseline?: number;
  hyperlink?: string;
};

export type PresentationParagraph = {
  runs: PresentationRun[];
  align?: 'left' | 'center' | 'right' | 'justify';
  level: number;
  bullet?: { kind: 'character'; value: string } | { kind: 'number' };
  marginLeft?: number;
  indent?: number;
  spaceBefore?: PresentationSpacing;
  spaceAfter?: PresentationSpacing;
  lineSpacing?: PresentationSpacing;
};

export type PresentationText = {
  paragraphs: PresentationParagraph[];
  margin: { left: number; right: number; top: number; bottom: number };
  vertical?: boolean;
  verticalReverse?: boolean;
  anchor?: 'top' | 'middle' | 'bottom';
  autoFit?: 'shrink' | 'resize' | 'none';
  fontFamily?: string;
  eastAsianFontFamily?: string;
  complexScriptFontFamily?: string;
  fontSize?: number;
  color?: PresentationColor;
};

export type PresentationBounds = { x: number; y: number; width: number; height: number; rotation?: number; flipH?: boolean; flipV?: boolean };

type PresentationNodeBase = PresentationBounds & {
  id: string;
  name: string;
  placeholderKey?: string;
};

export type PresentationShape = PresentationNodeBase & {
  type: 'shape';
  geometry: string;
  adjustments?: Record<string, number>;
  fill: PresentationFill;
  stroke?: PresentationStroke;
  text?: PresentationText;
};

export type PresentationImage = PresentationNodeBase & {
  type: 'image';
  image: Blob;
  alt: string;
  crop?: { left: number; right: number; top: number; bottom: number };
};

export type PresentationTableCell = {
  text?: PresentationText;
  fill: PresentationFill;
  stroke?: PresentationStroke;
  colSpan?: number;
  rowSpan?: number;
  merged?: boolean;
};

export type PresentationTable = PresentationNodeBase & {
  type: 'table';
  columns: number[];
  rows: Array<{ height: number; cells: PresentationTableCell[] }>;
};

export type PresentationChart = PresentationNodeBase & {
  type: 'chart';
  chartType: 'bar' | 'column' | 'line' | 'pie';
  title?: string;
  series: Array<{ name?: string; categories: string[]; values: number[]; color?: PresentationColor }>;
};

export type PresentationUnsupported = PresentationNodeBase & {
  type: 'unsupported';
  feature: string;
};

export type PresentationNode = PresentationShape | PresentationImage | PresentationTable | PresentationChart | PresentationUnsupported;

export type PresentationSlide = {
  id: string;
  name: string;
  background: PresentationFill;
  nodes: PresentationNode[];
  status?: 'incomplete' | 'invalid';
};

export type PresentationData = {
  width: number;
  height: number;
  slides: PresentationSlide[];
};

export type PresentationWorkerRequest = { type: 'parse'; buffer: ArrayBuffer };
export type PresentationWorkerResponse =
  { type: 'ready'; presentation: PresentationData } | { type: 'error'; code: 'parse-error' | 'resource-limit'; message: string };

import { SPREADSHEET_ARCHIVE_LIMITS } from './ooxmlArchiveLimits';

export const MAX_SPREADSHEET_PREVIEW_BYTES = SPREADSHEET_ARCHIVE_LIMITS.maxCompressedBytes;

export type SpreadsheetCssValue = string | number;
export type SpreadsheetCellStyle = Record<string, SpreadsheetCssValue>;

export type SpreadsheetCellData = {
  row: number;
  column: number;
  text: string;
  formula?: string;
  hyperlink?: string;
  alignRight: boolean;
  styleId: number;
};

export type SpreadsheetRowData = {
  index: number;
  styleId: number;
  cells: SpreadsheetCellData[];
};

export type SpreadsheetAxisSize = {
  index: number;
  size: number;
};

export type SpreadsheetMergeRange = {
  top: number;
  left: number;
  bottom: number;
  right: number;
};

export type SpreadsheetCellComment = {
  author?: string;
  text: string;
};

export type SpreadsheetAutoFilter = SpreadsheetMergeRange;

export type SpreadsheetDrawingAnchor = {
  fromColumn: number;
  fromRow: number;
  fromColumnOffset: number;
  fromRowOffset: number;
  toColumn?: number;
  toRow?: number;
  toColumnOffset?: number;
  toRowOffset?: number;
  width?: number;
  height?: number;
};

export type SpreadsheetChartSeries = {
  name?: string;
  categories: string[];
  values: number[];
  color?: string;
};

export type SpreadsheetChart = {
  id: string;
  anchor: SpreadsheetDrawingAnchor;
  type: 'line' | 'column' | 'bar' | 'pie' | 'scatter';
  title?: string;
  categoryAxisTitle?: string;
  valueAxisTitle?: string;
  legendPosition?: 'bottom' | 'left' | 'right' | 'top' | 'none';
  series: SpreadsheetChartSeries[];
};

export type SpreadsheetImage = {
  id: string;
  anchor: SpreadsheetDrawingAnchor;
  alt: string;
  source: string;
};

export type SpreadsheetSheetData = {
  id: number;
  name: string;
  state: 'visible' | 'hidden' | 'veryHidden';
  rowCount: number;
  columnCount: number;
  defaultRowHeight: number;
  defaultColumnWidth: number;
  rowSizes: SpreadsheetAxisSize[];
  columnSizes: SpreadsheetAxisSize[];
  rowStyleIds: Array<{ index: number; styleId: number }>;
  columnStyleIds: Array<{ index: number; styleId: number }>;
  rows: SpreadsheetRowData[];
  merges: SpreadsheetMergeRange[];
  showGridLines: boolean;
  autoFilter?: SpreadsheetAutoFilter;
  comments: Array<{ row: number; column: number; comment: SpreadsheetCellComment }>;
  charts: SpreadsheetChart[];
  images: SpreadsheetImage[];
};

export type SpreadsheetWorkbookData = {
  activeSheetIndex: number;
  sheets: SpreadsheetSheetData[];
  styles: SpreadsheetCellStyle[];
};

export type SpreadsheetWorkerRequest = {
  type: 'parse';
  buffer: ArrayBuffer;
};

export type SpreadsheetWorkerResponse =
  { type: 'ready'; workbook: SpreadsheetWorkbookData } | { type: 'error'; code: 'parse-error' | 'resource-limit'; message: string };

export type SpreadsheetAxisMetrics = {
  offsets: Float64Array;
  totalSize: number;
};

export function columnLabel(column: number): string {
  if (!Number.isInteger(column) || column < 1) throw new RangeError('Column index must be a positive integer');
  let value = column;
  let label = '';
  while (value > 0) {
    const remainder = (value - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    value = Math.floor((value - 1) / 26);
  }
  return label;
}

export function buildAxisMetrics(count: number, defaultSize: number, overrides: SpreadsheetAxisSize[]): SpreadsheetAxisMetrics {
  if (!Number.isInteger(count) || count < 0) throw new RangeError('Axis count must be a non-negative integer');
  if (!Number.isFinite(defaultSize) || defaultSize < 0) throw new RangeError('Default axis size must be non-negative');

  const overrideSizes = new Map<number, number>();
  overrides.forEach(({ index, size }) => {
    if (!Number.isInteger(index) || index < 1 || index > count) throw new RangeError(`Axis override index ${index} is outside the axis`);
    if (!Number.isFinite(size) || size < 0) throw new RangeError(`Axis override size for ${index} must be non-negative`);
    overrideSizes.set(index, size);
  });

  const offsets = new Float64Array(count + 1);
  for (let index = 1; index <= count; index += 1) {
    offsets[index] = offsets[index - 1] + (overrideSizes.get(index) ?? defaultSize);
  }
  return { offsets, totalSize: offsets[count] ?? 0 };
}

export function axisIndexAtOffset(offsets: Float64Array, offset: number): number {
  const count = offsets.length - 1;
  if (count <= 0) return 0;
  if (offset <= 0) {
    let first = 1;
    while (first < count && offsets[first] <= 0) first += 1;
    return first;
  }
  if (offset >= offsets[count]) return count;

  let low = 1;
  let high = count;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (offsets[middle] <= offset) low = middle + 1;
    else high = middle;
  }
  return low;
}

export function parseCellRange(range: string): SpreadsheetMergeRange {
  const match = /^\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)$/i.exec(range.trim());
  if (!match) throw new Error(`Invalid spreadsheet range: ${range}`);
  const [, leftLabel, topText, rightLabel, bottomText] = match;
  const left = columnNumber(leftLabel);
  const right = columnNumber(rightLabel);
  const top = Number(topText);
  const bottom = Number(bottomText);
  if (top < 1 || bottom < top || right < left) throw new Error(`Invalid spreadsheet range: ${range}`);
  return { top, left, bottom, right };
}

function columnNumber(label: string): number {
  let value = 0;
  for (const character of label.toUpperCase()) {
    const code = character.charCodeAt(0);
    if (code < 65 || code > 90) throw new Error(`Invalid spreadsheet column: ${label}`);
    value = value * 26 + code - 64;
  }
  return value;
}

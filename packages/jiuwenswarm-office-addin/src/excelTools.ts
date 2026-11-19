/**
 * Excel's tools on ``Excel.run``: reading cells (``get_cell_ranges``, ``get_range_as_csv``), writing them
 * (``set_cell_range``, ``copy_paste_range``), the model's own code (``execute_office_js``) and the workbook's state
 * (``read_surface_context``). Results are shaped as the industry add-in shapes them, which the Excel persona reads.
 * A write refused before it starts (overwrite, merged areas, a blocked formula) says so and changes nothing.
 */
import type { OfficeHost } from './appContract';
import type { ClientToolResponse, ClientToolRuntime } from './clientToolDispatcher';
import {
  type Block, blockName, cellName, cellsOf, contains, overlap, parseBlock,
} from './excelAddress';
import { firstThreat, screenExcelWrite } from './excelGuard';
import type { JsonObject } from './gatewayClient';
import type { HostWriteQueue } from './hostWriteQueue';
import { type CodeHost, runOfficeCode } from './officeCodeRunner';

/** ``Excel.run``, injected so tests can drive a fake workbook. */
export type ExcelRun = <T>(batch: (context: Excel.RequestContext) => Promise<T>) => Promise<T>;

export const EXCEL_TOOL_NAMES = ['get_cell_ranges', 'get_range_as_csv', 'set_cell_range', 'copy_paste_range',
  'execute_office_js', 'read_surface_context'] as const;
type ExcelToolName = (typeof EXCEL_TOOL_NAMES)[number];

/** Rows read in one round trip; a large range is read in blocks of this many rows. */
const CHUNK_ROWS = 500;
/** Cells a refusal names before it says how many more there are. */
const NAMED = 5;
/** Notes on cells come with ExcelApi 1.18; on an older Excel a note is left out and the result says so. */
const NOTES_SET = ['ExcelApi', '1.18'] as const;

/** A request the tool refuses before it touches the workbook; ``blocked`` when the reason is safety. */
class Refused extends Error {
  constructor(message: string, readonly blocked = false) {
    super(blocked ? `ToolExecutionBlocked: ${message}` : message);
  }
}

interface Options {
  run: ExcelRun;
  host: OfficeHost;
  /** The enums ``execute_office_js`` code sees as ``Excel``. */
  enums: Record<string, Readonly<Record<string, string>>>;
  /** The current sheet, selection and chart, as ``read_surface_context`` returns them. */
  surface: () => Promise<string>;
  /** The queue the writes run in, told where each wrote. */
  writes: HostWriteQueue;
}

export function excelRuntime(options: Options): ClientToolRuntime<ExcelToolName> {
  const tools: Record<ExcelToolName, (input: JsonObject) => Promise<ClientToolResponse>> = {
    get_cell_ranges: input => options.run(context => readCells(context, input, options.host)),
    get_range_as_csv: input => options.run(context => readCsv(context, input)),
    set_cell_range: input => options.run(context => writeCells(context, input, options.host, options.writes)),
    copy_paste_range: input => options.run(context => copyRange(context, input, options.writes)),
    execute_office_js: input => runCode(String(input.code), options),
    read_surface_context: async () => ({ ok: true, result: await options.surface() }),
  };
  return {
    tools: new Set(EXCEL_TOOL_NAMES),
    execute: async ({ toolName, input }) => {
      try {
        return await tools[toolName](input);
      } catch (error) {
        if (!(error instanceof Refused)) throw error;
        return { ok: false, error: {
          code: error.blocked ? 'TOOL_EXECUTION_BLOCKED' : 'EXCEL_REQUEST_REFUSED', message: error.message,
          retryable: false, category: 'validation', details: { outcome: 'not_applied', native_mutation_started: false },
        } };
      }
    },
  };
}

/** The code, telling the queue the ranges it wrote to. */
async function runCode(code: string, { run, enums, writes }: Options): Promise<ClientToolResponse> {
  const ranges: Excel.Range[] = [];
  const response = await runOfficeCode({ name: 'Excel', run: run as CodeHost['run'], enums, screen: screenExcelWrite,
    // Loaded ahead of the write, the address is the range's as the write found it, whatever the write does to it.
    writing: target => 'address' in target && ranges.push((target as Excel.Range).load('address')) }, code);
  for (const range of ranges) {
    try {
      writes.touch(range.address);
    } catch {
      // The code never synced this write: it did not land, and the address never came.
    }
  }
  return response;
}

/** An address of cells on a sheet, as Excel writes one. */
function onSheet(sheetName: string, cells: string): string {
  return `'${sheetName.replace(/'/g, "''")}'!${cells}`;
}

interface Sheet {
  sheet: Excel.Worksheet;
  name: string;
  /** The last used row and column, counted from A1; 0 on an empty sheet. */
  extent: { rows: number; columns: number };
}

/** The named worksheet and its used extent; a name that is not there is refused with every sheet's exact name. */
async function sheetNamed(context: Excel.RequestContext, name: string): Promise<Sheet> {
  const sheets = context.workbook.worksheets;
  sheets.load('items/name');
  const sheet = sheets.getItemOrNullObject(name);
  const used = sheet.getUsedRangeOrNullObject();
  used.load('rowIndex,columnIndex,rowCount,columnCount');
  await context.sync();
  if (sheet.isNullObject) {
    throw new Refused(`There is no worksheet named ${JSON.stringify(name)}. The worksheets are: ${
      sheets.items.map(item => JSON.stringify(item.name)).join(', ')}.`);
  }
  return { sheet, name, extent: used.isNullObject ? { rows: 0, columns: 0 }
    : { rows: used.rowIndex + used.rowCount, columns: used.columnIndex + used.columnCount } };
}

function blockOf(address: string, extent: Sheet['extent']): Block {
  const block = parseBlock(address, extent);
  if (!block) {
    throw new Refused(`${JSON.stringify(address)} is not an A1 address of one sheet's cells, such as 'A1:C10', `
      + '\'B5\', \'D:D\' or \'5:5\'; give the sheet in sheetName.');
  }
  return block;
}

const rangeOf = (sheet: Excel.Worksheet, { top, left, bottom, right }: Block): Excel.Range => (
  sheet.getRangeByIndexes(top - 1, left - 1, bottom - top + 1, right - left + 1));

function listed(cells: string[]): string {
  return cells.length > NAMED ? `${cells.slice(0, NAMED).join(', ')}… (and ${cells.length - NAMED} more)`
    : cells.join(', ');
}

const CELL_PROPERTIES: Excel.CellPropertiesLoadOptions = { format: {
  font: { bold: true, italic: true, underline: true, strikethrough: true, size: true, color: true, name: true },
  fill: { color: true }, horizontalAlignment: true, borders: { style: true, weight: true, color: true },
} };
const BORDER_STYLES: Record<string, string> = { Continuous: 'solid', Dash: 'dashed', Dot: 'dotted', Double: 'double' };

/** A cell's formatting in the industry's short keys, leaving out what every new cell has. */
function styleOf(format: Excel.CellPropertiesFormat | undefined, numberFormat: unknown): JsonObject {
  const font = format?.font ?? {};
  const style: JsonObject = {
    ...(font.bold ? { b: true } : {}), ...(font.italic ? { i: true } : {}),
    ...(font.underline && font.underline !== 'None' ? { u: String(font.underline).toLowerCase() } : {}),
    ...(font.strikethrough ? { strike: true } : {}), sz: font.size, family: font.name,
    ...(font.color && font.color.toUpperCase() !== '#000000' ? { color: font.color } : {}),
    ...(format?.fill?.color && format.fill.color.toUpperCase() !== '#FFFFFF' ? { fgColor: format.fill.color } : {}),
    ...(format?.horizontalAlignment && format.horizontalAlignment !== 'General'
      ? { alignment: format.horizontalAlignment.toLowerCase() } : {}),
    ...(typeof numberFormat === 'string' && numberFormat !== 'General' ? { numFmt: numberFormat } : {}),
  };
  return Object.fromEntries(Object.entries(style).filter(([, value]) => value !== undefined));
}

function bordersOf(format: Excel.CellPropertiesFormat | undefined): string | JsonObject | undefined {
  const sides = Object.fromEntries((['top', 'bottom', 'left', 'right'] as const).flatMap(side => {
    const border = format?.borders?.[side];
    if (!border?.style || border.style === 'None') return [];
    const style = BORDER_STYLES[border.style] ?? border.style.toLowerCase();
    return [[side, `${String(border.weight ?? 'Thin').toLowerCase()} ${style} ${border.color ?? '#000000'}`]];
  }));
  const kinds = new Set(Object.values(sides));
  if (!kinds.size) return undefined;
  return Object.keys(sides).length === 4 && kinds.size === 1 ? [...kinds][0] : sides;
}

/** The blocks that the cells form: runs along each row, stacked where rows repeat the same run. */
function blocksOf(cells: [number, number][]): Block[] {
  const sorted = [...cells].sort(([r1, c1], [r2, c2]) => r1 - r2 || c1 - c2);
  const blocks: Block[] = [];
  const open = new Map<string, Block>();
  for (const [row, column] of sorted) {
    const last = blocks.at(-1);
    if (last && last.bottom === row && last.top === row && last.right === column - 1) {
      last.right = column;
      continue;
    }
    blocks.push({ top: row, left: column, bottom: row, right: column });
  }
  const stacked: Block[] = [];
  for (const run of blocks) {
    const key = `${run.left}:${run.right}`;
    const above = open.get(key);
    if (above && above.bottom === run.top - 1) {
      above.bottom = run.top;
    } else {
      stacked.push(run);
      open.set(key, run);
    }
  }
  return stacked;
}

/** Shared formatting as ``{A1 range: formatting}``, one entry per block of cells that share it. */
function byBlocks(perCell: Map<string, { at: [number, number]; value: unknown }>): JsonObject {
  const groups = new Map<string, { value: unknown; cells: [number, number][] }>();
  for (const { at, value } of perCell.values()) {
    const key = JSON.stringify(value);
    const group = groups.get(key) ?? { value, cells: [] };
    group.cells.push(at);
    groups.set(key, group);
  }
  return Object.fromEntries([...groups.values()].flatMap(({ value, cells }) => blocksOf(cells)
    .map(block => [blockName(block), value])));
}

async function readCells(
  context: Excel.RequestContext, input: JsonObject, host: OfficeHost,
): Promise<ClientToolResponse> {
  const { sheet, name, extent } = await sheetNamed(context, String(input.sheetName));
  const blocks = (input.ranges as string[]).map(address => blockOf(address, extent));
  const includeStyles = input.includeStyles !== false;
  const limit = Number(input.cellLimit ?? 2000);
  const used: Block = { top: 1, left: 1, bottom: extent.rows, right: extent.columns };
  const inside = extent.rows ? blocks.flatMap(block => overlap(block, used) ?? []) : [];
  const notes = await notesOf(context, sheet, host);
  const merged = blocks.map(block => rangeOf(sheet, block).getMergedAreasOrNullObject());
  merged.forEach(areas => areas.load('areas/items/address'));
  const cells: JsonObject = {};
  const styles = new Map<string, { at: [number, number]; value: unknown }>();
  const borders = new Map<string, { at: [number, number]; value: unknown }>();
  const next: string[] = [];
  let count = 0;
  let full = false;
  for (const block of inside) {
    for (let top = block.top; top <= block.bottom; top += CHUNK_ROWS) {
      const chunk = { ...block, top, bottom: Math.min(block.bottom, top + CHUNK_ROWS - 1) };
      if (full || count >= limit) {
        full = true;
        next.push(blockName({ ...block, top }));
        break;
      }
      const range = rangeOf(sheet, chunk);
      range.load('values,formulas,numberFormat');
      const properties = includeStyles ? range.getCellProperties(CELL_PROPERTIES) : undefined;
      await context.sync();
      let row = 0;
      for (; row < range.values.length; row += 1) {
        const filled = range.values[row].flatMap((value, column) => {
          const at = cellName(chunk.top + row, chunk.left + column);
          const formula = range.formulas[row][column];
          const isFormula = typeof formula === 'string' && formula.startsWith('=');
          const note = notes.get(at);
          const empty = value === '' && !isFormula && note === undefined;
          return empty ? [] : [{ at, column, value, isFormula, formula, note }];
        });
        if (count && count + filled.length > limit) break;
        count += filled.length;
        for (const { at, column, value, isFormula, formula, note } of filled) {
          if (note !== undefined) cells[at] = [value, isFormula ? formula : '', note];
          else cells[at] = isFormula ? [value, formula] : value;
          if (!properties) continue;
          const format = properties.value[row][column]?.format;
          const position: [number, number] = [chunk.top + row, chunk.left + column];
          styles.set(at, { at: position, value: styleOf(format, range.numberFormat[row][column]) });
          const border = bordersOf(format);
          if (border) borders.set(at, { at: position, value: border });
        }
      }
      if (row < range.values.length) {
        full = true;
        next.push(blockName({ ...block, top: chunk.top + row }));
        break;
      }
    }
  }
  await context.sync();
  const mergedRanges = merged.flatMap(areas => (areas.isNullObject ? []
    : areas.areas.items.map(item => cellsOf(item.address))));
  return { ok: true, result: {
    worksheet: {
      name, ...(extent.rows ? { dimension: blockName(used) } : {}), cells,
      ...(includeStyles ? { styles: byBlocks(styles), borders: byBlocks(borders) } : {}),
    },
    hasMore: next.length > 0, ...(next.length ? { nextRanges: next } : {}),
    ...(mergedRanges.length ? { mergedRanges: [...new Set(mergedRanges)] } : {}),
  } };
}

/** The sheet's notes by cell, where the host has notes. */
async function notesOf(
  context: Excel.RequestContext, sheet: Excel.Worksheet, host: OfficeHost,
): Promise<Map<string, string>> {
  if (!host.isSetSupported(...NOTES_SET)) return new Map();
  const notes = sheet.notes;
  notes.load('items/content');
  await context.sync();
  const places = notes.items.map(note => note.getLocation());
  places.forEach(place => place.load('address'));
  await context.sync();
  return new Map(notes.items.map((note, index) => [cellsOf(places[index].address), note.content]));
}

async function readCsv(context: Excel.RequestContext, input: JsonObject): Promise<ClientToolResponse> {
  const { sheet, name, extent } = await sheetNamed(context, String(input.sheetName));
  const block = blockOf(String(input.range), extent);
  const rows = Number(input.maxRows ?? 500) + (input.includeHeaders === false ? 0 : 1);
  const bottom = Math.min(block.bottom, Math.max(extent.rows, block.top), block.top + rows - 1);
  const range = rangeOf(sheet, { ...block, bottom });
  range.load('values');
  await context.sync();
  const field = (value: unknown) => {
    const text = String(value ?? '');
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = range.values.map(row => row.map(field).join(','));
  return { ok: true, result: {
    csv: lines.join('\n'), rowCount: input.includeHeaders === false ? lines.length : Math.max(lines.length - 1, 0),
    columnCount: block.right - block.left + 1,
    hasMore: block.bottom > bottom && extent.rows > bottom, sheetName: name,
  } };
}

interface CellInput {
  value?: string | number | boolean;
  formula?: string;
  note?: string;
  cellStyles?: JsonObject;
  borderStyles?: Record<string, { style?: string; weight?: string; color?: string }>;
}

const BORDER_EDGES: Record<string, Excel.BorderIndex[]> = {
  top: ['EdgeTop' as Excel.BorderIndex], bottom: ['EdgeBottom' as Excel.BorderIndex],
  left: ['EdgeLeft' as Excel.BorderIndex], right: ['EdgeRight' as Excel.BorderIndex],
  insideHorizontal: ['InsideHorizontal' as Excel.BorderIndex], insideVertical: ['InsideVertical' as Excel.BorderIndex],
  all: ['EdgeTop', 'EdgeBottom', 'EdgeLeft', 'EdgeRight', 'InsideHorizontal', 'InsideVertical'] as Excel.BorderIndex[],
};
const LINE_STYLES: Record<string, string> = { solid: 'Continuous', dashed: 'Dash', dotted: 'Dot', double: 'Double',
  none: 'None' };

function applyStyles(range: Excel.Range, styles: JsonObject): void {
  const { font } = range.format;
  if (styles.fontColor !== undefined) font.color = String(styles.fontColor);
  if (styles.fontSize !== undefined) font.size = Number(styles.fontSize);
  if (styles.fontFamily !== undefined) font.name = String(styles.fontFamily);
  if (styles.fontWeight !== undefined) font.bold = styles.fontWeight === 'bold';
  if (styles.fontStyle !== undefined) font.italic = styles.fontStyle === 'italic';
  if (styles.fontLine !== undefined) {
    font.underline = (styles.fontLine === 'underline' ? 'Single' : 'None') as Excel.RangeUnderlineStyle;
    font.strikethrough = styles.fontLine === 'line-through';
  }
  if (styles.backgroundColor !== undefined) range.format.fill.color = String(styles.backgroundColor);
  if (styles.horizontalAlignment !== undefined) {
    range.format.horizontalAlignment = (String(styles.horizontalAlignment)[0].toUpperCase()
      + String(styles.horizontalAlignment).slice(1)) as Excel.HorizontalAlignment;
  }
  if (styles.numberFormat !== undefined) range.numberFormat = [[String(styles.numberFormat)]];
}

function applyBorders(range: Excel.Range, sides: NonNullable<CellInput['borderStyles']>): void {
  for (const [side, line] of Object.entries(sides)) {
    for (const edge of BORDER_EDGES[side]) {
      const border = range.format.borders.getItem(edge);
      if (line.style) border.style = LINE_STYLES[line.style] as Excel.BorderLineStyle;
      if (line.weight) border.weight = (line.weight[0].toUpperCase() + line.weight.slice(1)) as Excel.BorderWeight;
      if (line.color) border.color = line.color;
    }
  }
}

function resize(range: Excel.Range, width?: JsonObject, height?: JsonObject): void {
  if (width?.type === 'points') range.format.columnWidth = Number(width.value);
  if (width?.type === 'standard') range.format.useStandardWidth = true;
  if (width?.type === 'autofit') range.format.autofitColumns();
  if (height?.type === 'points') range.format.rowHeight = Number(height.value);
  if (height?.type === 'standard') range.format.useStandardHeight = true;
  if (height?.type === 'autofit') range.format.autofitRows();
}

/** A value as ``set_cell_range`` writes it: text Excel would take for a formula stays text. */
function written(value: unknown): unknown {
  return typeof value === 'string' && value.startsWith('=') ? `'${value}` : value;
}

async function writeCells(
  context: Excel.RequestContext, input: JsonObject, host: OfficeHost, writes: HostWriteQueue,
): Promise<ClientToolResponse> {
  const entries = Object.entries(input.cells as Record<string, CellInput>).map(([address, cell]) => {
    const block = parseBlock(address, { rows: 0, columns: 0 });
    if (!block || block.top !== block.bottom || block.left !== block.right) {
      throw new Refused(`${JSON.stringify(address)} is not one cell; cells are keyed by A1 addresses such as 'B5'.`);
    }
    return { at: cellName(block.top, block.left), block, cell };
  });
  const threat = firstThreat(entries.map(({ cell }) => [cell.formula, written(cell.value)]));
  if (threat) throw new Refused(`${threat} Nothing was written.`, true);
  const { sheet } = await sheetNamed(context, String(input.sheetName));
  const filled = entries.filter(({ cell }) => cell.value !== undefined || cell.formula !== undefined);
  const notes = host.isSetSupported(...NOTES_SET) ? entries.filter(({ cell }) => cell.note) : [];
  const existing = notes.map(({ at }) => sheet.notes.getItemOrNullObject(at));
  existing.forEach(note => note.load('content'));
  const span = (list: typeof entries): Block => list.map(({ block }) => block).reduce((a, b) => ({
    top: Math.min(a.top, b.top), left: Math.min(a.left, b.left),
    bottom: Math.max(a.bottom, b.bottom), right: Math.max(a.right, b.right) }));
  const before = filled.length && input.allow_overwrite !== true ? rangeOf(sheet, span(filled)) : undefined;
  before?.load('formulas');
  await context.sync();
  if (before) {
    const origin = span(filled);
    const taken = filled.filter(({ block }) => before.formulas[block.top - origin.top][block.left - origin.left] !== '')
      .map(({ at }) => at);
    if (taken.length) {
      throw new Refused(`Would overwrite ${taken.length} non-empty cell${taken.length > 1 ? 's' : ''}: `
        + `${listed(taken)}. To proceed with overwriting existing data, retry with allow_overwrite set to true.`);
    }
  }
  const messages: string[] = [];
  for (const { at, cell } of entries) {
    const range = sheet.getRange(at);
    if (cell.formula !== undefined) range.formulas = [[cell.formula]];
    else if (cell.value !== undefined) range.values = [[written(cell.value)]];
    if (cell.formula !== undefined || cell.value !== undefined) writes.touch(onSheet(String(input.sheetName), at));
    if (cell.cellStyles) applyStyles(range, cell.cellStyles);
    if (cell.borderStyles) applyBorders(range, cell.borderStyles);
  }
  notes.forEach(({ at, cell }, index) => {
    if (existing[index].isNullObject) sheet.notes.add(at, cell.note!);
    else existing[index].content = cell.note!;
  });
  const unsupportedNotes = host.isSetSupported(...NOTES_SET) ? [] : entries.filter(({ cell }) => cell.note);
  if (unsupportedNotes.length) {
    messages.push(`Notes for ${listed(unsupportedNotes.map(({ at }) => at))} were left out: this Excel has no notes `
      + '(they need ExcelApi 1.18).');
  }
  if (input.resizeWidth || input.resizeHeight) {
    resize(rangeOf(sheet, span(entries)), input.resizeWidth as JsonObject, input.resizeHeight as JsonObject);
  }
  const formulas = entries.filter(({ cell }) => cell.formula !== undefined)
    .map(({ at }) => ({ at, range: sheet.getRange(at).load('values') }));
  await context.sync();
  const formulaResults = Object.fromEntries(formulas.map(({ at, range }) => [at, range.values[0][0]]));
  return {
    ok: true,
    result: {
      ...(formulas.length ? { formula_results: formulaResults } : {}), ...(messages.length ? { messages } : {}),
    },
    mutationEvidence: { outcome: 'applied', native_mutation_started: true },
  };
}

async function activeSheetName(context: Excel.RequestContext): Promise<string> {
  const active = context.workbook.worksheets.getActiveWorksheet();
  active.load('name');
  await context.sync();
  return active.name;
}

const COPY_TYPES: Record<string, Excel.RangeCopyType> = {
  all: 'All' as Excel.RangeCopyType, formats: 'Formats' as Excel.RangeCopyType,
  values: 'Values' as Excel.RangeCopyType, formulas: 'Formulas' as Excel.RangeCopyType,
};

async function copyRange(
  context: Excel.RequestContext, input: JsonObject, writes: HostWriteQueue,
): Promise<ClientToolResponse> {
  const sourceName = typeof input.sourceSheetName === 'string' ? input.sourceSheetName : await activeSheetName(context);
  const destName = String(input.destSheetName ?? sourceName);
  const source = await sheetNamed(context, sourceName);
  const dest = destName === sourceName ? source : await sheetNamed(context, destName);
  const copyType = String(input.copyType ?? 'all');
  const from = blockOf(String(input.sourceRange), source.extent);
  const to = blockOf(String(input.destRange), dest.extent);
  const area = { top: to.top, left: to.left,
    bottom: to.top + Math.max(to.bottom - to.top, from.bottom - from.top),
    right: to.left + Math.max(to.right - to.left, from.right - from.left) };
  const sourceLabel = `${sourceName}!${blockName(from)}`;
  const used = overlap(from, { top: 1, left: 1, bottom: source.extent.rows, right: source.extent.columns });
  const sourceCells = used && copyType !== 'values' && copyType !== 'formats' ? rangeOf(source.sheet, used) : undefined;
  sourceCells?.load('formulas');
  const mergedAt = (sheet: Excel.Worksheet, block: Block) => {
    const areas = rangeOf(sheet, block).getMergedAreasOrNullObject();
    areas.load('areas/items/address');
    return areas;
  };
  const destMerged = copyType === 'formats' ? undefined : mergedAt(dest.sheet, area);
  const sourceMerged = copyType === 'formats' ? undefined : mergedAt(source.sheet, from);
  const before = input.allow_overwrite === true || copyType === 'formats' ? undefined : rangeOf(dest.sheet, area);
  before?.load('formulas');
  await context.sync();
  const threat = sourceCells && firstThreat(sourceCells.formulas);
  if (threat) {
    throw new Refused(`${threat} The source range ${sourceLabel} holds it, and copying would spread it. Use copyType `
      + '\'values\' to carry computed results only, or tell the user why the copy was refused.', true);
  }
  const blocksIn = (areas: Excel.RangeAreas | undefined) => (areas && !areas.isNullObject
    ? areas.areas.items.map(item => cellsOf(item.address)) : []);
  const partial = (areas: string[], within: Block) => areas.filter(address => {
    const block = parseBlock(address, { rows: 0, columns: 0 })!;
    return overlap(block, within) && !contains(within, block);
  });
  const destAreas = blocksIn(destMerged);
  const cut = partial(destAreas, area);
  if (cut.length) {
    throw new Refused(`Copy blocked: the destination cuts across part of merged cell area(s) ${cut.map(address => (
      `${destName}!${address}`)).join(', ')}. Excel cannot change part of a merged cell. Unmerge the area first, `
      + 'change the destination to cover or avoid it, or ask the user how to handle the merged cells.');
  }
  if (before) {
    const taken = before.formulas.flatMap((row, r) => row.flatMap((formula, c) => (formula === '' ? []
      : [cellName(area.top + r, area.left + c)])));
    if (taken.length) {
      throw new Refused(`Would overwrite non-empty cell${taken.length > 1 ? 's' : ''} in ${destName}!`
        + `${blockName(area)}: ${listed(taken)}. Confirm with the user that overwriting is intended, then retry with `
        + 'allow_overwrite: true.');
    }
  }
  const messages: string[] = [];
  const sourceCut = partial(blocksIn(sourceMerged), from);
  if (sourceCut.length) {
    messages.push(`The source range cuts across part of merged cell area(s) ${sourceCut.join(', ')}; if the copy `
      + 'looks wrong, expand the source to the whole merged area or unmerge it first.');
  }
  const replaced = copyType === 'all' ? destAreas.filter(address => !cut.includes(address)) : [];
  if (replaced.length) {
    messages.push(`The destination held merged cell area(s) ${replaced.join(', ')}, which the copy replaced with `
      + 'their contents and layout; tell the user if those were headers they wanted kept.');
  }
  // The blocks checked above, not the addresses: a whole column is checked as far as its sheet is used only.
  rangeOf(dest.sheet, area).copyFrom(rangeOf(source.sheet, from), COPY_TYPES[copyType]);
  writes.touch(onSheet(destName, blockName(area)));
  await context.sync();
  if (destName !== sourceName && (copyType === 'all' || copyType === 'formulas')) {
    messages.push('Copied to another sheet: relative references now point at the destination sheet. If the source '
      + 'formulas referred to cells outside the copied block, check them with get_cell_ranges.');
  }
  return {
    ok: true,
    result: { copied: `${sourceLabel} → ${destName}!${blockName(area)} (copyType: ${copyType})`,
      ...(messages.length ? { messages } : {}) },
    mutationEvidence: { outcome: 'applied', native_mutation_started: true },
  };
}

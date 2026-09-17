import ExcelJS, { type Border, type Cell, type Color, type Fill, type Font, type Style, type Workbook, type Worksheet } from 'exceljs';
import JSZip from 'jszip';
import { SaxesParser, type SaxesTagNS } from 'saxes';
import SSF from 'ssf';
import { officeFontStack } from './officeFontStack';
import { SPREADSHEET_ARCHIVE_LIMITS, inspectOoxmlArchive } from './ooxmlArchiveLimits';
import {
  parseCellRange,
  type SpreadsheetCellData,
  type SpreadsheetCellStyle,
  type SpreadsheetChart,
  type SpreadsheetChartSeries,
  type SpreadsheetDrawingAnchor,
  type SpreadsheetImage,
  type SpreadsheetSheetData,
  type SpreadsheetWorkbookData,
} from './spreadsheetPreviewModel';

export { officeFontStack } from './officeFontStack';

type ExtendedColor = Partial<Color> & {
  indexed?: number;
  tint?: number;
};

type RuntimeCellModel = {
  address: string;
};

type RuntimeRowModel = {
  number: number;
  height?: number;
  hidden?: boolean;
  style?: Partial<Style>;
  cells: RuntimeCellModel[];
};

type RuntimeColumnModel = {
  min: number;
  max: number;
  width?: number;
  hidden?: boolean;
  style?: Partial<Style>;
};

type RuntimeWorksheetModel = {
  rows?: RuntimeRowModel[];
  cols?: RuntimeColumnModel[];
  merges?: string[];
};

type RuntimeConditionalFormattingRule = {
  type: 'cellIs' | 'containsText' | 'notContainsText' | 'beginsWith' | 'endsWith' | 'colorScale' | 'dataBar' | string;
  operator?: string;
  formulae?: string[];
  text?: string;
  style?: Partial<Style>;
  color?: ExtendedColor;
  colorScale?: { cfvo: Array<{ type: string; value?: number }>; color: ExtendedColor[] };
};

type RuntimeConditionalFormatting = {
  ref: string;
  rules: RuntimeConditionalFormattingRule[];
};

type RuntimeWorksheetView = {
  showGridLines?: boolean;
};

type SheetArtifacts = {
  comments: SpreadsheetSheetData['comments'];
  charts: SpreadsheetChart[];
  images: SpreadsheetImage[];
};

type WorkbookThemes = {
  theme1?: string;
};

const DEFAULT_THEME_COLORS = ['FFFFFF', '000000', 'EEECE1', '1F497D', '4F81BD', 'C0504D', '9BBB59', '8064A2', '4BACC6', 'F79646', '0000FF', '800080'];

const INDEXED_COLORS = [
  '000000',
  'FFFFFF',
  'FF0000',
  '00FF00',
  '0000FF',
  'FFFF00',
  'FF00FF',
  '00FFFF',
  '000000',
  'FFFFFF',
  'FF0000',
  '00FF00',
  '0000FF',
  'FFFF00',
  'FF00FF',
  '00FFFF',
  '800000',
  '008000',
  '000080',
  '808000',
  '800080',
  '008080',
  'C0C0C0',
  '808080',
  '9999FF',
  '993366',
  'FFFFCC',
  'CCFFFF',
  '660066',
  'FF8080',
  '0066CC',
  'CCCCFF',
  '000080',
  'FF00FF',
  'FFFF00',
  '00FFFF',
  '800080',
  '800000',
  '008080',
  '0000FF',
  '00CCFF',
  'CCFFFF',
  'CCFFCC',
  'FFFF99',
  '99CCFF',
  'FF99CC',
  'CC99FF',
  'FFCC99',
  '3366FF',
  '33CCCC',
  '99CC00',
  'FFCC00',
  'FF9900',
  'FF6600',
  '666699',
  '969696',
  '003366',
  '339966',
  '003300',
  '333300',
  '993300',
  '993366',
  '333399',
  '333333',
];

const THEME_COLOR_NAMES = ['lt1', 'dk1', 'lt2', 'dk2', 'accent1', 'accent2', 'accent3', 'accent4', 'accent5', 'accent6', 'hlink', 'folHlink'];
const DRAWING_ARCHIVE_ENTRY = /^xl\/drawings\/(?:_rels\/)?drawing\d+\.xml(?:\.rels)?$/i;
const DRAWING_RELATIONSHIP = /\/relationships\/drawing$/;
const COMMENTS_RELATIONSHIP = /\/relationships\/comments$/;
const CHART_RELATIONSHIP = /\/relationships\/chart$/;
const IMAGE_RELATIONSHIP = /\/relationships\/image$/;

type Relationship = { id: string; target: string; type: string };
type SheetArchiveReference = { name: string; path: string };
type RelationshipCollection = Relationship[] & { get: (id: string) => Relationship | undefined };

class StyleRegistry {
  readonly styles: SpreadsheetCellStyle[] = [{}];
  private readonly indexes = new Map<string, number>([['{}', 0]]);

  add(style: SpreadsheetCellStyle): number {
    const key = JSON.stringify(style);
    const existing = this.indexes.get(key);
    if (existing !== undefined) return existing;
    const index = this.styles.length;
    this.styles.push(style);
    this.indexes.set(key, index);
    return index;
  }
}

export async function parseSpreadsheetWorkbook(buffer: ArrayBuffer): Promise<SpreadsheetWorkbookData> {
  inspectOoxmlArchive(buffer, SPREADSHEET_ARCHIVE_LIMITS);
  const archive = await JSZip.loadAsync(buffer);
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.load(await prepareWorkbookBuffer(archive, buffer), { ignoreNodes: ['drawing'] });
  if (workbook.worksheets.length === 0) throw new Error('The workbook contains no worksheets');

  const palette = parseThemePalette(workbook);
  const registry = new StyleRegistry();
  const artifacts = await parseWorkbookArtifacts(archive, workbook);
  const sheets = workbook.worksheets.map(worksheet => buildSheetData(workbook, worksheet, palette, registry, artifacts.get(worksheet.name)));
  if (!sheets.some(sheet => sheet.state === 'visible')) throw new Error('The workbook contains no visible worksheets');
  const requestedActiveIndex = workbook.views?.[0]?.activeTab ?? 0;

  return {
    activeSheetIndex: Math.min(Math.max(requestedActiveIndex, 0), sheets.length - 1),
    sheets,
    styles: registry.styles,
  };
}

async function prepareWorkbookBuffer(archive: JSZip, buffer: ArrayBuffer): Promise<ArrayBuffer> {
  const drawingEntries = Object.keys(archive.files).filter(name => DRAWING_ARCHIVE_ENTRY.test(name));
  if (drawingEntries.length === 0) return buffer;

  // JSZip's clone shares file records. Re-open the source so removing records
  // for ExcelJS cannot remove the drawing payload we render ourselves.
  const parserArchive = await JSZip.loadAsync(buffer);
  drawingEntries.forEach(name => parserArchive.remove(name));
  return parserArchive.generateAsync({ type: 'arraybuffer' });
}

function buildSheetData(
  workbook: Workbook,
  worksheet: Worksheet,
  palette: string[],
  registry: StyleRegistry,
  artifacts?: SheetArtifacts,
): SpreadsheetSheetData {
  const model = worksheet.model as unknown as RuntimeWorksheetModel;
  const rows = model.rows ?? [];
  const columns = model.cols ?? [];
  const merges = (model.merges ?? []).map(parseCellRange);
  let rowCount = 0;
  let columnCount = 0;

  rows.forEach(row => {
    rowCount = Math.max(rowCount, row.number);
    row.cells.forEach(cell => {
      const location = decodeCellAddress(cell.address);
      columnCount = Math.max(columnCount, location.column);
    });
  });
  merges.forEach(range => {
    rowCount = Math.max(rowCount, range.bottom);
    columnCount = Math.max(columnCount, range.right);
  });
  [...(artifacts?.charts ?? []), ...(artifacts?.images ?? [])].forEach(({ anchor }) => {
    rowCount = Math.max(
      rowCount,
      anchor.toRow ?? anchor.fromRow + Math.ceil((anchor.height ?? 0) / pointsToPixels(worksheet.properties.defaultRowHeight ?? 15)),
    );
    columnCount = Math.max(
      columnCount,
      anchor.toColumn ?? anchor.fromColumn + Math.ceil((anchor.width ?? 0) / columnWidthToPixels(worksheet.properties.defaultColWidth ?? 8.43)),
    );
  });

  const rowSizes: SpreadsheetSheetData['rowSizes'] = [];
  const rowStyleIds: SpreadsheetSheetData['rowStyleIds'] = [];
  const rowData: SpreadsheetSheetData['rows'] = [];

  rows.forEach(rowModel => {
    if (rowModel.hidden || rowModel.height !== undefined) {
      rowSizes.push({
        index: rowModel.number,
        size: rowModel.hidden ? 0 : pointsToPixels(rowModel.height ?? worksheet.properties.defaultRowHeight ?? 15),
      });
    }
    const rowStyleId = registry.add(styleToCss(rowModel.style ?? {}, palette));
    if (rowStyleId !== 0) rowStyleIds.push({ index: rowModel.number, styleId: rowStyleId });

    const cells: SpreadsheetCellData[] = [];
    rowModel.cells.forEach(cellModel => {
      const location = decodeCellAddress(cellModel.address);
      const cell = worksheet.findCell(location.row, location.column);
      if (!cell || cell.type === ExcelJS.ValueType.Merge) return;
      const cellData = buildCellData(workbook, cell, palette, registry);
      if (cellData.text || cellData.formula || cellData.hyperlink || cellData.styleId !== 0) cells.push(cellData);
    });
    if (cells.length > 0) rowData.push({ index: rowModel.number, styleId: rowStyleId, cells });
  });

  const columnSizes: SpreadsheetSheetData['columnSizes'] = [];
  const columnStyleIds: SpreadsheetSheetData['columnStyleIds'] = [];
  columns.forEach(columnModel => {
    const styleId = registry.add(styleToCss(columnModel.style ?? {}, palette));
    for (let index = columnModel.min; index <= columnModel.max; index += 1) {
      columnCount = Math.max(columnCount, index);
      if (columnModel.hidden || columnModel.width !== undefined) {
        columnSizes.push({
          index,
          size: columnModel.hidden ? 0 : columnWidthToPixels(columnModel.width ?? worksheet.properties.defaultColWidth ?? 8.43),
        });
      }
      if (styleId !== 0) columnStyleIds.push({ index, styleId });
    }
  });

  applyConditionalFormats(worksheet, rowData, palette, registry);

  const defaultView = worksheet.views?.[0] as RuntimeWorksheetView | undefined;
  const showGridLines = defaultView?.showGridLines ?? worksheet.properties.showGridLines ?? true;

  return {
    id: worksheet.id,
    name: worksheet.name,
    state: worksheet.state,
    rowCount,
    columnCount,
    defaultRowHeight: pointsToPixels(worksheet.properties.defaultRowHeight ?? 15),
    defaultColumnWidth: columnWidthToPixels(worksheet.properties.defaultColWidth ?? 8.43),
    rowSizes,
    columnSizes,
    rowStyleIds,
    columnStyleIds,
    rows: rowData,
    merges,
    showGridLines,
    autoFilter: parseAutoFilter(worksheet.autoFilter),
    comments: artifacts?.comments ?? [],
    charts: artifacts?.charts ?? [],
    images: artifacts?.images ?? [],
  };
}

function applyConditionalFormats(worksheet: Worksheet, rows: SpreadsheetSheetData['rows'], palette: string[], registry: StyleRegistry): void {
  const formats = (worksheet as Worksheet & { conditionalFormattings?: RuntimeConditionalFormatting[] }).conditionalFormattings ?? [];
  if (formats.length === 0) return;
  const cells = new Map(rows.flatMap(row => row.cells.map(cell => [cellKey(cell.row, cell.column), cell])));

  formats.forEach(format => {
    const ranges = format.ref.split(/\s+/).filter(Boolean).map(parseCellRange);
    format.rules.forEach(rule => {
      const rangeCells = ranges.flatMap(range => cellsInRange(range, cells, worksheet));
      if (rule.type === 'colorScale') {
        applyColorScale(rule, rangeCells, palette, registry);
        return;
      }
      if (rule.type === 'dataBar') {
        applyDataBar(rule, rangeCells, palette, registry);
        return;
      }
      rangeCells.forEach(({ data, value }) => {
        if (!matchesConditionalRule(rule, data.text, value, worksheet)) return;
        const overlay = styleToCss(rule.style ?? {}, palette);
        if (Object.keys(overlay).length > 0) data.styleId = registry.add({ ...(registry.styles[data.styleId] ?? {}), ...overlay });
      });
    });
  });
}

function cellsInRange(
  range: ReturnType<typeof parseCellRange>,
  cells: Map<string, SpreadsheetCellData>,
  worksheet: Worksheet,
): Array<{ data: SpreadsheetCellData; value: number }> {
  const result: Array<{ data: SpreadsheetCellData; value: number }> = [];
  for (let row = range.top; row <= range.bottom; row += 1) {
    for (let column = range.left; column <= range.right; column += 1) {
      const data = cells.get(cellKey(row, column));
      const raw = chartCellValue(worksheet.getCell(row, column).value);
      const value = typeof raw === 'number' ? raw : Number(raw);
      if (data && Number.isFinite(value)) result.push({ data, value });
      else if (data && !Number.isFinite(value)) result.push({ data, value: Number.NaN });
    }
  }
  return result;
}

function matchesConditionalRule(rule: RuntimeConditionalFormattingRule, text: string, value: number, worksheet: Worksheet): boolean {
  const formula = rule.formulae?.[0];
  const criterion = formula === undefined ? undefined : resolveConditionalFormula(formula, worksheet);
  if (rule.type === 'cellIs' && criterion !== undefined && Number.isFinite(value)) {
    const second = rule.formulae?.[1] === undefined ? undefined : resolveConditionalFormula(rule.formulae[1], worksheet);
    if (rule.operator === 'between') return second !== undefined && value >= Number(criterion) && value <= Number(second);
    if (rule.operator === 'notBetween') return second !== undefined && (value < Number(criterion) || value > Number(second));
    if (rule.operator === 'equal') return value === Number(criterion);
    if (rule.operator === 'notEqual') return value !== Number(criterion);
    if (rule.operator === 'greaterThan') return value > Number(criterion);
    if (rule.operator === 'greaterThanOrEqual') return value >= Number(criterion);
    if (rule.operator === 'lessThan') return value < Number(criterion);
    if (rule.operator === 'lessThanOrEqual') return value <= Number(criterion);
  }
  const needle = String(rule.text ?? criterion ?? '');
  if (rule.type === 'containsText') return needle.length > 0 && text.includes(needle);
  if (rule.type === 'notContainsText') return needle.length > 0 && !text.includes(needle);
  if (rule.type === 'beginsWith') return needle.length > 0 && text.startsWith(needle);
  if (rule.type === 'endsWith') return needle.length > 0 && text.endsWith(needle);
  return false;
}

function resolveConditionalFormula(formula: string, worksheet: Worksheet): string | number | undefined {
  const normalized = formula.trim().replace(/^=/, '');
  if (/^-?(?:\d+\.?\d*|\.\d+)$/.test(normalized)) return Number(normalized);
  const reference = /^\$?([A-Z]+)\$?(\d+)$/i.exec(normalized);
  if (reference) return chartCellValue(worksheet.getCell(`${reference[1]}${reference[2]}`).value);
  return /^"(?:[^"]|"")*"$/.test(normalized) ? normalized.slice(1, -1).replace(/""/g, '"') : undefined;
}

function applyColorScale(
  rule: RuntimeConditionalFormattingRule,
  cells: Array<{ data: SpreadsheetCellData; value: number }>,
  palette: string[],
  registry: StyleRegistry,
): void {
  const finite = cells.filter(item => Number.isFinite(item.value));
  const colors = rule.colorScale?.color.map(color => resolveColor(color, palette)).filter((color): color is string => Boolean(color)) ?? [];
  if (finite.length === 0 || colors.length < 2) return;
  const min = Math.min(...finite.map(item => item.value));
  const max = Math.max(...finite.map(item => item.value));
  finite.forEach(({ data, value }) => {
    const ratio = max === min ? 0.5 : (value - min) / (max - min);
    const color = interpolateColor(colors, ratio);
    data.styleId = registry.add({ ...(registry.styles[data.styleId] ?? {}), backgroundColor: color });
  });
}

function applyDataBar(
  rule: RuntimeConditionalFormattingRule,
  cells: Array<{ data: SpreadsheetCellData; value: number }>,
  palette: string[],
  registry: StyleRegistry,
): void {
  const finite = cells.filter(item => Number.isFinite(item.value));
  if (finite.length === 0) return;
  const color = resolveColor(rule.color, palette) ?? '#638EC6';
  const min = Math.min(0, ...finite.map(item => item.value));
  const max = Math.max(...finite.map(item => item.value));
  const span = Math.max(1, max - min);
  finite.forEach(({ data, value }) => {
    const percentage = Math.max(0, Math.min(100, ((value - min) / span) * 100));
    data.styleId = registry.add({
      ...(registry.styles[data.styleId] ?? {}),
      backgroundImage: `linear-gradient(to right, ${color} 0 ${percentage}%, transparent ${percentage}% 100%)`,
    });
  });
}

function interpolateColor(colors: string[], ratio: number): string {
  const position = Math.max(0, Math.min(1, ratio)) * (colors.length - 1);
  const lower = Math.floor(position);
  const upper = Math.min(colors.length - 1, lower + 1);
  const fraction = position - lower;
  const start = hexToRgb(colors[lower].replace('#', ''));
  const end = hexToRgb(colors[upper].replace('#', ''));
  return `rgb(${start.map((value, index) => Math.round(value + (end[index] - value) * fraction)).join(', ')})`;
}

function buildCellData(workbook: Workbook, cell: Cell, palette: string[], registry: StyleRegistry): SpreadsheetCellData {
  const value = cell.value;
  let formula: string | undefined;
  let hyperlink: string | undefined;
  let displayValue: unknown = value;

  if (isFormulaValue(value)) {
    formula = 'formula' in value ? value.formula : cell.formula;
    displayValue = value.result === undefined ? `=${formula}` : value.result;
  } else if (isHyperlinkValue(value)) {
    displayValue = value.text;
    hyperlink = safeHyperlink(value.hyperlink);
  } else if (isRichTextValue(value)) {
    displayValue = value.richText.map(part => part.text).join('');
  } else if (isCellError(value)) {
    displayValue = value.error;
  }

  return {
    row: Number(cell.row),
    column: Number(cell.col),
    text: formatCellValue(displayValue, cell.numFmt, workbook.properties.date1904),
    formula,
    hyperlink,
    alignRight: typeof displayValue === 'number' || displayValue instanceof Date,
    styleId: registry.add(styleToCss(cell.style, palette)),
  };
}

function formatCellValue(value: unknown, numberFormat: string | undefined, date1904: boolean): string {
  if (value === null || value === undefined) return '';
  if (value instanceof Date) return SSF.format(numberFormat || 'm/d/yy', value, { date1904 });
  if (typeof value === 'number') return SSF.format(numberFormat || 'General', value, { date1904 });
  if (typeof value === 'boolean') return value ? 'TRUE' : 'FALSE';
  return String(value);
}

function styleToCss(style: Partial<Style>, palette: string[]): SpreadsheetCellStyle {
  const css: SpreadsheetCellStyle = {};
  applyFont(css, style.font, palette);
  applyFill(css, style.fill, palette);
  applyAlignment(css, style.alignment);
  applyBorders(css, style.border, palette);
  return css;
}

function applyFont(css: SpreadsheetCellStyle, font: Partial<Font> | undefined, palette: string[]): void {
  if (!font) return;
  if (font.name) css.fontFamily = officeFontStack(font.name);
  if (font.size) {
    css.fontSize = `${pointsToPixels(font.size)}px`;
    // Tailwind's text-size utility also sets a fixed 16px line-height. That
    // line-height is smaller than many workbook title fonts and clips their
    // descenders inside the row's exact Excel height.
    css.lineHeight = 'normal';
  }
  if (font.bold) css.fontWeight = 700;
  if (font.italic) css.fontStyle = 'italic';
  if (font.color) {
    const color = resolveColor(font.color, palette);
    if (color) css.color = color;
  }

  const decorations: string[] = [];
  if (font.underline && font.underline !== 'none') decorations.push('underline');
  if (font.strike) decorations.push('line-through');
  if (decorations.length > 0) css.textDecorationLine = decorations.join(' ');
  if (font.underline === 'double' || font.underline === 'doubleAccounting') css.textDecorationStyle = 'double';
  if (font.vertAlign === 'superscript') css.verticalAlign = 'super';
  if (font.vertAlign === 'subscript') css.verticalAlign = 'sub';
}

function applyFill(css: SpreadsheetCellStyle, fill: Fill | undefined, palette: string[]): void {
  if (!fill) return;
  if (fill.type === 'pattern') {
    if (fill.pattern === 'none') return;
    const foreground = resolveColor(fill.fgColor, palette);
    const background = resolveColor(fill.bgColor, palette);
    if (fill.pattern === 'solid') {
      if (foreground) css.backgroundColor = foreground;
      return;
    }
    if (background) css.backgroundColor = background;
    if (foreground) css.backgroundImage = `repeating-linear-gradient(45deg, ${foreground} 0 1px, transparent 1px 4px)`;
    return;
  }

  if (fill.type === 'gradient') {
    const stops = fill.stops
      .map(stop => {
        const color = resolveColor(stop.color, palette);
        return color ? `${color} ${Math.round(stop.position * 10000) / 100}%` : null;
      })
      .filter((stop): stop is string => Boolean(stop));
    if (stops.length === 0) return;
    if (fill.gradient === 'angle') {
      css.backgroundImage = `linear-gradient(${normalizeDegrees(fill.degree + 90)}deg, ${stops.join(', ')})`;
    } else {
      css.backgroundImage = `radial-gradient(circle at ${fill.center.left * 100}% ${fill.center.top * 100}%, ${stops.join(', ')})`;
    }
  }
}

function applyAlignment(css: SpreadsheetCellStyle, alignment: Partial<Style['alignment']> | undefined): void {
  if (!alignment) return;
  if (alignment.horizontal) {
    css.justifyContent =
      alignment.horizontal === 'center' || alignment.horizontal === 'centerContinuous'
        ? 'center'
        : alignment.horizontal === 'right'
          ? 'flex-end'
          : alignment.horizontal === 'distributed'
            ? 'space-between'
            : 'flex-start';
    if (alignment.horizontal === 'justify') css.textAlign = 'justify';
  }
  if (alignment.vertical) {
    css.alignItems = alignment.vertical === 'top' ? 'flex-start' : alignment.vertical === 'middle' ? 'center' : 'flex-end';
  }
  if (alignment.wrapText) {
    css.whiteSpace = 'normal';
    css.overflowWrap = 'anywhere';
  }
  if (alignment.indent) css.paddingLeft = `${8 + alignment.indent * 9}px`;
  if (alignment.readingOrder === 'rtl') css.direction = 'rtl';
}

function applyBorders(css: SpreadsheetCellStyle, borders: Partial<Style['border']> | undefined, palette: string[]): void {
  if (!borders) return;
  applyBorder(css, 'Top', borders.top, palette);
  applyBorder(css, 'Right', borders.right, palette);
  applyBorder(css, 'Bottom', borders.bottom, palette);
  applyBorder(css, 'Left', borders.left, palette);
}

function applyBorder(css: SpreadsheetCellStyle, side: 'Top' | 'Right' | 'Bottom' | 'Left', border: Partial<Border> | undefined, palette: string[]): void {
  if (!border?.style) return;
  const borderStyle =
    border.style === 'dotted' || border.style === 'hair' ? 'dotted' : border.style.includes('dash') ? 'dashed' : border.style === 'double' ? 'double' : 'solid';
  const borderWidth =
    border.style === 'hair' || border.style === 'thin' || border.style === 'dotted' || border.style === 'dashed' ? 1 : border.style === 'thick' ? 3 : 2;
  const color = resolveColor(border.color, palette) ?? '#000000';
  css[`border${side}`] = `${borderWidth}px ${borderStyle} ${color}`;
}

function resolveColor(color: ExtendedColor | undefined, palette: string[]): string | undefined {
  if (!color) return undefined;
  let hex: string | undefined;
  let alpha = 'FF';

  if (color.argb && /^[0-9a-f]{8}$/i.test(color.argb)) {
    alpha = color.argb.slice(0, 2);
    hex = color.argb.slice(2);
  } else if (color.argb && /^[0-9a-f]{6}$/i.test(color.argb)) {
    hex = color.argb;
  } else if (color.theme !== undefined) {
    hex = palette[color.theme];
  } else if (color.indexed !== undefined) {
    hex = INDEXED_COLORS[color.indexed];
  }
  if (!hex) return undefined;
  const tinted = applyTint(hex, color.tint ?? 0);
  // SpreadsheetML stores RGB colours as 00RRGGBB as well as FFRRGGBB. Cell
  // formatting has no alpha channel, so the leading byte is not transparency.
  const opacity = alpha === '00' ? 1 : Number.parseInt(alpha, 16) / 255;
  if (opacity >= 1) return `#${tinted}`;
  const [red, green, blue] = hexToRgb(tinted);
  return `rgba(${red}, ${green}, ${blue}, ${Math.round(opacity * 1000) / 1000})`;
}

async function parseWorkbookArtifacts(archive: JSZip, workbook?: Workbook): Promise<Map<string, SheetArtifacts>> {
  const sheets = await resolveWorkbookSheets(archive);
  const result = new Map<string, SheetArtifacts>();
  await Promise.all(
    sheets.map(async sheet => {
      const xml = await readArchiveText(archive, sheet.path);
      if (!xml) return;
      const relationships = await readRelationships(archive, sheet.path);
      const reference = parseSheetReferences(xml);
      const referencedComments = relationships.get(reference.commentsRelationshipId ?? '');
      const referencedDrawing = relationships.get(reference.drawingRelationshipId ?? '');
      const commentsRelationship =
        referencedComments && COMMENTS_RELATIONSHIP.test(referencedComments.type)
          ? referencedComments
          : relationships.find(value => COMMENTS_RELATIONSHIP.test(value.type));
      const drawingRelationship =
        referencedDrawing && DRAWING_RELATIONSHIP.test(referencedDrawing.type)
          ? referencedDrawing
          : relationships.find(value => DRAWING_RELATIONSHIP.test(value.type));
      const comments = commentsRelationship ? await parseComments(archive, commentsRelationship.target) : [];
      const drawing = drawingRelationship ? await parseDrawing(archive, drawingRelationship.target, workbook) : { charts: [], images: [] };
      result.set(sheet.name, { comments, ...drawing });
    }),
  );
  return result;
}

async function resolveWorkbookSheets(archive: JSZip): Promise<SheetArchiveReference[]> {
  const workbookXml = await readArchiveText(archive, 'xl/workbook.xml');
  if (!workbookXml) return [];
  const relationships = await readRelationships(archive, 'xl/workbook.xml');
  const sheets: Array<{ name: string; relationshipId: string }> = [];
  const parser = new SaxesParser<{ xmlns: true }>({ xmlns: true });
  parser.on('opentag', tag => {
    if (tag.local !== 'sheet') return;
    const name = xmlAttribute(tag, 'name');
    const relationshipId = xmlAttribute(tag, 'id');
    if (name && relationshipId) sheets.push({ name, relationshipId });
  });
  parser.write(workbookXml).close();
  return sheets.flatMap(sheet => {
    const relationship = relationships.get(sheet.relationshipId);
    return relationship ? [{ name: sheet.name, path: relationship.target }] : [];
  });
}

async function readRelationships(archive: JSZip, originPath: string): Promise<RelationshipCollection> {
  const path = relationshipPath(originPath);
  const xml = await readArchiveText(archive, path);
  const values: Relationship[] = [];
  if (xml) {
    const parser = new SaxesParser<{ xmlns: true }>({ xmlns: true });
    parser.on('opentag', tag => {
      if (tag.local !== 'Relationship') return;
      const id = xmlAttribute(tag, 'Id');
      const target = xmlAttribute(tag, 'Target');
      const type = xmlAttribute(tag, 'Type');
      if (id && target && type) values.push({ id, target: resolveArchivePath(originPath, target), type });
    });
    parser.write(xml).close();
  }
  const indexed = values as RelationshipCollection;
  const byId = new Map(values.map(value => [value.id, value]));
  indexed.get = id => byId.get(id);
  return indexed;
}

function parseSheetReferences(xml: string): { drawingRelationshipId?: string; commentsRelationshipId?: string } {
  const result: { drawingRelationshipId?: string; commentsRelationshipId?: string } = {};
  const parser = new SaxesParser<{ xmlns: true }>({ xmlns: true });
  parser.on('opentag', tag => {
    if (tag.local === 'drawing') result.drawingRelationshipId = xmlAttribute(tag, 'id');
    if (tag.local === 'legacyDrawing') result.commentsRelationshipId = xmlAttribute(tag, 'id');
  });
  parser.write(xml).close();
  return result;
}

async function parseComments(archive: JSZip, path: string): Promise<SpreadsheetSheetData['comments']> {
  const xml = await readArchiveText(archive, path);
  if (!xml) return [];
  const authors: string[] = [];
  const comments: SpreadsheetSheetData['comments'] = [];
  let stack: string[] = [];
  let activeAuthor = -1;
  let activeReference: { row: number; column: number } | undefined;
  let text = '';
  const parser = new SaxesParser<{ xmlns: true }>({ xmlns: true });
  parser.on('opentag', tag => {
    stack.push(tag.local);
    if (tag.local === 'comment') {
      const reference = xmlAttribute(tag, 'ref');
      const authorId = Number(xmlAttribute(tag, 'authorId'));
      if (reference) activeReference = decodeCellAddress(reference);
      activeAuthor = Number.isInteger(authorId) ? authorId : -1;
      text = '';
    }
  });
  parser.on('text', value => {
    if (stack.at(-1) === 'author') authors.push(value);
    if (activeReference && stack.at(-1) === 't') text += value;
  });
  parser.on('closetag', tag => {
    if (tag.local === 'comment' && activeReference) {
      comments.push({ ...activeReference, comment: { author: authors[activeAuthor], text } });
      activeReference = undefined;
      activeAuthor = -1;
    }
    stack.pop();
  });
  parser.write(xml).close();
  return comments;
}

async function parseDrawing(archive: JSZip, path: string, workbook?: Workbook): Promise<Pick<SheetArtifacts, 'charts' | 'images'>> {
  const xml = await readArchiveText(archive, path);
  if (!xml) return { charts: [], images: [] };
  const relationships = await readRelationships(archive, path);
  const anchors = parseDrawingAnchors(xml);
  const charts: SpreadsheetChart[] = [];
  const images: SpreadsheetImage[] = [];
  await Promise.all(
    anchors.map(async (anchor, index) => {
      if (anchor.chartRelationshipId) {
        const relationship = relationships.get(anchor.chartRelationshipId);
        if (relationship && CHART_RELATIONSHIP.test(relationship.type)) {
          const chart = await parseChart(archive, relationship.target, workbook);
          if (chart) charts.push({ ...chart, id: `${path}:chart:${index}`, anchor: anchor.anchor });
        }
      }
      if (anchor.imageRelationshipId) {
        const relationship = relationships.get(anchor.imageRelationshipId);
        if (relationship && IMAGE_RELATIONSHIP.test(relationship.type)) {
          const source = await archive.file(relationship.target)?.async('base64');
          if (source)
            images.push({
              id: `${path}:image:${index}`,
              anchor: anchor.anchor,
              alt: anchor.name ?? '',
              source: `data:${mediaType(relationship.target)};base64,${source}`,
            });
        }
      }
    }),
  );
  return { charts, images };
}

type DrawingAnchorReference = {
  anchor: SpreadsheetDrawingAnchor;
  chartRelationshipId?: string;
  imageRelationshipId?: string;
  name?: string;
};

function parseDrawingAnchors(xml: string): DrawingAnchorReference[] {
  const anchors: DrawingAnchorReference[] = [];
  const stack: string[] = [];
  let active: DrawingAnchorReference | undefined;
  let marker: 'from' | 'to' | undefined;
  let text = '';
  const parser = new SaxesParser<{ xmlns: true }>({ xmlns: true });
  parser.on('opentag', tag => {
    stack.push(tag.local);
    if (tag.local === 'oneCellAnchor' || tag.local === 'twoCellAnchor' || tag.local === 'absoluteAnchor') {
      active = { anchor: { fromColumn: 1, fromRow: 1, fromColumnOffset: 0, fromRowOffset: 0 } };
    }
    if (!active) return;
    if (tag.local === 'from' || tag.local === 'to') marker = tag.local;
    if (tag.local === 'ext' && stack.at(-2)?.endsWith('Anchor')) {
      active.anchor.width = emuToPixels(Number(xmlAttribute(tag, 'cx')));
      active.anchor.height = emuToPixels(Number(xmlAttribute(tag, 'cy')));
    }
    if (tag.local === 'chart') active.chartRelationshipId = xmlAttribute(tag, 'id');
    if (tag.local === 'blip') active.imageRelationshipId = xmlAttribute(tag, 'embed');
    if (tag.local === 'cNvPr') active.name = xmlAttribute(tag, 'descr') ?? xmlAttribute(tag, 'name');
    if (tag.local === 'col' || tag.local === 'row' || tag.local === 'colOff' || tag.local === 'rowOff') text = '';
  });
  parser.on('text', value => {
    if (active && marker && ['col', 'row', 'colOff', 'rowOff'].includes(stack.at(-1) ?? '')) text += value;
  });
  parser.on('closetag', tag => {
    if (active && marker && ['col', 'row', 'colOff', 'rowOff'].includes(tag.local)) {
      const value = Number(text);
      if (Number.isFinite(value)) setAnchorMarker(active.anchor, marker, tag.local, value);
    }
    if (tag.local === 'from' || tag.local === 'to') marker = undefined;
    if ((tag.local === 'oneCellAnchor' || tag.local === 'twoCellAnchor' || tag.local === 'absoluteAnchor') && active) {
      anchors.push(active);
      active = undefined;
    }
    stack.pop();
  });
  parser.write(xml).close();
  return anchors;
}

function setAnchorMarker(anchor: SpreadsheetDrawingAnchor, marker: 'from' | 'to', kind: string, value: number): void {
  if (marker === 'from') {
    if (kind === 'col') anchor.fromColumn = value + 1;
    if (kind === 'row') anchor.fromRow = value + 1;
    if (kind === 'colOff') anchor.fromColumnOffset = emuToPixels(value);
    if (kind === 'rowOff') anchor.fromRowOffset = emuToPixels(value);
    return;
  }
  if (kind === 'col') anchor.toColumn = value + 1;
  if (kind === 'row') anchor.toRow = value + 1;
  if (kind === 'colOff') anchor.toColumnOffset = emuToPixels(value);
  if (kind === 'rowOff') anchor.toRowOffset = emuToPixels(value);
}

type ParsedChartSeries = SpreadsheetChartSeries & {
  nameReference?: string;
  categoryReference?: string;
  valueReference?: string;
};

async function parseChart(archive: JSZip, path: string, workbook?: Workbook): Promise<Omit<SpreadsheetChart, 'id' | 'anchor'> | undefined> {
  const xml = await readArchiveText(archive, path);
  if (!xml) return undefined;
  const stack: string[] = [];
  const series: ParsedChartSeries[] = [];
  let activeSeries: ParsedChartSeries | undefined;
  let chartType: SpreadsheetChart['type'] = 'line';
  let title = '';
  let categoryAxisTitle = '';
  let valueAxisTitle = '';
  let legendPosition: SpreadsheetChart['legendPosition'] = 'right';
  let pointIndex = 0;
  let text = '';
  const parser = new SaxesParser<{ xmlns: true }>({ xmlns: true });
  parser.on('opentag', tag => {
    stack.push(tag.local);
    if (tag.local === 'lineChart') chartType = 'line';
    if (tag.local === 'scatterChart') chartType = 'scatter';
    if (tag.local === 'pieChart' || tag.local === 'doughnutChart') chartType = 'pie';
    if (tag.local === 'barChart') chartType = xmlAttribute(tag, 'barDir') === 'bar' ? 'bar' : 'column';
    if (tag.local === 'barDir') chartType = xmlAttribute(tag, 'val') === 'bar' ? 'bar' : 'column';
    if (tag.local === 'ser') {
      activeSeries = { categories: [], values: [] };
      series.push(activeSeries);
    }
    if (tag.local === 'pt') pointIndex = Number(xmlAttribute(tag, 'idx')) || 0;
    if (tag.local === 'legendPos') {
      const value = xmlAttribute(tag, 'val');
      legendPosition = value === 'b' ? 'bottom' : value === 't' ? 'top' : value === 'l' ? 'left' : value === 'r' ? 'right' : 'none';
    }
    if (tag.local === 'srgbClr' && activeSeries && stack.includes('spPr')) {
      const value = xmlAttribute(tag, 'val');
      if (value && /^[0-9a-f]{6}$/i.test(value)) activeSeries.color = `#${applyTint(value, 0)}`;
    }
    if (tag.local === 't' || tag.local === 'v' || tag.local === 'f') text = '';
  });
  parser.on('text', value => {
    if (stack.at(-1) === 't' || stack.at(-1) === 'v' || stack.at(-1) === 'f') text += value;
  });
  parser.on('closetag', tag => {
    if (!activeSeries && tag.local !== 't' && tag.local !== 'v') {
      stack.pop();
      return;
    }
    if (tag.local === 't' || tag.local === 'v') {
      const value = text.trim();
      if (value) {
        if (stack.includes('title')) {
          if (stack.includes('catAx')) categoryAxisTitle += value;
          else if (stack.includes('valAx')) valueAxisTitle += value;
          else title += value;
        } else if (activeSeries && stack.includes('tx')) {
          activeSeries.name = value;
        } else if (activeSeries && (stack.includes('cat') || stack.includes('xVal'))) {
          activeSeries.categories[pointIndex] = value;
        } else if (activeSeries && (stack.includes('val') || stack.includes('yVal'))) {
          const numeric = Number(value);
          if (Number.isFinite(numeric)) activeSeries.values[pointIndex] = numeric;
        }
      }
    }
    if (tag.local === 'f' && activeSeries) {
      const reference = text.trim();
      if (reference) {
        if (stack.includes('tx')) activeSeries.nameReference = reference;
        else if (stack.includes('cat') || stack.includes('xVal')) activeSeries.categoryReference = reference;
        else if (stack.includes('val') || stack.includes('yVal')) activeSeries.valueReference = reference;
      }
    }
    if (tag.local === 'ser') activeSeries = undefined;
    stack.pop();
  });
  parser.write(xml).close();
  if (workbook) {
    series.forEach(item => hydrateChartSeries(item, workbook));
  }
  return {
    type: chartType,
    title: title || undefined,
    categoryAxisTitle: categoryAxisTitle || undefined,
    valueAxisTitle: valueAxisTitle || undefined,
    legendPosition,
    series: series.map(({ nameReference: _nameReference, categoryReference: _categoryReference, valueReference: _valueReference, ...item }) => item),
  };
}

function hydrateChartSeries(series: ParsedChartSeries, workbook: Workbook): void {
  if (!series.name && series.nameReference) series.name = resolveChartRange(series.nameReference, workbook).map(String).join('');
  if (series.categories.length === 0 && series.categoryReference) series.categories = resolveChartRange(series.categoryReference, workbook).map(String);
  if (series.values.length === 0 && series.valueReference) {
    series.values = resolveChartRange(series.valueReference, workbook)
      .map(value => Number(value))
      .filter(value => Number.isFinite(value));
  }
}

function resolveChartRange(reference: string, workbook: Workbook): Array<string | number> {
  const match = /^(?:'((?:[^']|'')+)'|([^'!]+))!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$/i.exec(reference.trim());
  if (!match) return [];
  const sheetName = (match[1] ?? match[2]).replace(/''/g, "'");
  const worksheet = workbook.getWorksheet(sheetName);
  if (!worksheet) return [];
  const start = decodeCellAddress(`${match[3]}${match[4]}`);
  const end = decodeCellAddress(`${match[5] ?? match[3]}${match[6] ?? match[4]}`);
  const values: Array<string | number> = [];
  for (let row = start.row; row <= end.row; row += 1) {
    for (let column = start.column; column <= end.column; column += 1) {
      const cell = worksheet.getCell(row, column);
      const value = chartCellValue(cell.value);
      if (typeof value === 'number' || typeof value === 'string') values.push(value);
    }
  }
  return values;
}

function chartCellValue(value: Cell['value']): string | number | undefined {
  if (typeof value === 'number' || typeof value === 'string') return value;
  if (value instanceof Date) return value.toLocaleDateString();
  if (isFormulaValue(value)) return chartCellValue(value.result as Cell['value']);
  if (isHyperlinkValue(value)) return value.text;
  if (isRichTextValue(value)) return value.richText.map(part => part.text).join('');
  return undefined;
}

function parseAutoFilter(autoFilter: unknown): SpreadsheetSheetData['autoFilter'] {
  const reference =
    typeof autoFilter === 'string'
      ? autoFilter
      : autoFilter && typeof autoFilter === 'object' && 'ref' in autoFilter && typeof autoFilter.ref === 'string'
        ? autoFilter.ref
        : undefined;
  return reference ? parseCellRange(reference) : undefined;
}

async function readArchiveText(archive: JSZip, path: string): Promise<string | undefined> {
  const entry = archive.file(path);
  return entry ? entry.async('text') : undefined;
}

function relationshipPath(originPath: string): string {
  const parts = originPath.split('/');
  const file = parts.pop();
  return [...parts, '_rels', `${file}.rels`].join('/');
}

function resolveArchivePath(originPath: string, target: string): string {
  if (target.startsWith('/')) return target.slice(1);
  const parts = originPath.split('/').slice(0, -1);
  target.split('/').forEach(part => {
    if (!part || part === '.') return;
    if (part === '..') parts.pop();
    else parts.push(part);
  });
  return parts.join('/');
}

function emuToPixels(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.round((value / 9525) * 100) / 100) : 0;
}

function mediaType(path: string): string {
  const extension = path.split('.').pop()?.toLowerCase();
  if (extension === 'jpg' || extension === 'jpeg') return 'image/jpeg';
  if (extension === 'gif') return 'image/gif';
  if (extension === 'svg') return 'image/svg+xml';
  if (extension === 'bmp') return 'image/bmp';
  if (extension === 'tif' || extension === 'tiff') return 'image/tiff';
  return 'image/png';
}

function parseThemePalette(workbook: Workbook): string[] {
  const themes = workbook.model.themes as unknown as WorkbookThemes;
  const xml = themes?.theme1;
  if (!xml) return [...DEFAULT_THEME_COLORS];

  const palette = [...DEFAULT_THEME_COLORS];
  let activeThemeIndex = -1;
  const parser = new SaxesParser<{ xmlns: true }>({ xmlns: true });
  parser.on('opentag', tag => {
    const themeIndex = THEME_COLOR_NAMES.indexOf(tag.local);
    if (themeIndex >= 0) {
      activeThemeIndex = themeIndex;
      return;
    }
    if (activeThemeIndex < 0 || (tag.local !== 'srgbClr' && tag.local !== 'sysClr')) return;
    const color = tag.local === 'sysClr' ? xmlAttribute(tag, 'lastClr') : xmlAttribute(tag, 'val');
    if (color && /^[0-9a-f]{6}$/i.test(color)) palette[activeThemeIndex] = color.toUpperCase();
  });
  parser.on('closetag', tag => {
    if (THEME_COLOR_NAMES[activeThemeIndex] === tag.local) activeThemeIndex = -1;
  });
  parser.write(xml).close();
  return palette;
}

function xmlAttribute(tag: SaxesTagNS, name: string): string | undefined {
  const attribute = Object.values(tag.attributes).find(value => value.local === name);
  return attribute?.value;
}

function applyTint(hex: string, tint: number): string {
  const normalizedTint = Math.max(-1, Math.min(1, tint));
  if (normalizedTint === 0) return hex.toUpperCase();
  const [red, green, blue] = hexToRgb(hex).map(value => value / 255) as [number, number, number];
  const maximum = Math.max(red, green, blue);
  const minimum = Math.min(red, green, blue);
  let hue = 0;
  let saturation = 0;
  let lightness = (maximum + minimum) / 2;

  if (maximum !== minimum) {
    const delta = maximum - minimum;
    saturation = lightness > 0.5 ? delta / (2 - maximum - minimum) : delta / (maximum + minimum);
    if (maximum === red) hue = (green - blue) / delta + (green < blue ? 6 : 0);
    else if (maximum === green) hue = (blue - red) / delta + 2;
    else hue = (red - green) / delta + 4;
    hue /= 6;
  }
  lightness = normalizedTint < 0 ? lightness * (1 + normalizedTint) : lightness * (1 - normalizedTint) + normalizedTint;

  const channels =
    saturation === 0
      ? [lightness, lightness, lightness]
      : [hueToRgb(lightness, saturation, hue + 1 / 3), hueToRgb(lightness, saturation, hue), hueToRgb(lightness, saturation, hue - 1 / 3)];
  return channels
    .map(channel =>
      Math.round(channel * 255)
        .toString(16)
        .padStart(2, '0'),
    )
    .join('')
    .toUpperCase();
}

function hueToRgb(lightness: number, saturation: number, hue: number): number {
  let normalizedHue = hue;
  if (normalizedHue < 0) normalizedHue += 1;
  if (normalizedHue > 1) normalizedHue -= 1;
  const upper = lightness < 0.5 ? lightness * (1 + saturation) : lightness + saturation - lightness * saturation;
  const lower = 2 * lightness - upper;
  if (normalizedHue < 1 / 6) return lower + (upper - lower) * 6 * normalizedHue;
  if (normalizedHue < 1 / 2) return upper;
  if (normalizedHue < 2 / 3) return lower + (upper - lower) * (2 / 3 - normalizedHue) * 6;
  return lower;
}

function hexToRgb(hex: string): [number, number, number] {
  return [Number.parseInt(hex.slice(0, 2), 16), Number.parseInt(hex.slice(2, 4), 16), Number.parseInt(hex.slice(4, 6), 16)];
}

function decodeCellAddress(address: string): { row: number; column: number } {
  const match = /^\$?([A-Z]+)\$?(\d+)$/i.exec(address);
  if (!match) throw new Error(`Invalid spreadsheet cell address: ${address}`);
  let column = 0;
  for (const character of match[1].toUpperCase()) column = column * 26 + character.charCodeAt(0) - 64;
  return { row: Number(match[2]), column };
}

function cellKey(row: number, column: number): string {
  return `${row}:${column}`;
}

function columnWidthToPixels(width: number): number {
  if (width <= 0) return 0;
  return Math.max(1, Math.floor(((256 * width + Math.floor(128 / 7)) / 256) * 7));
}

function pointsToPixels(points: number): number {
  return Math.max(0, Math.round((points * 96 * 100) / 72) / 100);
}

function safeHyperlink(hyperlink: string): string | undefined {
  return /^(https?:|mailto:|tel:)/i.test(hyperlink) ? hyperlink : undefined;
}

function isFormulaValue(value: unknown): value is { formula: string; result?: unknown } | { sharedFormula: string; result?: unknown } {
  return Boolean(value && typeof value === 'object' && ('formula' in value || 'sharedFormula' in value));
}

function isHyperlinkValue(value: unknown): value is { text: string; hyperlink: string } {
  return Boolean(value && typeof value === 'object' && 'text' in value && 'hyperlink' in value);
}

function isRichTextValue(value: unknown): value is { richText: Array<{ text: string }> } {
  return Boolean(value && typeof value === 'object' && 'richText' in value);
}

function isCellError(value: unknown): value is { error: string } {
  return Boolean(value && typeof value === 'object' && 'error' in value);
}

function normalizeDegrees(degrees: number): number {
  return ((degrees % 360) + 360) % 360;
}

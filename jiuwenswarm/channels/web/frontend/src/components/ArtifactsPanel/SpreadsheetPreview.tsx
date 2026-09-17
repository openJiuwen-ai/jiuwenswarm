import { useEffect, useId, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type UIEvent as ReactUIEvent } from 'react';
import { AlertCircle, ChevronDown, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { categoryCenter, categoryPoint, clusteredCategoryBand, ensureNonZeroAxisSpan, linearPosition, spanFromBaseline } from './chartGeometry';
import {
  MAX_SPREADSHEET_PREVIEW_BYTES,
  axisIndexAtOffset,
  buildAxisMetrics,
  columnLabel,
  type SpreadsheetCellData,
  type SpreadsheetCellStyle,
  type SpreadsheetChart,
  type SpreadsheetDrawingAnchor,
  type SpreadsheetMergeRange,
  type SpreadsheetSheetData,
  type SpreadsheetWorkerResponse,
  type SpreadsheetWorkbookData,
} from './spreadsheetPreviewModel';

type PreviewStatus = 'loading' | 'ready' | 'error' | 'resource-limit' | 'too-large';
type Viewport = { width: number; height: number; scrollLeft: number; scrollTop: number };

const ROW_HEADER_WIDTH = 48;
const COLUMN_HEADER_HEIGHT = 28;
const AXIS_OVERSCAN = 2;

function PreviewMessage({ children, danger = false }: { children: string; danger?: boolean }) {
  return (
    <div
      className={clsx('flex h-full min-h-[240px] items-center justify-center gap-2 p-6 text-sm', danger ? 'text-danger' : 'text-text-muted')}
      role={danger ? 'alert' : undefined}
    >
      <AlertCircle size={16} />
      <span>{children}</span>
    </div>
  );
}

export function SpreadsheetPreview({ url, title, size }: { url: string; title: string; size?: number }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<PreviewStatus>(size !== undefined && size > MAX_SPREADSHEET_PREVIEW_BYTES ? 'too-large' : 'loading');
  const [workbook, setWorkbook] = useState<SpreadsheetWorkbookData | null>(null);

  useEffect(() => {
    if (size !== undefined && size > MAX_SPREADSHEET_PREVIEW_BYTES) {
      setStatus('too-large');
      setWorkbook(null);
      return;
    }

    const abortController = new AbortController();
    const worker = new Worker(new URL('./spreadsheetPreview.worker.ts', import.meta.url), { type: 'module' });
    let cancelled = false;
    setStatus('loading');
    setWorkbook(null);

    worker.onmessage = (event: MessageEvent<SpreadsheetWorkerResponse>) => {
      if (cancelled) return;
      if (event.data.type === 'ready') {
        setWorkbook(event.data.workbook);
        setStatus('ready');
      } else {
        console.error('Failed to parse XLSX preview', event.data.message);
        setStatus(event.data.code === 'resource-limit' ? 'resource-limit' : 'error');
      }
      worker.terminate();
    };
    worker.onerror = event => {
      if (cancelled) return;
      console.error('XLSX preview worker failed', event.message);
      setStatus('error');
      worker.terminate();
    };

    void fetch(url, { cache: 'no-store', signal: abortController.signal })
      .then(async response => {
        const contentType = (response.headers.get('content-type') ?? '').toLowerCase();
        const contentLength = Number(response.headers.get('content-length'));
        if (!response.ok || contentType.includes('text/html') || contentType.includes('application/json')) {
          throw new Error(`XLSX request failed with HTTP ${response.status}`);
        }
        if (Number.isFinite(contentLength) && contentLength > MAX_SPREADSHEET_PREVIEW_BYTES) throw new Error('spreadsheet_too_large');
        return readLimitedResponse(response);
      })
      .then(buffer => {
        if (!cancelled) worker.postMessage({ type: 'parse', buffer }, [buffer]);
      })
      .catch(error => {
        if (cancelled || abortController.signal.aborted) return;
        setStatus(error instanceof Error && error.message === 'spreadsheet_too_large' ? 'too-large' : 'error');
        worker.terminate();
      });

    return () => {
      cancelled = true;
      abortController.abort();
      worker.terminate();
    };
  }, [size, url]);

  if (status === 'loading') {
    return (
      <div
        className="flex h-full min-h-[240px] items-center justify-center gap-2 text-sm text-text-muted"
        aria-label={title}
        data-testid="artifact-spreadsheet-preview"
        data-variant="loading"
      >
        <LoaderCircle className="animate-spin" size={16} />
        {t('common.loading')}
      </div>
    );
  }
  if (status === 'too-large') {
    return (
      <div className="h-full" aria-label={title} data-testid="artifact-spreadsheet-preview" data-variant="too-large">
        <PreviewMessage danger>{t('artifacts.spreadsheetTooLarge', { size: '50 MiB' })}</PreviewMessage>
      </div>
    );
  }
  if (status === 'resource-limit') {
    return (
      <div className="h-full" aria-label={title} data-testid="artifact-spreadsheet-preview" data-variant="resource-limit">
        <PreviewMessage danger>{t('artifacts.spreadsheetResourceLimitExceeded')}</PreviewMessage>
      </div>
    );
  }
  if (status === 'error' || !workbook) {
    return (
      <div className="h-full" aria-label={title} data-testid="artifact-spreadsheet-preview" data-variant="error">
        <PreviewMessage danger>{t('artifacts.spreadsheetPreviewFailed')}</PreviewMessage>
      </div>
    );
  }
  return <WorkbookViewer workbook={workbook} title={title} />;
}

async function readLimitedResponse(response: Response): Promise<ArrayBuffer> {
  if (!response.body) throw new Error('spreadsheet_response_body_missing');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let byteLength = 0;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    byteLength += value.byteLength;
    if (byteLength > MAX_SPREADSHEET_PREVIEW_BYTES) {
      await reader.cancel();
      throw new Error('spreadsheet_too_large');
    }
    chunks.push(value);
  }

  const buffer = new Uint8Array(byteLength);
  let offset = 0;
  chunks.forEach(chunk => {
    buffer.set(chunk, offset);
    offset += chunk.byteLength;
  });
  return buffer.buffer;
}

function WorkbookViewer({ workbook, title }: { workbook: SpreadsheetWorkbookData; title: string }) {
  const { t } = useTranslation();
  const tabPanelId = useId();
  const visibleSheets = useMemo(() => workbook.sheets.filter(sheet => sheet.state === 'visible'), [workbook.sheets]);
  const requestedActiveSheet = workbook.sheets[workbook.activeSheetIndex];
  const initialSheet = visibleSheets.find(sheet => sheet.id === requestedActiveSheet?.id) ?? visibleSheets[0];
  const [activeSheetId, setActiveSheetId] = useState(initialSheet?.id ?? 0);
  const tabRefs = useRef(new Map<number, HTMLButtonElement>());
  const activeSheet = visibleSheets.find(sheet => sheet.id === activeSheetId) ?? visibleSheets[0];

  const activateSheet = (sheetId: number, focus = false) => {
    setActiveSheetId(sheetId);
    if (focus) requestAnimationFrame(() => tabRefs.current.get(sheetId)?.focus());
  };

  const handleTabKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let targetIndex: number | null = null;
    if (event.key === 'ArrowRight') targetIndex = (index + 1) % visibleSheets.length;
    else if (event.key === 'ArrowLeft') targetIndex = (index - 1 + visibleSheets.length) % visibleSheets.length;
    else if (event.key === 'Home') targetIndex = 0;
    else if (event.key === 'End') targetIndex = visibleSheets.length - 1;
    if (targetIndex === null) return;
    event.preventDefault();
    activateSheet(visibleSheets[targetIndex].id, true);
  };

  return (
    <div
      className="flex h-full min-h-0 w-full flex-col overflow-hidden border border-border bg-card"
      aria-label={title}
      data-testid="artifact-spreadsheet-preview"
      data-variant="ready"
    >
      <div
        className="flex h-10 shrink-0 items-end gap-1 overflow-x-auto border-b border-border bg-panel px-2 pt-1"
        role="tablist"
        aria-label={t('artifacts.spreadsheetSheetTabsLabel')}
        data-testid="artifact-spreadsheet-sheet-tabs"
      >
        {visibleSheets.map((sheet, index) => {
          const selected = sheet.id === activeSheet?.id;
          return (
            <button
              key={sheet.id}
              ref={element => {
                if (element) tabRefs.current.set(sheet.id, element);
                else tabRefs.current.delete(sheet.id);
              }}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={tabPanelId}
              tabIndex={selected ? 0 : -1}
              title={sheet.name}
              data-testid="artifact-spreadsheet-sheet-tab"
              data-variant={sheet.id}
              className={clsx(
                'relative h-8 max-w-48 shrink-0 truncate rounded-t-md px-3 text-xs transition-colors',
                selected ? 'bg-card font-medium text-text' : 'text-text-muted hover:bg-secondary hover:text-text',
              )}
              onClick={() => activateSheet(sheet.id)}
              onKeyDown={event => handleTabKeyDown(event, index)}
            >
              {sheet.name}
              {selected && <span className="absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-accent" />}
            </button>
          );
        })}
      </div>
      {activeSheet ? (
        <div id={tabPanelId} role="tabpanel" className="min-h-0 flex-1" data-testid="artifact-spreadsheet-sheet-panel">
          <WorksheetGrid key={activeSheet.id} sheet={activeSheet} styles={workbook.styles} />
        </div>
      ) : (
        <PreviewMessage>{t('artifacts.spreadsheetEmptyWorkbook')}</PreviewMessage>
      )}
    </div>
  );
}

function WorksheetGrid({ sheet, styles }: { sheet: SpreadsheetSheetData; styles: SpreadsheetCellStyle[] }) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const scrollFrameRef = useRef<number | null>(null);
  const [viewport, setViewport] = useState<Viewport>({ width: 0, height: 0, scrollLeft: 0, scrollTop: 0 });
  const rowMetrics = useMemo(() => buildAxisMetrics(sheet.rowCount, sheet.defaultRowHeight, sheet.rowSizes), [sheet]);
  const columnMetrics = useMemo(() => buildAxisMetrics(sheet.columnCount, sheet.defaultColumnWidth, sheet.columnSizes), [sheet]);
  const cellMap = useMemo(() => {
    const map = new Map<string, SpreadsheetCellData>();
    sheet.rows.forEach(row => row.cells.forEach(cell => map.set(cellKey(cell.row, cell.column), cell)));
    return map;
  }, [sheet.rows]);
  const rowStyleMap = useMemo(() => new Map(sheet.rowStyleIds.map(item => [item.index, item.styleId])), [sheet.rowStyleIds]);
  const columnStyleMap = useMemo(() => new Map(sheet.columnStyleIds.map(item => [item.index, item.styleId])), [sheet.columnStyleIds]);
  const commentMap = useMemo(() => new Map(sheet.comments.map(item => [cellKey(item.row, item.column), item.comment])), [sheet.comments]);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const updateSize = () => {
      setViewport(current => ({
        ...current,
        width: element.clientWidth,
        height: element.clientHeight,
        scrollLeft: element.scrollLeft,
        scrollTop: element.scrollTop,
      }));
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(
    () => () => {
      if (scrollFrameRef.current !== null) cancelAnimationFrame(scrollFrameRef.current);
    },
    [],
  );

  const handleScroll = (event: ReactUIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    if (scrollFrameRef.current !== null) return;
    scrollFrameRef.current = requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      setViewport(current => ({
        ...current,
        scrollLeft: element.scrollLeft,
        scrollTop: element.scrollTop,
      }));
    });
  };

  if (sheet.rowCount === 0 || sheet.columnCount === 0) return <PreviewMessage>{t('artifacts.spreadsheetEmptySheet')}</PreviewMessage>;

  const firstRow = Math.max(1, axisIndexAtOffset(rowMetrics.offsets, Math.max(0, viewport.scrollTop - COLUMN_HEADER_HEIGHT)) - AXIS_OVERSCAN);
  const lastRow = Math.min(
    sheet.rowCount,
    axisIndexAtOffset(rowMetrics.offsets, Math.max(0, viewport.scrollTop - COLUMN_HEADER_HEIGHT + viewport.height)) + AXIS_OVERSCAN,
  );
  const firstColumn = Math.max(1, axisIndexAtOffset(columnMetrics.offsets, Math.max(0, viewport.scrollLeft - ROW_HEADER_WIDTH)) - AXIS_OVERSCAN);
  const lastColumn = Math.min(
    sheet.columnCount,
    axisIndexAtOffset(columnMetrics.offsets, Math.max(0, viewport.scrollLeft - ROW_HEADER_WIDTH + viewport.width)) + AXIS_OVERSCAN,
  );

  const visibleMerges = sheet.merges.filter(
    merge => merge.bottom >= firstRow && merge.top <= lastRow && merge.right >= firstColumn && merge.left <= lastColumn,
  );
  const mergedCells = new Set<string>();
  visibleMerges.forEach(merge => {
    for (let row = Math.max(merge.top, firstRow); row <= Math.min(merge.bottom, lastRow); row += 1) {
      for (let column = Math.max(merge.left, firstColumn); column <= Math.min(merge.right, lastColumn); column += 1) {
        mergedCells.add(cellKey(row, column));
      }
    }
  });

  const cells: React.ReactNode[] = visibleMerges.map(merge =>
    renderCell(sheet, styles, cellMap, commentMap, rowStyleMap, columnStyleMap, rowMetrics.offsets, columnMetrics.offsets, merge.top, merge.left, merge),
  );
  for (let row = firstRow; row <= lastRow; row += 1) {
    if (rowMetrics.offsets[row] === rowMetrics.offsets[row - 1]) continue;
    for (let column = firstColumn; column <= lastColumn; column += 1) {
      if (columnMetrics.offsets[column] === columnMetrics.offsets[column - 1] || mergedCells.has(cellKey(row, column))) continue;
      cells.push(renderCell(sheet, styles, cellMap, commentMap, rowStyleMap, columnStyleMap, rowMetrics.offsets, columnMetrics.offsets, row, column));
    }
  }

  const columnHeaders: React.ReactNode[] = [];
  for (let column = firstColumn; column <= lastColumn; column += 1) {
    const width = columnMetrics.offsets[column] - columnMetrics.offsets[column - 1];
    if (width <= 0) continue;
    columnHeaders.push(
      <div
        key={column}
        role="columnheader"
        aria-colindex={column}
        className="absolute flex h-full items-center justify-center overflow-hidden border-r border-border bg-panel text-[11px] font-medium text-text-muted"
        style={{ left: ROW_HEADER_WIDTH + columnMetrics.offsets[column - 1] - viewport.scrollLeft, width }}
      >
        {columnLabel(column)}
      </div>,
    );
  }

  const rowHeaders: React.ReactNode[] = [];
  for (let row = firstRow; row <= lastRow; row += 1) {
    const height = rowMetrics.offsets[row] - rowMetrics.offsets[row - 1];
    if (height <= 0) continue;
    rowHeaders.push(
      <div
        key={row}
        role="rowheader"
        aria-rowindex={row}
        className="absolute flex w-full items-center justify-center overflow-hidden border-b border-border bg-panel text-[11px] tabular-nums text-text-muted"
        style={{ top: COLUMN_HEADER_HEIGHT + rowMetrics.offsets[row - 1] - viewport.scrollTop, height }}
      >
        {row}
      </div>,
    );
  }

  return (
    <div className="relative isolate h-full min-h-0 w-full overflow-hidden bg-card" data-testid="artifact-spreadsheet-grid">
      <div
        ref={scrollRef}
        className="relative z-0 h-full w-full overflow-auto"
        role="grid"
        aria-label={t('artifacts.spreadsheetTableLabel', { sheet: sheet.name })}
        aria-rowcount={sheet.rowCount}
        aria-colcount={sheet.columnCount}
        data-testid="artifact-spreadsheet-grid-scroll"
        onScroll={handleScroll}
      >
        <div
          className="relative"
          style={{
            width: ROW_HEADER_WIDTH + columnMetrics.totalSize,
            height: COLUMN_HEADER_HEIGHT + rowMetrics.totalSize,
          }}
        >
          {cells}
          {sheet.images.map(image => (
            <img
              key={image.id}
              src={image.source}
              alt={image.alt}
              className="absolute z-10 object-contain"
              style={drawingStyle(image.anchor, rowMetrics.offsets, columnMetrics.offsets)}
            />
          ))}
          {sheet.charts.map(chart => (
            <WorkbookChart key={chart.id} chart={chart} style={drawingStyle(chart.anchor, rowMetrics.offsets, columnMetrics.offsets)} />
          ))}
        </div>
      </div>
      <div className="pointer-events-none absolute inset-x-0 top-0 z-30 h-7 overflow-hidden border-b border-border bg-panel">{columnHeaders}</div>
      <div className="pointer-events-none absolute inset-y-0 left-0 z-30 w-12 overflow-hidden border-r border-border bg-panel">{rowHeaders}</div>
      <div className="pointer-events-none absolute left-0 top-0 z-40 h-7 w-12 border-b border-r border-border bg-panel" />
    </div>
  );
}

function renderCell(
  sheet: SpreadsheetSheetData,
  styles: SpreadsheetCellStyle[],
  cellMap: Map<string, SpreadsheetCellData>,
  commentMap: Map<string, { author?: string; text: string }>,
  rowStyleMap: Map<number, number>,
  columnStyleMap: Map<number, number>,
  rowOffsets: Float64Array,
  columnOffsets: Float64Array,
  row: number,
  column: number,
  merge?: SpreadsheetMergeRange,
): React.ReactNode {
  const cell = cellMap.get(cellKey(row, column));
  const comment = commentMap.get(cellKey(row, column));
  const bottom = merge?.bottom ?? row;
  const right = merge?.right ?? column;
  const width = columnOffsets[right] - columnOffsets[column - 1];
  const height = rowOffsets[bottom] - rowOffsets[row - 1];
  if (width <= 0 || height <= 0) return null;

  const inheritedStyle = {
    ...(styles[columnStyleMap.get(column) ?? 0] ?? {}),
    ...(styles[rowStyleMap.get(row) ?? 0] ?? {}),
  };
  const documentStyle = cell ? (styles[cell.styleId] ?? {}) : inheritedStyle;
  const style: CSSProperties = {
    left: ROW_HEADER_WIDTH + columnOffsets[column - 1],
    top: COLUMN_HEADER_HEIGHT + rowOffsets[row - 1],
    width,
    height,
    justifyContent: cell?.alignRight ? 'flex-end' : 'flex-start',
    alignItems: 'flex-end',
    ...documentStyle,
  };
  const content = cell?.hyperlink ? (
    <a
      href={cell.hyperlink}
      target="_blank"
      rel="noreferrer"
      className={clsx('min-w-0 text-text-link underline', documentStyle.whiteSpace === 'normal' ? 'break-words' : 'truncate')}
    >
      {cell.text}
    </a>
  ) : (
    <span className={clsx('min-w-0', documentStyle.whiteSpace === 'normal' ? 'break-words' : 'truncate')}>{cell?.text ?? ''}</span>
  );

  return (
    <div
      key={`${row}:${column}:${bottom}:${right}`}
      role="gridcell"
      aria-rowindex={row}
      aria-colindex={column}
      aria-rowspan={merge ? merge.bottom - merge.top + 1 : undefined}
      aria-colspan={merge ? merge.right - merge.left + 1 : undefined}
      title={
        [cell?.formula ? `${cell.text}\n=${cell.formula}` : undefined, comment ? `${comment.author ? `${comment.author}: ` : ''}${comment.text}` : undefined]
          .filter((value): value is string => Boolean(value))
          .join('\n') ||
        cell?.text ||
        undefined
      }
      className={clsx(
        'absolute flex min-w-0 overflow-hidden px-2 text-xs text-text',
        sheet.showGridLines ? 'border-b border-r border-border' : 'border-b border-r border-transparent',
      )}
      style={style}
    >
      {content}
      {comment && (
        <span aria-label={comment.text} className="absolute right-0 top-0 h-0 w-0 border-b-[7px] border-l-[7px] border-b-transparent border-l-red-500" />
      )}
      {sheet.autoFilter && row === sheet.autoFilter.top && column >= sheet.autoFilter.left && column <= sheet.autoFilter.right && (
        <ChevronDown aria-hidden="true" size={12} className="ml-1 shrink-0 opacity-70" />
      )}
    </div>
  );
}

function cellKey(row: number, column: number): string {
  return `${row}:${column}`;
}

function drawingStyle(anchor: SpreadsheetDrawingAnchor, rowOffsets: Float64Array, columnOffsets: Float64Array): CSSProperties {
  const left = ROW_HEADER_WIDTH + (columnOffsets[anchor.fromColumn - 1] ?? 0) + anchor.fromColumnOffset;
  const top = COLUMN_HEADER_HEIGHT + (rowOffsets[anchor.fromRow - 1] ?? 0) + anchor.fromRowOffset;
  const width =
    anchor.width ??
    Math.max(
      0,
      (columnOffsets[anchor.toColumn ?? anchor.fromColumn] ?? 0) -
        (columnOffsets[anchor.fromColumn - 1] ?? 0) +
        (anchor.toColumnOffset ?? 0) -
        anchor.fromColumnOffset,
    );
  const height =
    anchor.height ??
    Math.max(0, (rowOffsets[anchor.toRow ?? anchor.fromRow] ?? 0) - (rowOffsets[anchor.fromRow - 1] ?? 0) + (anchor.toRowOffset ?? 0) - anchor.fromRowOffset);
  return { left, top, width, height };
}

function WorkbookChart({ chart, style }: { chart: SpreadsheetChart; style: CSSProperties }) {
  const width = Math.max(240, Number(style.width) || 240);
  const height = Math.max(160, Number(style.height) || 160);
  const series = chart.series.filter(item => item.values.length > 0);
  const legendSeries = series.map((item, seriesIndex) => ({
    item,
    seriesIndex,
    label: item.name || `Series ${seriesIndex + 1}`,
  }));
  const values = series.flatMap(item => item.values);
  if (values.length === 0) return null;

  const viewWidth = 800;
  const viewHeight = 400;
  const chartLabel = chart.title || 'Chart';
  const legendWidth = chart.legendPosition === 'right' && legendSeries.length > 0 ? 150 : 0;
  const bottomLegendReserve = chart.type === 'bar' && chart.valueAxisTitle && chart.legendPosition === 'bottom' && legendSeries.length > 0 ? 34 : 0;
  const plot = {
    left: chart.type === 'bar' ? 145 : 76,
    top: 72,
    right: viewWidth - 42 - legendWidth,
    bottom: viewHeight - 62 - bottomLegendReserve,
  };
  const minValue = Math.min(0, ...values);
  const maxValue = Math.max(0, ...values);
  const span = Math.max(1, maxValue - minValue);
  const tickStep = niceTickStep(span / 5);
  const { minimum: axisMin, maximum: axisMax } = ensureNonZeroAxisSpan(
    Math.floor(minValue / tickStep) * tickStep,
    Math.ceil(maxValue / tickStep) * tickStep,
    tickStep,
  );
  const categoryCount = Math.max(1, ...series.map(item => Math.max(item.categories.length, item.values.length)));
  const categories = series.find(item => item.categories.length > 0)?.categories ?? Array.from({ length: categoryCount }, (_, index) => String(index + 1));
  const xAtCategory = (index: number) =>
    chart.type === 'line' || chart.type === 'scatter'
      ? categoryPoint(index, categoryCount, plot.left, plot.right)
      : categoryCenter(index, categoryCount, plot.left, plot.right);
  const yAtCategory = (index: number) => categoryCenter(index, categoryCount, plot.top, plot.bottom);
  const xAtValue = (value: number) => linearPosition(value, axisMin, axisMax, plot.left, plot.right);
  const yAtValue = (value: number) => linearPosition(value, axisMin, axisMax, plot.bottom, plot.top);
  const zeroX = xAtValue(0);
  const zeroY = yAtValue(0);
  const colors = ['#2F80C3', '#D44343', '#72A842', '#8064A2', '#F79646', '#4BACC6'];
  const ticks: number[] = [];
  for (let value = axisMin; value <= axisMax + tickStep / 100; value += tickStep) ticks.push(Number(value.toFixed(10)));

  return (
    <figure
      className="absolute z-20 m-0 overflow-hidden border border-[#a0a0a0] bg-white text-black shadow-sm"
      style={{ ...style, minWidth: width, minHeight: height }}
    >
      <svg className="block h-full w-full" viewBox={`0 0 ${viewWidth} ${viewHeight}`} preserveAspectRatio="none" role="img" aria-label={chartLabel}>
        <rect width={viewWidth} height={viewHeight} fill="#FFFFFF" />
        {chart.title && (
          <text x={viewWidth / 2} y={42} textAnchor="middle" fontSize="27" fontWeight="700" fill="#111111">
            {chart.title}
          </text>
        )}
        {chart.type !== 'pie' &&
          ticks.map(value => {
            if (chart.type === 'bar') {
              const x = xAtValue(value);
              return (
                <g key={value}>
                  <line x1={x} x2={x} y1={plot.top} y2={plot.bottom} stroke="#8a8a8a" strokeWidth="1" />
                  <text x={x} y={plot.bottom + 22} textAnchor="middle" fontSize="13" fill="#111111">
                    {value}
                  </text>
                </g>
              );
            }
            const y = yAtValue(value);
            return (
              <g key={value}>
                <line x1={plot.left} x2={plot.right} y1={y} y2={y} stroke="#8a8a8a" strokeWidth="1" />
                <text x={plot.left - 10} y={y + 5} textAnchor="end" fontSize="13" fill="#111111">
                  {value}
                </text>
              </g>
            );
          })}
        {chart.type !== 'pie' &&
          (chart.type === 'bar' ? (
            <>
              <line x1={zeroX} x2={zeroX} y1={plot.top} y2={plot.bottom} stroke="#666666" />
              <line x1={plot.left} x2={plot.right} y1={plot.bottom} y2={plot.bottom} stroke="#666666" />
              {categories.map((category, index) => (
                <text key={`${category}:${index}`} x={plot.left - 10} y={yAtCategory(index) + 5} textAnchor="end" fontSize="12" fill="#111111">
                  {category}
                </text>
              ))}
            </>
          ) : (
            <>
              <line x1={plot.left} x2={plot.left} y1={plot.top} y2={plot.bottom} stroke="#666666" />
              <line x1={plot.left} x2={plot.right} y1={zeroY} y2={zeroY} stroke="#666666" />
              {categories.map((category, index) => (
                <text key={`${category}:${index}`} x={xAtCategory(index)} y={plot.bottom + 25} textAnchor="middle" fontSize="12" fill="#111111">
                  {category}
                </text>
              ))}
            </>
          ))}
        {chart.valueAxisTitle &&
          (chart.type === 'bar' ? (
            <text x={(plot.left + plot.right) / 2} y={viewHeight - 8 - bottomLegendReserve} textAnchor="middle" fontSize="14" fontWeight="700" fill="#111111">
              {chart.valueAxisTitle}
            </text>
          ) : (
            <text transform={`translate(22 ${(plot.top + plot.bottom) / 2}) rotate(-90)`} textAnchor="middle" fontSize="14" fontWeight="700" fill="#111111">
              {chart.valueAxisTitle}
            </text>
          ))}
        <ChartMarks
          chart={chart}
          series={series}
          colors={colors}
          plot={plot}
          categoryCount={categoryCount}
          xAtCategory={xAtCategory}
          xAtValue={xAtValue}
          yAtValue={yAtValue}
        />
        {chart.legendPosition !== 'none' &&
          legendSeries.map(({ item, seriesIndex, label }, legendIndex) => {
            const x = chart.legendPosition === 'right' ? plot.right + 28 : plot.left + legendIndex * 115;
            const y = chart.legendPosition === 'bottom' ? viewHeight - 15 : chart.legendPosition === 'top' ? 57 : plot.top + 24 + legendIndex * 25;
            return (
              <g key={`${seriesIndex}:${label}:legend`}>
                <line x1={x} x2={x + 27} y1={y} y2={y} stroke={item.color ?? colors[seriesIndex % colors.length]} strokeWidth="6" strokeLinecap="round" />
                <text x={x + 34} y={y + 5} fontSize="13" fill="#111111">
                  {label}
                </text>
              </g>
            );
          })}
      </svg>
    </figure>
  );
}

function ChartMarks({
  chart,
  series,
  colors,
  plot,
  categoryCount,
  xAtCategory,
  xAtValue,
  yAtValue,
}: {
  chart: SpreadsheetChart;
  series: SpreadsheetChart['series'];
  colors: string[];
  plot: { left: number; top: number; right: number; bottom: number };
  categoryCount: number;
  xAtCategory: (index: number) => number;
  xAtValue: (value: number) => number;
  yAtValue: (value: number) => number;
}) {
  if (chart.type === 'pie') {
    const item = series[0];
    const total = item.values.reduce((sum, value) => sum + Math.max(0, value), 0);
    let angle = -Math.PI / 2;
    const centerX = (plot.left + plot.right) / 2;
    const centerY = (plot.top + plot.bottom) / 2;
    const radius = Math.min(plot.right - plot.left, plot.bottom - plot.top) * 0.36;
    return (
      <>
        {item.values.map((value, index) => {
          const next = angle + (Math.max(0, value) / Math.max(total, 1)) * Math.PI * 2;
          const path = sectorPath(centerX, centerY, radius, angle, next);
          angle = next;
          return <path key={index} d={path} fill={colors[index % colors.length]} stroke="#FFFFFF" />;
        })}
      </>
    );
  }
  if (chart.type === 'column' || chart.type === 'bar') {
    return (
      <>
        {series.flatMap((item, seriesIndex) =>
          item.values.map((value, index) => {
            const color = item.color ?? colors[seriesIndex % colors.length];
            if (chart.type === 'bar') {
              const band = clusteredCategoryBand(index, categoryCount, seriesIndex, series.length, plot.top, plot.bottom);
              const valueSpan = spanFromBaseline(xAtValue(value), xAtValue(0));
              return <rect key={`${seriesIndex}:${index}`} x={valueSpan.start} y={band.start} width={valueSpan.size} height={band.size} fill={color} />;
            }
            const band = clusteredCategoryBand(index, categoryCount, seriesIndex, series.length, plot.left, plot.right);
            const valueSpan = spanFromBaseline(yAtValue(value), yAtValue(0));
            return <rect key={`${seriesIndex}:${index}`} x={band.start} y={valueSpan.start} width={band.size} height={valueSpan.size} fill={color} />;
          }),
        )}
      </>
    );
  }
  return (
    <>
      {series.map((item, seriesIndex) => {
        const color = item.color ?? colors[seriesIndex % colors.length];
        const points = item.values.map((value, index) => `${xAtCategory(index)},${yAtValue(value)}`).join(' ');
        return (
          <g key={`${item.name ?? seriesIndex}:marks`}>
            <polyline points={points} fill="none" stroke={color} strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
            {chart.type === 'scatter' &&
              item.values.map((value, index) => <circle key={index} cx={xAtCategory(index)} cy={yAtValue(value)} r="4" fill={color} />)}
          </g>
        );
      })}
    </>
  );
}

function niceTickStep(value: number): number {
  const power = 10 ** Math.floor(Math.log10(Math.max(value, 1)));
  const normalized = value / power;
  return (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * power;
}

function sectorPath(centerX: number, centerY: number, radius: number, start: number, end: number): string {
  const startX = centerX + radius * Math.cos(start);
  const startY = centerY + radius * Math.sin(start);
  const endX = centerX + radius * Math.cos(end);
  const endY = centerY + radius * Math.sin(end);
  return `M ${centerX} ${centerY} L ${startX} ${startY} A ${radius} ${radius} 0 ${end - start > Math.PI ? 1 : 0} 1 ${endX} ${endY} Z`;
}

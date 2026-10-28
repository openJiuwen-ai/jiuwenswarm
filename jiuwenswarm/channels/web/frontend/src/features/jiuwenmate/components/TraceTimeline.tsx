import { ChevronRight, X } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { JiuwenMateTraceRole, JiuwenMateTraceRow } from '../types';
import {
  traceTurnViews,
  type JiuwenMateTraceFilter,
  type JiuwenMateTraceMetric,
} from '../workspaceState';

const ROLE_LABELS: Record<JiuwenMateTraceRole, string> = {
  system: 'System',
  context: 'Context',
  user: 'User',
  assistant: 'Assistant',
  tool: 'Tool',
};

const DURATION_LANES = [
  { key: 'input', label: 'Input' },
  { key: 'model', label: 'Model' },
  { key: 'tools', label: 'Tools' },
] as const;

const TOKEN_LANES = [
  { key: 'input', label: 'Input' },
  { key: 'output', label: 'Output' },
] as const;

type DetailTab = 'summary' | 'preview' | 'raw' | 'otel';
const DETAIL_TABS: readonly { value: DetailTab; label: string }[] = [
  { value: 'summary', label: 'Summary' },
  { value: 'preview', label: 'Preview' },
  { value: 'raw', label: 'Raw' },
  { value: 'otel', label: 'OTel' },
];

function toolCallLabel(t: (key: string, options: { count: number }) => string, count: number): string {
  return t(count === 1 ? 'jiuwenmate.trace.toolCall' : 'jiuwenmate.trace.toolCalls', { count });
}

interface GanttBar {
  id: string;
  rowId: string;
  lane: string;
  role: JiuwenMateTraceRole | 'input' | 'output';
  left: number;
  width: number;
  label: string;
  detail: string;
}

export function traceClock(startedAt: string, offsetMs: number): { date: string; time: string } {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})/.exec(startedAt);
  if (!match) return { date: '', time: '' };
  const [, date, hours, minutes, seconds, millis] = match;
  const total = ((Number(hours) * 60 + Number(minutes)) * 60 + Number(seconds)) * 1000 + Number(millis) + offsetMs;
  const pad = (value: number, size = 2) => String(value).padStart(size, '0');
  const ms = total % 1000;
  const totalSeconds = Math.floor(total / 1000);
  const time = `${pad(Math.floor(totalSeconds / 3600) % 24)}:${pad(Math.floor(totalSeconds / 60) % 60)}:${pad(totalSeconds % 60)}.${pad(ms, 3)}`;
  return { date, time };
}

function ganttBars(
  rows: readonly JiuwenMateTraceRow[],
  metric: JiuwenMateTraceMetric,
  startedAt: string,
  tokenUnit: string,
): GanttBar[] {
  if (metric === 'duration') {
    const total = Math.max(...rows.map((row) => row.startMs + row.durationMs), 1);
    return rows.map((row) => ({
      id: row.id,
      rowId: row.id,
      lane: row.lane,
      role: row.role,
      left: (row.startMs / total) * 100,
      width: (row.durationMs / total) * 100,
      label: ROLE_LABELS[row.role],
      detail: traceClock(startedAt, row.startMs).time,
    }));
  }
  const total = Math.max(rows.reduce((sum, row) => sum + row.inputTokens + row.outputTokens, 0), 1);
  const bars: GanttBar[] = [];
  let cursor = 0;
  for (const row of rows) {
    if (row.inputTokens > 0) {
      bars.push({
        id: `${row.id}-input`,
        rowId: row.id,
        lane: 'input',
        role: 'input',
        left: (cursor / total) * 100,
        width: (row.inputTokens / total) * 100,
        label: 'Input',
        detail: `${row.inputTokens.toFixed(1)} ${tokenUnit}`,
      });
      cursor += row.inputTokens;
    }
    if (row.outputTokens > 0) {
      bars.push({
        id: `${row.id}-output`,
        rowId: row.id,
        lane: 'output',
        role: 'output',
        left: (cursor / total) * 100,
        width: (row.outputTokens / total) * 100,
        label: 'Output',
        detail: `${row.outputTokens.toFixed(1)} ${tokenUnit}`,
      });
      cursor += row.outputTokens;
    }
  }
  return bars;
}

interface TraceTimelineProps {
  rows: readonly JiuwenMateTraceRow[];
  startedAt: string;
  metric: JiuwenMateTraceMetric;
  filter: JiuwenMateTraceFilter;
  expandedTurns: readonly number[];
  selectedRowId: string | null;
  onSelectRow: (rowId: string | null) => void;
  onToggleTurn: (turn: number) => void;
}

export function TraceTimeline({
  rows,
  startedAt,
  metric,
  filter,
  expandedTurns,
  selectedRowId,
  onSelectRow,
  onToggleTurn,
}: TraceTimelineProps) {
  const { t } = useTranslation();
  const [hoveredBarId, setHoveredBarId] = useState<string | null>(null);
  const bars = ganttBars(rows, metric, startedAt, 'tok');
  const lanes = metric === 'duration' ? DURATION_LANES : TOKEN_LANES;
  const hoveredRowId = bars.find((bar) => bar.id === hoveredBarId)?.rowId ?? null;
  const systemRows = rows.filter((row) => row.turn === 0);
  const turns = traceTurnViews(rows, filter, expandedTurns);
  const selectedRow = rows.find((row) => row.id === selectedRowId) ?? null;

  const renderRow = (row: JiuwenMateTraceRow) => (
    <button
      key={row.id}
      type="button"
      className="jiuwenmate-trace-row"
      aria-pressed={selectedRowId === row.id}
      data-hovered={hoveredRowId === row.id || undefined}
      onClick={() => onSelectRow(row.id)}
      data-testid="jiuwenmate-trace-row"
      data-variant={row.id}
    >
      <em className="jiuwenmate-trace-role" data-role={row.role}>
        {ROLE_LABELS[row.role]}
      </em>
      <span>{row.content}</span>
    </button>
  );

  return (
    <>
      <div className="jiuwenmate-gantt" data-testid="jiuwenmate-trace-gantt" data-metric={metric}>
        {lanes.map((lane) => (
          <div key={lane.key} className="jiuwenmate-gantt__lane">
            <span className="jiuwenmate-gantt__label">{lane.label}</span>
            <span className="jiuwenmate-gantt__track">
              {bars
                .filter((bar) => bar.lane === lane.key)
                .map((bar) => (
                  <button
                    key={bar.id}
                    type="button"
                    className="jiuwenmate-gantt__bar"
                    data-role={bar.role}
                    aria-label={`${bar.label} ${bar.detail}`}
                    aria-pressed={selectedRowId === bar.rowId}
                    style={{ left: `${bar.left}%`, width: `${Math.max(bar.width, 0.6)}%` }}
                    onMouseEnter={() => setHoveredBarId(bar.id)}
                    onMouseLeave={() => setHoveredBarId(null)}
                    onClick={() => onSelectRow(bar.rowId)}
                  >
                    {hoveredBarId === bar.id ? (
                      <span className="jiuwenmate-gantt__tooltip" role="tooltip">
                        <strong>{bar.label}</strong>
                        <small>
                          {metric === 'duration' ? t('jiuwenmate.trace.startedAt') : t('jiuwenmate.trace.consumed')} {bar.detail}
                        </small>
                      </span>
                    ) : null}
                  </button>
                ))}
            </span>
          </div>
        ))}
      </div>
      <div className={`jiuwenmate-trace-body${selectedRow ? ' jiuwenmate-trace-body--detail' : ''}`}>
        <div className="jiuwenmate-trace-list" data-testid="jiuwenmate-trace-timeline">
          {systemRows.length > 0 ? (
            <div className="jiuwenmate-trace-turn jiuwenmate-trace-turn--system">
              <span className="jiuwenmate-trace-turn__label" />
              <div>{systemRows.map(renderRow)}</div>
            </div>
          ) : null}
          {turns.map((turn) => {
            const active = selectedRow?.turn === turn.turn;
            return (
              <div key={turn.turn} className="jiuwenmate-trace-turn" data-testid="jiuwenmate-trace-turn" data-variant={turn.turn}>
                <span className="jiuwenmate-trace-turn__label" data-active={active || undefined}>
                  Turn {turn.turn}
                </span>
                <div>
                  {turn.items.map((item) =>
                    item.kind === 'row' ? (
                      renderRow(item.row)
                    ) : (
                      <button
                        key={item.id}
                        type="button"
                        className="jiuwenmate-trace-row jiuwenmate-trace-row--collapsed"
                        onClick={() => onToggleTurn(turn.turn)}
                        data-testid="jiuwenmate-trace-collapsed"
                      >
                        <span>
                          {item.onlyTools
                            ? `… ${toolCallLabel(t, item.tools)} · ${item.toolNames.join(', ')}`
                            : `… ${t(item.steps === 1 ? 'jiuwenmate.trace.step' : 'jiuwenmate.trace.steps', { count: item.steps })} · ${toolCallLabel(t, item.tools)}`}
                        </span>
                      </button>
                    ),
                  )}
                </div>
              </div>
            );
          })}
        </div>
        {selectedRow ? (
          <TraceDetail rows={rows} row={selectedRow} startedAt={startedAt} onClose={() => onSelectRow(selectedRow.id)} />
        ) : null}
      </div>
    </>
  );
}

function TraceDetail({
  rows,
  row,
  startedAt,
  onClose,
}: {
  rows: readonly JiuwenMateTraceRow[];
  row: JiuwenMateTraceRow;
  startedAt: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<DetailTab>('summary');
  const turnRows = rows.filter((item) => item.turn === row.turn);
  const step = Math.max(1, turnRows.filter((item) => item.role === 'assistant' && item.startMs <= row.startMs).length);
  const requestIndex = Math.max(1, rows.filter((item) => item.role === 'user' && item.startMs <= row.startMs).length);
  const tokens = row.inputTokens + row.outputTokens;
  const started = traceClock(startedAt, row.startMs);
  const seconds = row.durationMs / 1000;
  const raw = { id: row.id, trace_id: row.traceId, role: row.role, content: row.content };
  const otel = {
    span_id: row.id,
    trace_id: row.traceId,
    name: `agent.main.${row.role}`,
    start_offset_ms: row.startMs,
    duration_ms: row.durationMs,
    attributes: { 'gen_ai.usage.input_tokens': row.inputTokens, 'gen_ai.usage.output_tokens': row.outputTokens },
  };

  return (
    <aside className="jiuwenmate-trace-detail" data-testid="jiuwenmate-trace-detail">
      <header>
        <em className="jiuwenmate-trace-role" data-role={row.role}>
          {ROLE_LABELS[row.role]}
        </em>
        <span className="jiuwenmate-trace-detail__step">
          {row.turn > 0 ? `Turn ${row.turn} · Step ${step}` : 'System'}
        </span>
        <button type="button" className="jiuwenmate-icon-button" aria-label={t('common.close')} onClick={onClose}>
          <X aria-hidden />
        </button>
      </header>
      <nav className="jiuwenmate-trace-detail__tabs" role="tablist">
        {DETAIL_TABS.map((item) => (
          <button key={item.value} type="button" role="tab" aria-selected={tab === item.value} onClick={() => setTab(item.value)}>
            {item.label}
          </button>
        ))}
      </nav>
      {tab === 'summary' ? (
        <div className="jiuwenmate-trace-detail__body">
          <dl>
            <dt>Source</dt>
            <dd>
              Request #{requestIndex}
              <ChevronRight aria-hidden />
            </dd>
            <dt>Status</dt>
            <dd>Completed</dd>
            <dt>Tokens</dt>
            <dd>{tokens} tok</dd>
            <dt>Reasoning</dt>
            <dd>0 tok</dd>
            <dt>Content</dt>
            <dd>{row.outputTokens || row.inputTokens} tok</dd>
          </dl>
          <h4>
            Preview
            <ChevronRight aria-hidden />
          </h4>
          <p className="jiuwenmate-trace-detail__preview">{row.content}</p>
          <h4>
            Request Timing
            <ChevronRight aria-hidden />
          </h4>
          <dl>
            <dt>Started</dt>
            <dd>
              {started.date} {started.time}
            </dd>
            <dt>Total Duration</dt>
            <dd>{seconds.toFixed(2)} s</dd>
            <dt>TTFT</dt>
            <dd>{Math.max(seconds - 0.01, 0).toFixed(2)} s</dd>
            <dt>Generation</dt>
            <dd>{Math.min(row.durationMs, 10)} ms</dd>
            <dt>Throughput</dt>
            <dd>{seconds > 0 ? (tokens / seconds).toFixed(1) : '0'}tok/s</dd>
          </dl>
        </div>
      ) : null}
      {tab === 'preview' ? <p className="jiuwenmate-trace-detail__body">{row.content}</p> : null}
      {tab === 'raw' ? <pre className="jiuwenmate-trace-detail__body">{JSON.stringify(raw, null, 2)}</pre> : null}
      {tab === 'otel' ? <pre className="jiuwenmate-trace-detail__body">{JSON.stringify(otel, null, 2)}</pre> : null}
    </aside>
  );
}

import { ChevronLeft, Download, Search } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SegmentedTabs } from '../components/SegmentedTabs';
import { TraceTimeline } from '../components/TraceTimeline';
import type { JiuwenMateTraceRow } from '../types';
import type { JiuwenMateTraceFilter, JiuwenMateTraceMetric } from '../workspaceState';

interface ChannelTracePageProps {
  rows: readonly JiuwenMateTraceRow[];
  startedAt: string;
  metric: JiuwenMateTraceMetric;
  filter: JiuwenMateTraceFilter;
  expandedTurns: readonly number[];
  selectedRowId: string | null;
  onMetricChange: (metric: JiuwenMateTraceMetric) => void;
  onToggleFilter: (filter: Exclude<JiuwenMateTraceFilter, 'none'>) => void;
  onToggleTurn: (turn: number) => void;
  onSelectRow: (rowId: string | null) => void;
  onBack: () => void;
}

function spanName(row: JiuwenMateTraceRow): string {
  return `agent.main.${row.role}.${row.traceId}/${row.id}`;
}

export function ChannelTracePage({
  rows,
  startedAt,
  metric,
  filter,
  expandedTurns,
  selectedRowId,
  onMetricChange,
  onToggleFilter,
  onToggleTurn,
  onSelectRow,
  onBack,
}: ChannelTracePageProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [rawSpanId, setRawSpanId] = useState(rows[0]?.id ?? '');
  const keyword = query.trim();
  const visibleRows = keyword ? rows.filter((row) => row.content.includes(keyword)) : rows;
  const traceCount = new Set(rows.map((row) => row.traceId)).size;
  const rawRow = rows.find((row) => row.id === rawSpanId) ?? rows[0];

  const exportTrace = () => {
    const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'jiuwenmate-trace.json';
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className="jiuwenmate-trace-page" data-testid="jiuwenmate-channel-trace-page">
      <header className="jiuwenmate-trace-page__header">
        <button type="button" className="jiuwenmate-trace-page__back" onClick={onBack} data-testid="jiuwenmate-trace-back-button">
          <ChevronLeft aria-hidden />
          <strong>{t('jiuwenmate.trace.title')}</strong>
        </button>
        <em>{t('jiuwenmate.trace.traceCount', { count: traceCount })}</em>
        <em>{t('jiuwenmate.trace.spanCount', { count: rows.length })}</em>
        <span className="jiuwenmate-trace-page__tools">
          <button type="button" className="jiuwenmate-link-button" onClick={exportTrace}>
            <Download aria-hidden />
            {t('jiuwenmate.trace.export')}
          </button>
          <label className="jiuwenmate-search">
            <Search aria-hidden />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('jiuwenmate.workspace.searchKeyword')}
              aria-label={t('jiuwenmate.workspace.searchKeyword')}
            />
          </label>
        </span>
      </header>
      <div className="jiuwenmate-trace-page__controls">
        <SegmentedTabs
          label={t('jiuwenmate.trace.metric')}
          variant="underline"
          tabs={[
            { value: 'duration' as const, label: t('jiuwenmate.trace.duration') },
            { value: 'tokens' as const, label: t('jiuwenmate.trace.tokens') },
          ]}
          value={metric}
          onChange={onMetricChange}
        />
        <span className="jiuwenmate-trace-page__filters" data-testid="jiuwenmate-trace-filter-list">
          {(['turn', 'tool'] as const).map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={filter === value}
              onClick={() => onToggleFilter(value)}
              data-testid="jiuwenmate-trace-filter"
              data-variant={value}
            >
              {t(`jiuwenmate.trace.filters.${value}`)}
            </button>
          ))}
        </span>
      </div>
      <TraceTimeline
        rows={visibleRows}
        startedAt={startedAt}
        metric={metric}
        filter={filter}
        expandedTurns={expandedTurns}
        selectedRowId={selectedRowId}
        onSelectRow={onSelectRow}
        onToggleTurn={onToggleTurn}
      />
      <section className="jiuwenmate-trace-page__raw" data-testid="jiuwenmate-trace-raw-record">
        <header>
          <strong>{t('jiuwenmate.trace.rawRecord', { count: rows.length })}</strong>
          <select value={rawRow?.id ?? ''} onChange={(event) => setRawSpanId(event.target.value)} aria-label={t('jiuwenmate.trace.rawSpan')}>
            {rows.map((row) => (
              <option key={row.id} value={row.id}>
                {spanName(row)}
              </option>
            ))}
          </select>
        </header>
        {rawRow ? (
          <pre>
            {JSON.stringify(
              { id: rawRow.id, name: spanName(rawRow), status: 'completed', trace_id: rawRow.traceId, turn: rawRow.turn },
              null,
              2,
            )}
          </pre>
        ) : null}
      </section>
    </section>
  );
}

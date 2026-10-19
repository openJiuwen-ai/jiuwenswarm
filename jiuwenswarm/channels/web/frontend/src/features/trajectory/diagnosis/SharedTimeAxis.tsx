/** Shared time axis ruler. When selectable, dragging picks one window; idle gaps are already removed from this axis. */

import { useRef, useState, type PointerEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { TrajectoryTimeRange } from '../trajectory/timeline';
import { formatElapsed, type WallClockDomain } from './diagnosisTimeSelection';
import css from './SharedTimeAxis.module.css';

const MINIMUM_DRAG_PX = 4;

export interface SharedTimeAxisProps {
  domain: WallClockDomain | null;
  /** Visible slice of `domain`. `null` shows the whole axis. */
  viewport?: TrajectoryTimeRange | null;
  range: TrajectoryTimeRange | null;
  onRangeChange: (range: TrajectoryTimeRange | null) => void;
  /** When false the axis is a plain ruler: dragging selects nothing. Defaults to true. */
  selectable?: boolean;
}

function clampFraction(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function fractionAt(event: PointerEvent<HTMLDivElement>): number {
  const rect = event.currentTarget.getBoundingClientRect();
  return clampFraction((event.clientX - rect.left) / Math.max(1, rect.width));
}

export function SharedTimeAxis({
  domain,
  viewport = null,
  range,
  onRangeChange,
  selectable = true,
}: SharedTimeAxisProps) {
  const { t } = useTranslation();
  const dragRef = useRef<{ pointerId: number; anchor: number; originX: number } | null>(null);
  const [draft, setDraft] = useState<TrajectoryTimeRange | null>(null);
  const origin = domain?.startMs ?? 0;
  const windowStart = domain === null
    ? 0
    : viewport === null ? domain.startMs : Math.max(domain.startMs, viewport.start);
  const windowEnd = domain === null
    ? 0
    : viewport === null ? domain.endMs : Math.min(domain.endMs, viewport.end);
  const duration = Math.max(0, windowEnd - windowStart);
  const visible = draft ?? range;
  // Endpoint labels follow the visible window. The selection's own labels stay
  // on the picked instants, so zooming the window does not rewrite them.
  const selection = visible === null || duration <= 0
    ? null
    : (() => {
      const overlapStart = Math.max(visible.start, windowStart);
      const overlapEnd = Math.min(visible.end, windowEnd);
      if (overlapEnd <= overlapStart) return null;
      const startFraction = (visible.start - windowStart) / duration;
      const endFraction = (visible.end - windowStart) / duration;
      return {
        left: clampFraction((overlapStart - windowStart) / duration),
        width: clampFraction((overlapEnd - overlapStart) / duration),
        startLabel: formatElapsed(visible.start - origin),
        endLabel: formatElapsed(visible.end - origin),
        showStart: startFraction >= -0.001 && startFraction <= 1.001,
        showEnd: endFraction >= -0.001 && endFraction <= 1.001,
      };
    })();

  const timeAt = (fraction: number): number => {
    if (domain === null || duration <= 0) return 0;
    return windowStart + fraction * duration;
  };

  return (
    <div className={css.row} data-testid="trajectory-analysis-time-axis">
      <span className={css.label}>{t('trajectory.diagnosis.timeAxis')}</span>
      <div className={css.plot}>
        <span className={css.boundStart}>{formatElapsed(windowStart - origin)}</span>
        <span className={css.boundEnd}>{formatElapsed(windowEnd - origin)}</span>
        <div
          className={css.track}
          aria-label={t('trajectory.diagnosis.timeAxis')}
          data-disabled={domain === null ? 'true' : 'false'}
          data-selectable={selectable ? 'true' : 'false'}
          onDoubleClick={(event) => {
            if (!selectable) return;
            event.preventDefault();
            onRangeChange(null);
          }}
          onPointerDown={(event) => {
            if (!selectable || domain === null || event.button !== 0) return;
            const anchor = fractionAt(event);
            dragRef.current = { pointerId: event.pointerId, anchor, originX: event.clientX };
            setDraft({ start: timeAt(anchor), end: timeAt(anchor) });
            if (typeof event.currentTarget.setPointerCapture === 'function') {
              event.currentTarget.setPointerCapture(event.pointerId);
            }
          }}
          onPointerMove={(event) => {
            const drag = dragRef.current;
            if (domain === null || drag === null || drag.pointerId !== event.pointerId) return;
            const point = fractionAt(event);
            const start = timeAt(Math.min(drag.anchor, point));
            const end = timeAt(Math.max(drag.anchor, point));
            setDraft({ start, end });
          }}
          onPointerUp={(event) => {
            const drag = dragRef.current;
            if (drag === null || drag.pointerId !== event.pointerId) return;
            dragRef.current = null;
            setDraft(null);
            if (domain === null) return;
            if (Math.abs(event.clientX - drag.originX) < MINIMUM_DRAG_PX) return;
            const point = fractionAt(event);
            const start = timeAt(Math.min(drag.anchor, point));
            const end = timeAt(Math.max(drag.anchor, point));
            onRangeChange(end > start ? { start, end } : null);
          }}
          onPointerCancel={() => {
            dragRef.current = null;
            setDraft(null);
          }}
        >
          {selection === null ? null : (
            <span
              className={css.selection}
              data-testid="trajectory-analysis-time-axis-selection"
              style={{ left: `${selection.left * 100}%`, width: `${Math.max(selection.width, 0.004) * 100}%` }}
            >
              {selection.showStart ? (
                <span className={css.selectionStart}>{selection.startLabel}</span>
              ) : null}
              {selection.showEnd ? (
                <span className={css.selectionEnd}>{selection.endLabel}</span>
              ) : null}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

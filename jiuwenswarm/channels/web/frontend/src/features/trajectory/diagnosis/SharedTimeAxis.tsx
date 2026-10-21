/** Shared time axis ruler. When selectable, dragging picks one window; idle gaps are already removed from this axis. */

import { useLayoutEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TrajectoryTimeRange } from '../trajectory/timeline';
import { formatElapsed, type WallClockDomain } from './diagnosisTimeSelection';
import css from './SharedTimeAxis.module.css';

const MINIMUM_DRAG_PX = 4;
const LABEL_GAP_PX = 8;
const LABEL_CHAR_PX = 8;

function textWidth(text: string): number {
  return Math.max(LABEL_CHAR_PX, text.length * LABEL_CHAR_PX);
}

interface PlacedSelectionLabels {
  showStart: boolean;
  showEnd: boolean;
  combined: string | null;
  /** `translateX` for the start label, whose left edge sits on the selection start. */
  startShift: number;
  /** `translateX` for the end label, whose right edge sits on the selection end. */
  endShift: number;
  /** `translateX` for the combined label, also anchored on the selection start. */
  combinedShift: number;
}

/**
 * Place the selection's endpoint labels so they miss each other and the axis ends.
 *
 * Matching an axis time drops that copy. A narrow window slides its two times apart.
 * When the pair cannot fit between the axis labels, they collapse into one string.
 */
function placeSelectionLabels(input: {
  startPx: number;
  endPx: number;
  plotWidth: number;
  startLabel: string;
  endLabel: string;
  boundStartLabel: string;
  boundEndLabel: string;
  startInView: boolean;
  endInView: boolean;
}): PlacedSelectionLabels {
  const empty: PlacedSelectionLabels = {
    showStart: false,
    showEnd: false,
    combined: null,
    startShift: 0,
    endShift: 0,
    combinedShift: 0,
  };
  if (input.plotWidth <= 0) {
    return {
      ...empty,
      showStart: input.startInView,
      showEnd: input.endInView,
    };
  }
  const startW = textWidth(input.startLabel);
  const endW = textWidth(input.endLabel);
  const minLeft = textWidth(input.boundStartLabel) + LABEL_GAP_PX;
  const maxRight = Math.max(minLeft, input.plotWidth - textWidth(input.boundEndLabel) - LABEL_GAP_PX);
  let showStart = input.startInView;
  let showEnd = input.endInView;
  let sLeft = input.startPx - startW / 2;
  let sRight = input.startPx + startW / 2;
  let eLeft = input.endPx - endW / 2;
  let eRight = input.endPx + endW / 2;
  if (showStart && input.startLabel === input.boundStartLabel && sLeft < minLeft) showStart = false;
  if (showEnd && input.endLabel === input.boundEndLabel && eRight > maxRight) showEnd = false;
  if (showStart && sLeft < minLeft) {
    sLeft = minLeft;
    sRight = sLeft + startW;
  }
  if (showEnd && eRight > maxRight) {
    eRight = maxRight;
    eLeft = eRight - endW;
  }
  if (showStart && showEnd && sRight + LABEL_GAP_PX > eLeft) {
    const pairWidth = startW + LABEL_GAP_PX + endW;
    if (input.startLabel === input.endLabel || pairWidth > maxRight - minLeft) {
      if (input.startLabel === input.endLabel) {
        showEnd = false;
        const mid = (input.startPx + input.endPx) / 2;
        sLeft = mid - startW / 2;
        sRight = sLeft + startW;
        if (sLeft < minLeft) {
          sLeft = minLeft;
          sRight = sLeft + startW;
        }
        if (sRight > maxRight) {
          sRight = maxRight;
          sLeft = sRight - startW;
        }
      } else {
        const combined = `${input.startLabel} – ${input.endLabel}`;
        const combinedW = textWidth(combined);
        let combinedLeft = (input.startPx + input.endPx) / 2 - combinedW / 2;
        combinedLeft = Math.min(Math.max(combinedLeft, minLeft), Math.max(minLeft, maxRight - combinedW));
        return {
          ...empty,
          combined,
          combinedShift: combinedLeft - input.startPx,
        };
      }
    } else {
      const mid = (input.startPx + input.endPx) / 2;
      sLeft = mid - pairWidth / 2;
      sRight = sLeft + startW;
      eLeft = sRight + LABEL_GAP_PX;
      eRight = eLeft + endW;
      if (sLeft < minLeft) {
        const delta = minLeft - sLeft;
        sLeft += delta;
        sRight += delta;
        eLeft += delta;
        eRight += delta;
      }
      if (eRight > maxRight) {
        const delta = eRight - maxRight;
        sLeft -= delta;
        sRight -= delta;
        eLeft -= delta;
        eRight -= delta;
      }
    }
  }
  return {
    showStart,
    showEnd,
    combined: null,
    startShift: showStart ? sLeft - input.startPx : 0,
    endShift: showEnd ? eRight - input.endPx : 0,
    combinedShift: 0,
  };
}

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

function fractionIn(clientX: number, plot: HTMLElement): number {
  const rect = plot.getBoundingClientRect();
  return clampFraction((clientX - rect.left) / Math.max(1, rect.width));
}

export function SharedTimeAxis({
  domain,
  viewport = null,
  range,
  onRangeChange,
  selectable = true,
}: SharedTimeAxisProps) {
  const { t } = useTranslation();
  const plotRef = useRef<HTMLDivElement>(null);
  const [plotWidth, setPlotWidth] = useState(0);
  const dragRef = useRef<{ pointerId: number; anchor: number; originX: number } | null>(null);
  useLayoutEffect(() => {
    const plot = plotRef.current;
    if (plot === null) return undefined;
    const measure = () => {
      const next = plot.clientWidth;
      setPlotWidth(current => (current === next ? current : next));
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(plot);
    return () => observer.disconnect();
  }, []);
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
  const boundStartLabel = formatElapsed(windowStart - origin);
  const boundEndLabel = formatElapsed(windowEnd - origin);
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
      const left = clampFraction((overlapStart - windowStart) / duration);
      const width = clampFraction((overlapEnd - overlapStart) / duration);
      const startLabel = formatElapsed(visible.start - origin);
      const endLabel = formatElapsed(visible.end - origin);
      const placed = placeSelectionLabels({
        startPx: left * plotWidth,
        endPx: (left + width) * plotWidth,
        plotWidth,
        startLabel,
        endLabel,
        boundStartLabel,
        boundEndLabel,
        startInView: startFraction >= -0.001 && startFraction <= 1.001,
        endInView: endFraction >= -0.001 && endFraction <= 1.001,
      });
      return { left, width, startLabel, endLabel, ...placed };
    })();

  const timeAt = (fraction: number): number => {
    if (domain === null || duration <= 0) return 0;
    return windowStart + fraction * duration;
  };

  const fractionAt = (clientX: number): number | null => {
    const plot = plotRef.current;
    if (plot === null) return null;
    const rect = plot.getBoundingClientRect();
    if (clientX < rect.left || clientX > rect.right) return null;
    return fractionIn(clientX, plot);
  };

  return (
    <div
      className={css.row}
      data-testid="trajectory-analysis-time-axis"
      data-disabled={domain === null ? 'true' : 'false'}
      data-selectable={selectable ? 'true' : 'false'}
      onDoubleClick={(event) => {
        if (!selectable || fractionAt(event.clientX) === null) return;
        event.preventDefault();
        onRangeChange(null);
      }}
      onPointerDown={(event) => {
        if (!selectable || domain === null || event.button !== 0) return;
        const anchor = fractionAt(event.clientX);
        if (anchor === null) return;
        dragRef.current = { pointerId: event.pointerId, anchor, originX: event.clientX };
        setDraft({ start: timeAt(anchor), end: timeAt(anchor) });
        if (typeof event.currentTarget.setPointerCapture === 'function') {
          event.currentTarget.setPointerCapture(event.pointerId);
        }
      }}
      onPointerMove={(event) => {
        const drag = dragRef.current;
        if (domain === null || drag === null || drag.pointerId !== event.pointerId) return;
        const plot = plotRef.current;
        if (plot === null) return;
        const point = fractionIn(event.clientX, plot);
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
        const plot = plotRef.current;
        if (plot === null) return;
        const point = fractionIn(event.clientX, plot);
        const start = timeAt(Math.min(drag.anchor, point));
        const end = timeAt(Math.max(drag.anchor, point));
        onRangeChange(end > start ? { start, end } : null);
      }}
      onPointerCancel={() => {
        dragRef.current = null;
        setDraft(null);
      }}
    >
      <span className={css.label}>{t('trajectory.diagnosis.timeAxis')}</span>
      <div ref={plotRef} className={css.plot}>
        <div className={css.bounds}>
          <span className={css.boundStart}>{boundStartLabel}</span>
          <span className={css.boundEnd}>{boundEndLabel}</span>
        </div>
        <div
          className={css.track}
          aria-label={t('trajectory.diagnosis.timeAxis')}
        >
          {selection === null ? null : (
            <span
              className={css.selection}
              data-testid="trajectory-analysis-time-axis-selection"
              style={{ left: `${selection.left * 100}%`, width: `${Math.max(selection.width, 0.004) * 100}%` }}
            >
              {selection.combined !== null ? (
                <span
                  className={css.selectionCombined}
                  style={{ transform: `translateX(${selection.combinedShift}px)` }}
                >
                  {selection.combined}
                </span>
              ) : null}
              {selection.showStart ? (
                <span
                  className={css.selectionStart}
                  style={plotWidth > 0
                    ? { transform: `translateX(${selection.startShift}px)` }
                    : undefined}
                >
                  {selection.startLabel}
                </span>
              ) : null}
              {selection.showEnd ? (
                <span
                  className={css.selectionEnd}
                  style={plotWidth > 0
                    ? { transform: `translateX(${selection.endShift}px)` }
                    : undefined}
                >
                  {selection.endLabel}
                </span>
              ) : null}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Chrome-Network-style overview timeline for focusing the trajectory ledger.
 * Adapted mechanically from `packages/client/ui-trajectory/src/client/TrajectoryTimeline.tsx`
 * under the repository MIT license.
 */

import {
  memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties,
  type KeyboardEvent, type PointerEvent,
} from 'react'
import { Tooltip } from '../primitives/index.ts'
import type { TrajectoryTurnModel } from '../trajectory/model.ts'
import type { AssistantMetricDetail, TrajectoryCellKind, TrajectoryCellProps } from '../trajectory/record.ts'
import { formatTokenCount, liveElapsedSeconds } from '../trajectory/record.ts'
import {
  deriveTrajectoryTimeline,
  formatTimelineOffset,
  panTrajectoryTimelineViewport,
  type TrajectoryTimelineMode,
  type TrajectoryTimelineSegment,
  type TrajectoryTimelineSpan,
  type TrajectoryTimeRange,
  zoomTrajectoryTimelineViewport,
} from '../trajectory/timeline.ts'
import css from './TrajectoryTimeline.module.css'

const MINIMUM_DRAG_PX = 3
const MINIMUM_ZOOM_OPERATIONS = 4
const MINIMUM_ZOOM_TOKENS = 256
const MINIMUM_ZOOM_MILLIS = 20
const EDGE_PAN_ZONE_FRACTION = 0.08
const EDGE_PAN_STEP_FRACTION = 0.025
const MAXIMUM_EDGE_PAN_PX = 32
const TIMELINE_TOOLTIP_DELAY_MS = 500
const WHEEL_SETTLE_MS = 140

interface TimelineRecordDetail {
  decodingMs?: number
  durationMs?: number
  startedAt?: number
  ttftMs?: number
  cachedInput?: number
  cacheWriteInput?: number
  processedInput?: number
  outputTokens?: number
  reasoningTokens?: number
}

interface FractionRange {
  start: number
  end: number
}

interface HoverPoint {
  fraction: number
  recordIndex: number | null
}

interface PanGesture {
  anchorClientX: number
  anchorStart: number
  anchorTime: number
  button: number
  duration: number
  moved: boolean
  pannable: boolean
  pointerId: number
  spanKey: string | null
}

function assistantTimingDetail(
  metrics: AssistantMetricDetail | undefined,
): Pick<TimelineRecordDetail, 'ttftMs' | 'decodingMs'> {
  const start = metrics?.stepStartTime
  const first = metrics?.firstTokenTime
  const completed = metrics?.completedTime
  if (
    metrics?.timingRecorded !== true
    || typeof start !== 'number'
    || typeof first !== 'number'
    || typeof completed !== 'number'
    || !Number.isFinite(start)
    || !Number.isFinite(first)
    || !Number.isFinite(completed)
    || first < start
    || completed < first
  ) return {}
  return { ttftMs: first - start, decodingMs: completed - first }
}

function tokenUsageDetail(
  cell: TrajectoryCellProps,
): Pick<
  TimelineRecordDetail,
  'cachedInput' | 'cacheWriteInput' | 'processedInput' | 'outputTokens' | 'reasoningTokens'
> {
  const cachedInput = cell.cacheRead
  const processedInput = cell.input === undefined
    ? undefined
    : Math.max(0, cell.input - (cell.cacheRead ?? 0))
  return {
    ...(cachedInput === undefined ? {} : { cachedInput }),
    ...(cell.cacheWrite === undefined ? {} : { cacheWriteInput: cell.cacheWrite }),
    ...(processedInput === undefined ? {} : { processedInput }),
    ...(cell.output === undefined ? {} : { outputTokens: cell.output }),
    ...(cell.think === undefined ? {} : { reasoningTokens: cell.think }),
  }
}

function timelineRecordDetail(
  cell: TrajectoryCellProps,
  nowMilliseconds: number,
): TimelineRecordDetail {
  const elapsed = liveElapsedSeconds(cell, nowMilliseconds)
  const durationMs = elapsed === null || !Number.isFinite(elapsed)
    ? undefined
    : Math.max(0, elapsed * 1_000)
  const startedAt = cell.startedAt === null || !Number.isFinite(cell.startedAt)
    ? undefined
    : cell.startedAt
  return {
    ...(durationMs === undefined ? {} : { durationMs }),
    ...(startedAt === undefined ? {} : { startedAt }),
    ...assistantTimingDetail(cell.assistantMetrics),
    ...tokenUsageDetail(cell),
  }
}

function timelineKindLabel(kind: TrajectoryCellKind): string {
  switch (kind) {
    case 'system': return 'SYSTEM'
    case 'user': return 'USER'
    case 'context': return 'CONTEXT'
    case 'compacted': return 'COMPACTED'
    case 'message': return 'ASSISTANT'
    case 'tool': return 'TOOL'
    case 'subtool': return 'SUBTOOL'
  }
}

function formatRecordedTime(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    fractionalSecondDigits: 3,
  })
}

function tokenTooltipLabel(
  segment: TrajectoryTimelineSegment,
  detail: TimelineRecordDetail | undefined,
): string {
  if (segment === 'input') {
    const processed = detail?.processedInput
    const cacheWrite = detail?.cacheWriteInput
    const cached = detail?.cachedInput
    const uncached = processed === undefined || cacheWrite === undefined
      ? null
      : `Uncached ${formatTokenCount(Math.max(0, processed - cacheWrite))}`
    return [
      'INPUT',
      [
        `Processed ${formatTokenCount(processed)}`,
        uncached,
        cacheWrite === undefined ? null : `Cache write ${formatTokenCount(cacheWrite)}`,
        cached === undefined ? null : `Cached ${formatTokenCount(cached)}`,
      ].filter(value => value !== null).join(' · '),
    ].join('\n')
  }
  const output = detail?.outputTokens
  const reasoning = detail?.reasoningTokens
  const content = output === undefined || reasoning === undefined
    ? null
    : `Content ${formatTokenCount(Math.max(0, output - reasoning))}`
  return [
    'OUTPUT',
    [
      `Total ${formatTokenCount(output)}`,
      reasoning === undefined ? null : `Reasoning ${formatTokenCount(reasoning)}`,
      content,
    ].filter(value => value !== null).join(' · '),
  ].join('\n')
}

function timelineTooltipLabel(
  kind: TrajectoryCellKind,
  detail: TimelineRecordDetail | undefined,
  segment: TrajectoryTimelineSegment | undefined,
): string {
  if (segment !== undefined) return tokenTooltipLabel(segment, detail)
  const heading = timelineKindLabel(kind)
  if (detail === undefined) return heading
  const duration = detail.durationMs === undefined
    ? null
    : `Total ${formatTimelineOffset(detail.durationMs)}`
  const range = detail.startedAt === undefined
    ? null
    : detail.durationMs === undefined
      ? `Started ${formatRecordedTime(detail.startedAt)}`
      : `${formatRecordedTime(detail.startedAt)} → ${formatRecordedTime(
        detail.startedAt + detail.durationMs,
      )}`
  const segments = detail.ttftMs === undefined || detail.decodingMs === undefined
    ? null
    : `TTFT ${formatTimelineOffset(detail.ttftMs)} · Decoding ${formatTimelineOffset(
      detail.decodingMs,
    )}`
  const timing = [duration, segments].filter(value => value !== null).join(' · ')
  return [heading, range, timing].filter(value => value !== null && value !== '').join('\n')
}

/** Props for the fixed full-domain overview above the trajectory ledger. */
export interface TrajectoryTimelineProps {
  turns: readonly TrajectoryTurnModel[]
  mode: TrajectoryTimelineMode
  range: TrajectoryTimeRange | null
  /** Whether one earlier history page request is pending anywhere. */
  olderHistoryLoading?: boolean
  /** Whether the loaded timeline omits an earlier history prefix. */
  hasEarlierRecords?: boolean
  /** Load one earlier history page from the truncation control. */
  onLoadEarlier?: () => Promise<boolean>
  selectedIndex?: number | null
  /** Record indexes matching the active ledger search, or null without a query. */
  searchMatchIndexes?: ReadonlySet<number> | null
  onRangeChange: (range: TrajectoryTimeRange | null) => void
  /** Select a directly clicked timeline block. */
  onRecordSelect?: (index: number) => void
  /** Bring the nearest record into view after clicking timeline whitespace. */
  onRecordFocus?: (index: number) => void
  /** Shared presentation clock used only for open running intervals. */
  nowMilliseconds?: number
}

function minimumZoomDomain(mode: TrajectoryTimelineMode): number {
  if (mode === 'sequence') return MINIMUM_ZOOM_OPERATIONS
  if (mode === 'tokens') return MINIMUM_ZOOM_TOKENS
  return MINIMUM_ZOOM_MILLIS
}

function spanKey(span: TrajectoryTimelineSpan): string {
  return `${span.lane}:${span.index}`
}

function spanCovers(span: TrajectoryTimelineSpan, index: number | null): boolean {
  if (index === null) return false
  return span.coveredIndexes === undefined
    ? span.index === index
    : span.index === index || span.coveredIndexes.includes(index)
}

function orderedRange(left: number, right: number): FractionRange {
  return left <= right ? { start: left, end: right } : { start: right, end: left }
}

function clampFraction(value: number): number {
  return Math.min(1, Math.max(0, value))
}

function centeredRange(
  center: number,
  width: number,
  minimum: number,
  maximum: number,
): FractionRange {
  const clampedWidth = Math.min(maximum - minimum, Math.max(0, width))
  const start = Math.min(
    Math.max(center - clampedWidth / 2, minimum),
    maximum - clampedWidth,
  )
  return { start, end: start + clampedWidth }
}

function rangeFraction(
  range: TrajectoryTimeRange,
  start: number,
  duration: number,
  minimum: number,
  maximum: number,
): FractionRange {
  const bounded = orderedRange(
    Math.min(maximum, Math.max(minimum, range.start)),
    Math.min(maximum, Math.max(minimum, range.end)),
  )
  return {
    start: (bounded.start - start) / duration,
    end: (bounded.end - start) / duration,
  }
}

function viewportGeometry(
  model: TrajectoryTimeRange,
  viewport: TrajectoryTimeRange | null,
): { domainDuration: number; domainStart: number; fullDuration: number } {
  const fullDuration = Math.max(1, model.end - model.start)
  const domainDuration = viewport === null
    ? fullDuration
    : Math.min(fullDuration, Math.max(1, viewport.end - viewport.start))
  const domainStart = viewport === null
    ? model.start
    : Math.min(Math.max(viewport.start, model.start), model.end - domainDuration)
  return { domainDuration, domainStart, fullDuration }
}

function projectedDomainStyle(
  model: TrajectoryTimeRange,
  viewport: TrajectoryTimeRange | null,
): CSSProperties {
  const { domainDuration, domainStart, fullDuration } = viewportGeometry(model, viewport)
  return {
    '--trajectory-domain-left': `${-(domainStart - model.start) / domainDuration * 100}%`,
    '--trajectory-domain-width': `${fullDuration / domainDuration * 100}%`,
  } as CSSProperties
}

function applyProjectedDomain(
  track: HTMLElement,
  model: TrajectoryTimeRange,
  viewport: TrajectoryTimeRange | null,
): void {
  const { domainDuration, domainStart, fullDuration } = viewportGeometry(model, viewport)
  track.style.setProperty(
    '--trajectory-domain-left',
    `${-(domainStart - model.start) / domainDuration * 100}%`,
  )
  track.style.setProperty(
    '--trajectory-domain-width',
    `${fullDuration / domainDuration * 100}%`,
  )
  track.querySelectorAll<HTMLElement>('[data-animate-viewport]').forEach((element) => {
    element.removeAttribute('data-animate-viewport')
  })
}

function LaneLabels({ mode }: { mode: TrajectoryTimelineMode }) {
  if (mode === 'tokens') {
    return (
      <div className={css.labels} aria-hidden="true">
        <span>Input</span>
        <span>Output</span>
      </div>
    )
  }
  return (
    <div className={css.labels} aria-hidden="true">
      <span>Input</span>
      <span>Model</span>
      <span>Tools</span>
    </div>
  )
}

function EarlierHistoryBoundary({
  loading,
  onHover,
  onLoad,
}: {
  loading: boolean
  onHover: () => void
  onLoad: (() => void) | undefined
}) {
  return (
    <Tooltip
      label={loading ? 'Loading earlier history…' : 'Click to load earlier history'}
      side="right"
      delayMs={TIMELINE_TOOLTIP_DELAY_MS}
    >
      <button
        type="button"
        className={css.earlierHistory}
        data-earlier-history
        data-loading={loading || undefined}
        aria-label={loading ? 'Loading earlier history' : 'Load earlier history'}
        aria-disabled={loading || onLoad === undefined}
        disabled={loading || onLoad === undefined}
        onClick={onLoad}
        onPointerEnter={(event) => {
          event.stopPropagation()
          onHover()
        }}
        onPointerMove={(event) => { event.stopPropagation() }}
        onPointerDown={(event) => { event.stopPropagation() }}
      >
        …
      </button>
    </Tooltip>
  )
}

/** Overview renderer with deferred wheel zoom, horizontal panning, range focus, and reset. */
export const TrajectoryTimeline = memo(function TrajectoryTimeline({
  turns,
  mode,
  range,
  olderHistoryLoading = false,
  hasEarlierRecords = false,
  onLoadEarlier,
  selectedIndex = null,
  searchMatchIndexes = null,
  onRangeChange,
  onRecordSelect,
  onRecordFocus,
  nowMilliseconds = Date.now(),
}: TrajectoryTimelineProps) {
  const model = useMemo(
    () => deriveTrajectoryTimeline(turns, mode, nowMilliseconds),
    [mode, nowMilliseconds, turns],
  )
  const detailByIndex = useMemo(
    () => new Map(turns.flatMap(turn =>
      turn.groups.flatMap(group =>
        group.cells.map(cell => [cell.index, timelineRecordDetail(cell, nowMilliseconds)] as const),
      ),
    )),
    [nowMilliseconds, turns],
  )
  const dragRef = useRef<{
    pointerId: number
    anchorTime: number
    anchorClientX: number
    recordIndex: number | null
    spanKey: string | null
  } | null>(null)
  const panRef = useRef<PanGesture | null>(null)
  const rootRef = useRef<HTMLElement | null>(null)
  const trackRef = useRef<HTMLDivElement | null>(null)
  const pendingViewportRef = useRef<TrajectoryTimeRange | null | undefined>(undefined)
  const wheelCommitTimerRef = useRef<number | null>(null)
  const [draft, setDraft] = useState<TrajectoryTimeRange | null>(null)
  const [hover, setHover] = useState<HoverPoint | null>(null)
  const loadingEarlierRef = useRef<Promise<boolean> | null>(null)
  const [localOlderHistoryLoading, setLocalOlderHistoryLoading] = useState(false)
  useEffect(() => {
    loadingEarlierRef.current = null
    setLocalOlderHistoryLoading(false)
  }, [onLoadEarlier])
  const [panning, setPanning] = useState(false)
  const [viewport, setViewport] = useState<TrajectoryTimeRange | null>(null)
  const [animateViewport, setAnimateViewport] = useState(false)
  const modelRef = useRef(model)
  const viewportRef = useRef(viewport)
  const modeRef = useRef(mode)
  modelRef.current = model
  viewportRef.current = viewport
  modeRef.current = mode
  useEffect(() => {
    if (
      model !== null
      && range !== null
      && (range.end < model.start || range.start > model.end)
    ) {
      onRangeChange(null)
    }
  }, [model, onRangeChange, range])
  useEffect(() => {
    if (model === null) return
    setAnimateViewport(false)
    setViewport(current =>
      current !== null && (current.end < model.start || current.start > model.end)
        ? null
        : current)
  }, [model])
  useLayoutEffect(() => {
    const pending = pendingViewportRef.current
    if (model === null || pending === undefined || trackRef.current === null) return
    applyProjectedDomain(trackRef.current, model, pending)
  }, [model])
  useEffect(() => {
    if (model === null || selectedIndex === null) return
    const selectedSpan = model.spans.find(span => spanCovers(span, selectedIndex))
    if (selectedSpan === undefined) return
    setAnimateViewport(true)
    setViewport((current) => {
      if (current === null) return current
      if (
        selectedSpan.end > current.start
        && selectedSpan.start < current.end
      ) return current
      const duration = Math.max(1, current.end - current.start)
      const desiredStart = selectedSpan.end <= current.start
        ? selectedSpan.start
        : selectedSpan.end - duration
      const nextStart = Math.min(
        Math.max(desiredStart, model.start),
        Math.max(model.start, model.end - duration),
      )
      if (nextStart === current.start) return current
      return { start: nextStart, end: nextStart + duration }
    })
  }, [model, selectedIndex])
  const geometry = model === null
    ? { domainDuration: 1, domainStart: 0, fullDuration: 1 }
    : viewportGeometry(model, viewport)
  const { domainDuration, domainStart, fullDuration } = geometry
  const showsEarlierBoundary = hasEarlierRecords
    && model !== null
    && domainStart === model.start
  const olderBusy = olderHistoryLoading || localOlderHistoryLoading
  const loadEarlier = onLoadEarlier === undefined || olderBusy
    ? undefined
    : () => {
      if (loadingEarlierRef.current !== null || olderHistoryLoading) return
      const operation = onLoadEarlier()
      loadingEarlierRef.current = operation
      setLocalOlderHistoryLoading(true)
      void operation.finally(() => {
        if (loadingEarlierRef.current !== operation) return
        loadingEarlierRef.current = null
        setLocalOlderHistoryLoading(false)
      })
    }
  const domainStyle = model === null
    ? undefined
    : projectedDomainStyle(model, viewport)
  const committed = model === null || range === null
    ? null
    : rangeFraction(range, domainStart, domainDuration, model.start, model.end)
  const draftFraction = model === null || draft === null
    ? null
    : rangeFraction(draft, domainStart, domainDuration, model.start, model.end)
  const visibleRange = draftFraction ?? committed
  const activeRange = draft ?? range
  useEffect(() => {
    const root = rootRef.current
    if (root === null) return
    const onWheel = (event: globalThis.WheelEvent): void => {
      const track = trackRef.current
      const currentModel = modelRef.current
      if (track === null || currentModel === null) return
      const rect = track.getBoundingClientRect()
      const unit = event.deltaMode === globalThis.WheelEvent.DOM_DELTA_LINE
        ? 16
        : event.deltaMode === globalThis.WheelEvent.DOM_DELTA_PAGE ? rect.width : 1
      const horizontal = event.shiftKey && Math.abs(event.deltaX) < Math.abs(event.deltaY)
        ? event.deltaY
        : event.deltaX
      const pansHorizontally = event.shiftKey
        || Math.abs(event.deltaX) > Math.abs(event.deltaY)
      const currentViewport = pendingViewportRef.current === undefined
        ? viewportRef.current
        : pendingViewportRef.current
      const nextViewport = pansHorizontally
        ? panTrajectoryTimelineViewport(
          currentModel,
          currentViewport,
          horizontal * unit / Math.max(1, rect.width),
        )
        : zoomTrajectoryTimelineViewport(
          currentModel,
          currentViewport,
          clampFraction((event.clientX - rect.left) / Math.max(1, rect.width)),
          event.deltaY * unit,
          minimumZoomDomain(modeRef.current),
        )
      if (pansHorizontally && currentViewport === null) return
      event.preventDefault()
      pendingViewportRef.current = nextViewport
      applyProjectedDomain(track, currentModel, nextViewport)
      if (wheelCommitTimerRef.current !== null) {
        window.clearTimeout(wheelCommitTimerRef.current)
      }
      wheelCommitTimerRef.current = window.setTimeout(() => {
        const pending = pendingViewportRef.current
        wheelCommitTimerRef.current = null
        pendingViewportRef.current = undefined
        if (pending === undefined) return
        setAnimateViewport(false)
        setViewport(pending)
      }, WHEEL_SETTLE_MS)
    }
    root.addEventListener('wheel', onWheel, { passive: false })
    return () => {
      root.removeEventListener('wheel', onWheel)
      if (wheelCommitTimerRef.current !== null) {
        window.clearTimeout(wheelCommitTimerRef.current)
        wheelCommitTimerRef.current = null
      }
    }
  }, [])

  if (model === null) {
    return (
      <section ref={rootRef} className={css.root} aria-label="Trajectory timeline">
        <div className={css.plot}>
          <LaneLabels mode={mode} />
          <div className={css.track}>
            <span className={css.empty}>No timing data</span>
            {hasEarlierRecords && (
              <EarlierHistoryBoundary
                loading={olderBusy}
                onHover={() => { setHover(null) }}
                onLoad={loadEarlier}
              />
            )}
          </div>
        </div>
      </section>
    )
  }

  const minimumSelectionDuration = Math.min(
    domainDuration,
    fullDuration / model.spans.length,
  )

  const fractionAt = (event: PointerEvent<HTMLDivElement>): number => {
    const rect = event.currentTarget.getBoundingClientRect()
    return clampFraction((event.clientX - rect.left) / Math.max(1, rect.width))
  }

  const recordIndexAt = (event: PointerEvent<HTMLDivElement>): number | null => {
    const target = event.target instanceof HTMLElement ? event.target : null
    const value = target?.closest<HTMLElement>('[data-timeline-record-index]')
      ?.dataset.timelineRecordIndex
    if (value === undefined) return null
    const index = Number(value)
    return Number.isFinite(index) ? index : null
  }

  const spanKeyAt = (event: PointerEvent<HTMLDivElement>): string | null => {
    const target = event.target instanceof HTMLElement ? event.target : null
    return target?.closest<HTMLElement>('[data-timeline-span-key]')
      ?.dataset.timelineSpanKey ?? null
  }

  const commit = (nextRange: TrajectoryTimeRange) => {
    onRangeChange(nextRange)
  }

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const currentViewport = pendingViewportRef.current === undefined
      ? viewport
      : pendingViewportRef.current
    const currentGeometry = viewportGeometry(model, currentViewport)
    const startsPan = event.button === 1
      || event.button === 2
      || (event.button === 0 && currentViewport !== null && !event.shiftKey)
    if (startsPan) {
      panRef.current = {
        anchorClientX: event.clientX,
        anchorStart: currentGeometry.domainStart,
        anchorTime: currentGeometry.domainStart
          + fractionAt(event) * currentGeometry.domainDuration,
        button: event.button,
        duration: currentGeometry.domainDuration,
        moved: false,
        pannable: currentViewport !== null,
        pointerId: event.pointerId,
        spanKey: spanKeyAt(event),
      }
      if (wheelCommitTimerRef.current !== null) {
        window.clearTimeout(wheelCommitTimerRef.current)
        wheelCommitTimerRef.current = null
      }
      setHover(null)
      if (currentViewport !== null) setPanning(true)
      if (typeof event.currentTarget.setPointerCapture === 'function') {
        event.currentTarget.setPointerCapture(event.pointerId)
      }
      event.preventDefault()
      return
    }
    if (event.button !== 0) return
    const anchor = fractionAt(event)
    const anchorTime = domainStart + anchor * domainDuration
    const recordIndex = recordIndexAt(event)
    setHover({ fraction: anchor, recordIndex })
    dragRef.current = {
      pointerId: event.pointerId,
      anchorTime,
      anchorClientX: event.clientX,
      recordIndex,
      spanKey: spanKeyAt(event),
    }
    if (typeof event.currentTarget.setPointerCapture === 'function') {
      event.currentTarget.setPointerCapture(event.pointerId)
    }
    setDraft({ start: anchorTime, end: anchorTime })
  }

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const pan = panRef.current
    if (pan !== null && pan.pointerId === event.pointerId) {
      if (Math.abs(event.clientX - pan.anchorClientX) >= MINIMUM_DRAG_PX) {
        pan.moved = true
      }
      if (!pan.pannable || !pan.moved) return
      const delta = (event.clientX - pan.anchorClientX) / Math.max(1, rect.width)
      const nextViewport = panTrajectoryTimelineViewport(
        model,
        { start: pan.anchorStart, end: pan.anchorStart + pan.duration },
        -delta,
      )
      pendingViewportRef.current = nextViewport
      applyProjectedDomain(event.currentTarget, model, nextViewport)
      return
    }
    const fraction = fractionAt(event)
    setHover({ fraction, recordIndex: recordIndexAt(event) })
    const drag = dragRef.current
    if (drag === null || drag.pointerId !== event.pointerId) return
    let nextDomainStart = domainStart
    if (viewport !== null) {
      const localX = event.clientX - rect.left
      const edgeWidth = Math.min(
        MAXIMUM_EDGE_PAN_PX,
        Math.max(1, rect.width * EDGE_PAN_ZONE_FRACTION),
      )
      const direction = localX < edgeWidth
        ? -1
        : localX > rect.width - edgeWidth ? 1 : 0
      if (direction !== 0) {
        const edgeDistance = direction < 0
          ? edgeWidth - localX
          : localX - (rect.width - edgeWidth)
        const strength = clampFraction(edgeDistance / edgeWidth)
        const desiredStart = domainStart
          + direction * domainDuration * EDGE_PAN_STEP_FRACTION
          * Math.max(0.2, strength)
        nextDomainStart = Math.min(
          Math.max(desiredStart, model.start),
          model.end - domainDuration,
        )
        if (nextDomainStart !== domainStart) {
          setAnimateViewport(false)
          setViewport({
            start: nextDomainStart,
            end: nextDomainStart + domainDuration,
          })
        }
      }
    }
    const pointTime = nextDomainStart + fraction * domainDuration
    setDraft(orderedRange(drag.anchorTime, pointTime))
  }

  const onPointerEnd = (event: PointerEvent<HTMLDivElement>) => {
    const pan = panRef.current
    if (pan !== null && pan.pointerId === event.pointerId) {
      const moved = pan.moved
        || Math.abs(event.clientX - pan.anchorClientX) >= MINIMUM_DRAG_PX
      panRef.current = null
      setPanning(false)
      if (moved && pan.pannable) {
        const pending = pendingViewportRef.current
        pendingViewportRef.current = undefined
        if (pending !== undefined) {
          setAnimateViewport(false)
          setViewport(pending)
        }
        return
      }
      pendingViewportRef.current = undefined
      applyProjectedDomain(event.currentTarget, model, viewport)
      if (pan.button === 2) {
        onRangeChange(null)
        return
      }
      if (pan.button !== 0) return
      const clickedSpan = pan.spanKey === null
        ? undefined
        : model.spans.find(span => spanKey(span) === pan.spanKey)
      if (clickedSpan !== undefined) {
        if (clickedSpan.segment === 'input') {
          onRangeChange({ start: clickedSpan.start, end: clickedSpan.end })
          onRecordFocus?.(clickedSpan.coveredIndexes?.[0] ?? clickedSpan.index)
          return
        }
        onRangeChange(null)
        onRecordSelect?.(clickedSpan.index)
        return
      }
      const clickRange = centeredRange(
        pan.anchorTime,
        Math.min(pan.duration, fullDuration / model.spans.length),
        model.start,
        model.end,
      )
      commit(clickRange)
      const nearest = model.spans.reduce((candidate, span) => {
        const candidateDistance = pan.anchorTime < candidate.start
          ? candidate.start - pan.anchorTime
          : pan.anchorTime > candidate.end ? pan.anchorTime - candidate.end : 0
        const spanDistance = pan.anchorTime < span.start
          ? span.start - pan.anchorTime
          : pan.anchorTime > span.end ? pan.anchorTime - span.end : 0
        return spanDistance < candidateDistance ? span : candidate
      })
      onRecordFocus?.(nearest.index)
      return
    }
    const drag = dragRef.current
    if (drag === null || drag.pointerId !== event.pointerId) return
    const pointFraction = fractionAt(event)
    const pointTime = domainStart + pointFraction * domainDuration
    const selected = orderedRange(drag.anchorTime, pointTime)
    setHover({ fraction: pointFraction, recordIndex: recordIndexAt(event) })
    dragRef.current = null
    setDraft(null)
    const click = Math.abs(event.clientX - drag.anchorClientX) < MINIMUM_DRAG_PX
    const clickedSpan = click && drag.spanKey !== null
      ? model.spans.find(span => spanKey(span) === drag.spanKey)
      : undefined
    if (clickedSpan !== undefined) {
      // An Input block stands for the records its request consumed, so clicking it
      // focuses that whole stretch of the ledger instead of the request row itself.
      if (clickedSpan.segment === 'input') {
        onRangeChange({ start: clickedSpan.start, end: clickedSpan.end })
        onRecordFocus?.(clickedSpan.coveredIndexes?.[0] ?? clickedSpan.index)
        return
      }
      onRangeChange(null)
      onRecordSelect?.(clickedSpan.index)
      return
    }
    const committedRange = selected.end - selected.start < minimumSelectionDuration
      ? centeredRange(
        click ? selected.start : (selected.start + selected.end) / 2,
        minimumSelectionDuration,
        model.start,
        model.end,
      )
      : selected
    commit(committedRange)
    if (click) {
      const timelinePoint = selected.start
      const nearest = model.spans.reduce((candidate, span) => {
        const candidateDistance = timelinePoint < candidate.start
          ? candidate.start - timelinePoint
          : timelinePoint > candidate.end ? timelinePoint - candidate.end : 0
        const spanDistance = timelinePoint < span.start
          ? span.start - timelinePoint
          : timelinePoint > span.end ? timelinePoint - span.end : 0
        return spanDistance < candidateDistance ? span : candidate
      })
      onRecordFocus?.(nearest.index)
    }
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Escape' || range === null) return
    event.preventDefault()
    onRangeChange(null)
  }

  const onPointerCancel = () => {
    dragRef.current = null
    panRef.current = null
    pendingViewportRef.current = undefined
    if (wheelCommitTimerRef.current !== null) {
      window.clearTimeout(wheelCommitTimerRef.current)
      wheelCommitTimerRef.current = null
    }
    if (trackRef.current !== null && modelRef.current !== null) {
      applyProjectedDomain(trackRef.current, modelRef.current, viewportRef.current)
    }
    setDraft(null)
    setHover(null)
    setPanning(false)
  }

  return (
    <section ref={rootRef} className={css.root} aria-label="Trajectory timeline">
      <div className={css.plot}>
        <LaneLabels mode={mode} />
        <div
          ref={trackRef}
          className={css.track}
          data-panning={panning || undefined}
          data-pannable={viewport !== null || undefined}
          aria-label="Timeline overview; scroll or drag horizontally to pan, wheel to zoom, Shift-drag to focus events"
          style={domainStyle}
          tabIndex={0}
          onKeyDown={onKeyDown}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerEnd}
          onPointerCancel={onPointerCancel}
          onPointerLeave={() => {
            if (dragRef.current === null && panRef.current === null) setHover(null)
          }}
          onDoubleClick={(event) => {
            event.preventDefault()
            onRangeChange(null)
          }}
          onContextMenu={(event) => {
            event.preventDefault()
          }}
        >
          {showsEarlierBoundary && (
            <EarlierHistoryBoundary
              loading={olderBusy}
              onHover={() => { setHover(null) }}
              onLoad={loadEarlier}
            />
          )}
          {hover !== null && hover.recordIndex === null && draft === null && (
            <div
              className={css.hoverLine}
              data-timeline-hover-line
              aria-hidden="true"
              style={{
                '--trajectory-hover-left': `${hover.fraction * 100}%`,
              } as CSSProperties}
            />
          )}
          {visibleRange !== null && (
            <>
              <div
                className={css.selection}
                data-dragging={draft === null ? undefined : 'true'}
                aria-hidden="true"
                style={{
                  '--trajectory-selection-left': `${visibleRange.start * 100}%`,
                  '--trajectory-selection-width': `${(visibleRange.end - visibleRange.start) * 100}%`,
                } as CSSProperties}
              />
              <div
                className={css.selectionEdges}
                data-dragging={draft === null ? undefined : 'true'}
                aria-hidden="true"
                style={{
                  '--trajectory-selection-left': `${visibleRange.start * 100}%`,
                  '--trajectory-selection-width': `${(visibleRange.end - visibleRange.start) * 100}%`,
                } as CSSProperties}
              />
            </>
          )}
          <div
            className={css.turnBoundaries}
            data-animate-viewport={animateViewport || undefined}
            aria-hidden="true"
          >
            {model.turnBoundaries
              .filter(boundary => boundary.time > model.start)
              .map(boundary => (
                <span
                  className={css.turnBoundary}
                  data-turn={boundary.turn}
                  key={boundary.turn}
                  style={{
                    '--trajectory-turn-left':
                      `${(boundary.time - model.start) / fullDuration * 100}%`,
                  } as CSSProperties}
                />
              ))}
          </div>
          <div
            className={css.lanes}
            data-animate-viewport={animateViewport || undefined}
            data-timeline-domain
          >
            {model.spans.map((span) => {
                const left = (span.start - model.start) / fullDuration
                const width = (span.end - span.start) / fullDuration
                const widthPercent = width * 100
                const detail = detailByIndex.get(span.index)
                const ttftMs = detail?.ttftMs
                const decodingMs = detail?.decodingMs
                const ttftFraction = ttftMs === undefined
                  || decodingMs === undefined
                  || ttftMs + decodingMs <= 0
                  ? null
                  : ttftMs / (ttftMs + decodingMs)
                const splitFraction = mode === 'tokens'
                  ? span.splitFraction ?? null
                  : ttftFraction
                return (
                  <Tooltip
                    key={spanKey(span)}
                    label={() => timelineTooltipLabel(span.kind, detail, span.segment)}
                    side="bottom"
                    delayMs={TIMELINE_TOOLTIP_DELAY_MS}
                  >
                    <span
                      aria-hidden="true"
                      className={css.span}
                      data-timeline-span={span.kind}
                      data-timeline-record-index={span.index}
                      data-timeline-span-key={spanKey(span)}
                      data-timeline-segment={span.segment}
                      data-span-split={splitFraction === null ? undefined : 'true'}
                      data-error={span.isError || undefined}
                      data-equal-duration={mode === 'time' || undefined}
                      data-current={spanCovers(span, selectedIndex) || undefined}
                      data-hovered={hover?.recordIndex === span.index || undefined}
                      data-search-match={searchMatchIndexes === null
                        ? undefined
                        : searchMatchIndexes.has(span.index) ? 'true' : 'false'}
                      data-selected={activeRange === null
                        ? undefined
                        : span.start <= activeRange.end && span.end >= activeRange.start
                          ? 'true'
                          : 'false'}
                      style={{
                        '--trajectory-span-left': `${left * 100}%`,
                        '--trajectory-span-width': `${widthPercent}%`,
                        '--trajectory-span-gap': `min(${widthPercent * 0.08}%, 1px)`,
                        '--trajectory-span-lane': span.lane,
                        ...(splitFraction === null
                          ? {}
                          : { '--trajectory-span-split': `${splitFraction * 100}%` }),
                      } as CSSProperties}
                    />
                  </Tooltip>
                )
              })}
          </div>
        </div>
      </div>
    </section>
  )
})

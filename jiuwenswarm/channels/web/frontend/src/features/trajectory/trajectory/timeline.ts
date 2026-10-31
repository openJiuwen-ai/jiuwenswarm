/**
 * Operation-sequence and recorded-time projections for the trajectory overview.
 * Adapted mechanically from `packages/client/ui-trajectory/src/client/timeline.ts`
 * under the repository MIT license.
 */

import type { TrajectoryTurnModel } from './model.ts'
import { formatDurationMillis, liveElapsedSeconds } from './record.ts'
import type { TrajectoryCellKind, TrajectoryCellProps } from './record.ts'

/** Horizontal projection used by the trajectory timeline. */
export type TrajectoryTimelineMode = 'sequence' | 'duration' | 'time' | 'actual' | 'tokens'

/** Which half of one request's token spend a block represents. */
export type TrajectoryTimelineSegment = 'input' | 'output'

/** Inclusive selection in the active timeline projection's domain. */
export interface TrajectoryTimeRange {
  start: number
  end: number
}

/** One ledger record projected into the active timeline domain. */
export interface TrajectoryTimelineSpan extends TrajectoryTimeRange {
  index: number
  isError: boolean
  kind: TrajectoryCellKind
  label: string
  lane: number
  /** Token-spend half represented by this block; absent outside the token projection. */
  segment?: TrajectoryTimelineSegment
  /**
   * Leading-segment share in `[0, 1]` used by the two-tone block gradient:
   * cache-written tokens for an Input block, reasoning for an Output block.
   */
  splitFraction?: number
  /**
   * Ledger records this block accounts for, which for an Input block are the
   * records consumed by request `index` rather than the request itself.
   * Absent when the block accounts for `index` alone.
   */
  coveredIndexes?: readonly number[]
}

/** One turn boundary in the active timeline domain. */
export interface TrajectoryTimelineTurnBoundary {
  turn: number
  time: number
}

/** Full-domain model used by the overview. */
export interface TrajectoryTimelineModel extends TrajectoryTimeRange {
  spans: readonly TrajectoryTimelineSpan[]
  turnBoundaries: readonly TrajectoryTimelineTurnBoundary[]
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value))
}

function boundedViewport(
  fullRange: TrajectoryTimeRange,
  viewport: TrajectoryTimeRange | null,
): TrajectoryTimeRange {
  const fullDuration = Math.max(0, fullRange.end - fullRange.start)
  if (viewport === null) return { ...fullRange }
  const duration = clamp(viewport.end - viewport.start, 0, fullDuration)
  const start = clamp(viewport.start, fullRange.start, fullRange.end - duration)
  return { start, end: start + duration }
}

/**
 * Zoom a timeline viewport around the pointer without moving its anchored time.
 * @param fullRange - Complete timeline domain.
 * @param viewport - Current viewport, or null for the complete domain.
 * @param anchorFraction - Pointer position inside the viewport, in the range zero to one.
 * @param wheelDelta - Normalized vertical wheel delta; negative values zoom in.
 * @param minimumDuration - Smallest duration allowed for this projection.
 * @returns The next bounded viewport, or null when zoomed fully out.
 */
export function zoomTrajectoryTimelineViewport(
  fullRange: TrajectoryTimeRange,
  viewport: TrajectoryTimeRange | null,
  anchorFraction: number,
  wheelDelta: number,
  minimumDuration: number,
): TrajectoryTimeRange | null {
  const fullDuration = Math.max(0, fullRange.end - fullRange.start)
  if (fullDuration === 0) return null
  const current = boundedViewport(fullRange, viewport)
  const currentDuration = current.end - current.start
  const nextDuration = clamp(
    currentDuration * Math.exp(wheelDelta * 0.0015),
    Math.min(minimumDuration, fullDuration),
    fullDuration,
  )
  if (nextDuration >= fullDuration * 0.999) return null
  const anchor = clamp(anchorFraction, 0, 1)
  const anchorTime = current.start + anchor * currentDuration
  const nextStart = clamp(
    anchorTime - anchor * nextDuration,
    fullRange.start,
    fullRange.end - nextDuration,
  )
  return { start: nextStart, end: nextStart + nextDuration }
}

/**
 * Pan a zoomed timeline viewport by a fraction of its visible width.
 * @param fullRange - Complete timeline domain.
 * @param viewport - Current viewport, or null for the complete domain.
 * @param deltaFraction - Signed horizontal travel in viewport widths.
 * @returns The next bounded viewport, or null when the full domain is visible.
 */
export function panTrajectoryTimelineViewport(
  fullRange: TrajectoryTimeRange,
  viewport: TrajectoryTimeRange | null,
  deltaFraction: number,
): TrajectoryTimeRange | null {
  if (viewport === null) return null
  const current = boundedViewport(fullRange, viewport)
  const duration = current.end - current.start
  const nextStart = clamp(
    current.start + deltaFraction * duration,
    fullRange.start,
    fullRange.end - duration,
  )
  return { start: nextStart, end: nextStart + duration }
}

/**
 * Format a timeline duration as an integer-millisecond label.
 * @param milliseconds - Non-negative duration in milliseconds.
 * @returns Millisecond label with thousands separators.
 */
export function formatTimelineOffset(milliseconds: number): string {
  return formatDurationMillis(milliseconds)
}

function laneFor(kind: TrajectoryCellKind): number {
  if (kind === 'tool' || kind === 'subtool') return 2
  if (kind === 'message' || kind === 'compacted') return 1
  return 0
}

function isModelCell(kind: TrajectoryCellKind): boolean {
  return kind === 'message' || kind === 'compacted'
}

/** Prompt / window rows that the ledger lists before the model reply. */
function isPrepCell(kind: TrajectoryCellKind): boolean {
  return kind === 'system' || kind === 'user' || kind === 'context'
}

/**
 * Request identity used to keep prep rows beside the reply they feed.
 * Prefer the physical request id; fall back to the schema-v2 inference id.
 */
function requestGroupKey(cell: Pick<TrajectoryCellProps, 'requestRecordId' | 'physicalInferenceId'>): string | undefined {
  if (cell.requestRecordId !== undefined && cell.requestRecordId !== '') {
    return `request:${cell.requestRecordId}`
  }
  if (cell.physicalInferenceId !== undefined && cell.physicalInferenceId !== '') {
    return `inference:${cell.physicalInferenceId}`
  }
  return undefined
}

type PrepTimedSpan = TrajectoryTimelineSpan & {
  groupKey?: string
  behaviorOrder?: number
}

/**
 * Context-window commits often record after inference start, so a raw wall-clock
 * projection draws CONTEXT over the ASSISTANT bar. Slide those prep rows back
 * into the open interval after earlier on-time prep (USER/SYSTEM) and before
 * the model reply, matching ledger phase order without rewriting cell.startedAt.
 *
 * Only mutate display coordinates. Callers that feed shared-axis `toWall` brush
 * selection must keep absolute wall-clock spans (see deriveTimedTimeline /
 * alignPrepSpansBeforeModel).
 */
function placePrepSpansBeforeModel(spans: PrepTimedSpan[]): void {
  const messageStartByGroup = new Map<string, number>()
  for (const span of spans) {
    if (span.groupKey === undefined || !isModelCell(span.kind)) continue
    const prior = messageStartByGroup.get(span.groupKey)
    messageStartByGroup.set(
      span.groupKey,
      prior === undefined ? span.start : Math.min(prior, span.start),
    )
  }
  if (messageStartByGroup.size === 0) return

  const prepByGroup = new Map<string, Array<typeof spans[number]>>()
  for (const span of spans) {
    if (span.groupKey === undefined || !isPrepCell(span.kind)) continue
    if (!messageStartByGroup.has(span.groupKey)) continue
    const list = prepByGroup.get(span.groupKey)
    if (list === undefined) prepByGroup.set(span.groupKey, [span])
    else list.push(span)
  }

  for (const [groupKey, prep] of prepByGroup) {
    const messageStart = messageStartByGroup.get(groupKey)
    if (messageStart === undefined) continue
    const onTime: typeof prep = []
    const late: typeof prep = []
    for (const span of prep) {
      if (span.start > messageStart) late.push(span)
      else onTime.push(span)
    }
    if (late.length === 0) continue
    late.sort((left, right) =>
      left.start - right.start
      || (left.behaviorOrder ?? 0) - (right.behaviorOrder ?? 0)
      || left.index - right.index)

    const floor = onTime.length === 0
      ? messageStart
      : Math.max(
        ...onTime.map(span => span.end),
        ...onTime.map(span => span.start),
      )
    const room = messageStart - floor
    if (!(room > 0)) {
      // No open interval before the reply: park as a point on the reply edge
      // instead of extending a duration into the ASSISTANT bar.
      for (const span of late) {
        span.start = messageStart
        span.end = messageStart
      }
      continue
    }

    const totalDuration = late.reduce(
      (sum, span) => sum + Math.max(0, span.end - span.start),
      0,
    )
    if (totalDuration > 0 && totalDuration <= room) {
      let cursor = floor
      for (const span of late) {
        const duration = Math.max(0, span.end - span.start)
        span.start = cursor
        span.end = cursor + duration
        cursor += duration
      }
      continue
    }

    const step = room / (late.length + 1)
    late.forEach((span, index) => {
      const time = floor + step * (index + 1)
      span.start = time
      span.end = time
    })
  }
}

/**
 * Resolve the active projection from the toolbar's independent switches.
 * @param options - Token view flag plus the recorded-duration and complete-time switches.
 * @returns Projection selected by the toolbar, token cost taking precedence.
 */
export function resolveTimelineMode(options: {
  tokenView: boolean
  actualDuration: boolean
  actualTime: boolean
}): TrajectoryTimelineMode {
  if (options.tokenView) return 'tokens'
  if (options.actualDuration) return options.actualTime ? 'actual' : 'duration'
  return options.actualTime ? 'time' : 'sequence'
}

function finite(value: number | null | undefined): value is number {
  return value !== null && value !== undefined && Number.isFinite(value)
}

function cellRange(
  cell: TrajectoryCellProps,
  nowMilliseconds: number | undefined,
): TrajectoryTimeRange | null {
  if (!finite(cell.startedAt)) return null
  const elapsed = nowMilliseconds === undefined
    ? cell.timeSeconds
    : liveElapsedSeconds(cell, nowMilliseconds)
  const durationMs = finite(elapsed)
    ? Math.max(0, elapsed * 1_000)
    : 0
  return { start: cell.startedAt, end: cell.startedAt + durationMs }
}

/**
 * Project every visible record into a stable three-lane timeline.
 * @param turns - Unfiltered trajectory layout.
 * @param mode - Equal/recorded duration, compressed/complete time, or token-cost projection.
 * @returns Timeline model, or `null` when no record is visible.
 */
export function deriveTrajectoryTimeline(
  turns: readonly TrajectoryTurnModel[],
  mode: TrajectoryTimelineMode = 'sequence',
  nowMilliseconds?: number,
): TrajectoryTimelineModel | null {
  if (mode === 'tokens') return deriveTokenTimeline(turns)
  if (mode !== 'sequence') {
    return deriveTimedTimeline(
      turns,
      mode === 'duration' || mode === 'actual',
      mode === 'duration',
      nowMilliseconds,
    )
  }
  const spans: TrajectoryTimelineSpan[] = []
  const turnBoundaries: TrajectoryTimelineTurnBoundary[] = []

  for (const turn of turns) {
    const cells = turn.groups.flatMap(group =>
      group.cells.filter(cell => cell.requestOnly !== true),
    )
    if (cells.length === 0) continue
    if (turn.turn !== null) {
      turnBoundaries.push({
        turn: turn.turn,
        time: spans.length,
      })
    }
    spans.push(...cells.map((cell, offset): TrajectoryTimelineSpan => ({
      start: spans.length + offset,
      end: spans.length + offset + 1,
      index: cell.index,
      isError: cell.isError === true,
      kind: cell.kind,
      label: cell.text,
      lane: laneFor(cell.kind),
    })))
  }

  if (spans.length === 0) return null
  return {
    start: 0,
    end: spans.length,
    spans,
    turnBoundaries,
  }
}

function tokenCount(value: number | undefined): number {
  return value === undefined || !Number.isFinite(value) ? 0 : Math.max(0, value)
}

/**
 * Project recorded token spend into a cumulative two-lane timeline.
 *
 * Every model request contributes an Input block sized by its cache-missed input
 * and an Output block sized by its produced tokens, laid head to tail so the full
 * domain equals the session's total token spend. Tool, prompt, and context records
 * do not own a block; their cost surfaces inside the Input block of the request
 * that consumed them.
 *
 * @param turns - Unfiltered trajectory layout.
 * @returns Timeline model, or `null` when no record reported usage.
 */
function deriveTokenTimeline(
  turns: readonly TrajectoryTurnModel[],
): TrajectoryTimelineModel | null {
  const spans: TrajectoryTimelineSpan[] = []
  const turnBoundaries: TrajectoryTimelineTurnBoundary[] = []
  let cursor = 0
  let pendingIndexes: number[] = []

  for (const turn of turns) {
    const spansBeforeTurn = spans.length
    const turnStart = cursor
    for (const cell of turn.groups.flatMap(group => group.cells)) {
      if (cell.requestOnly === true) continue
      if (!isModelCell(cell.kind)) {
        pendingIndexes.push(cell.index)
        continue
      }
      const uncachedInput = Math.max(0, tokenCount(cell.input) - tokenCount(cell.cacheRead))
      const output = tokenCount(cell.output)
      const covered = pendingIndexes
      pendingIndexes = []
      if (uncachedInput > 0) {
        // A request whose new input went entirely into the cache reports no
        // plain miss at all, so a full leading share is a real reading.
        const cacheWrite = tokenCount(cell.cacheWrite)
        const splitFraction = cacheWrite > 0
          ? Math.min(1, cacheWrite / uncachedInput)
          : null
        spans.push({
          start: cursor,
          end: cursor + uncachedInput,
          index: cell.index,
          isError: false,
          kind: cell.kind,
          label: cell.text,
          lane: 0,
          segment: 'input',
          coveredIndexes: covered.length === 0 ? [cell.index] : covered,
          ...(splitFraction === null ? {} : { splitFraction }),
        })
        cursor += uncachedInput
      }
      if (output > 0) {
        const think = tokenCount(cell.think)
        const splitFraction = think > 0 ? Math.min(1, think / output) : null
        spans.push({
          start: cursor,
          end: cursor + output,
          index: cell.index,
          isError: cell.isError === true,
          kind: cell.kind,
          label: cell.text,
          lane: 1,
          segment: 'output',
          coveredIndexes: [cell.index],
          ...(splitFraction === null ? {} : { splitFraction }),
        })
        cursor += output
      }
    }
    if (turn.turn !== null && spans.length > spansBeforeTurn) {
      turnBoundaries.push({ turn: turn.turn, time: turnStart })
    }
  }

  if (spans.length === 0) return null
  return {
    start: 0,
    end: cursor,
    spans,
    turnBoundaries,
  }
}

type TimedRawSpan = TrajectoryTimelineSpan & {
  groupKey?: string
  behaviorOrder?: number
}

function deriveTimedTimeline(
  turns: readonly TrajectoryTurnModel[],
  actualDuration: boolean,
  compressIdle: boolean,
  nowMilliseconds: number | undefined,
): TrajectoryTimelineModel | null {
  const timedTurns = turns.flatMap((turn) => {
    const rawSpans = turn.groups.flatMap(group =>
      group.cells.flatMap((cell): TimedRawSpan[] => {
        if (cell.requestOnly === true) return []
        const range = cellRange(cell, nowMilliseconds)
        if (range === null) return []
        const groupKey = requestGroupKey(cell)
        return [{
          ...range,
          index: cell.index,
          isError: cell.isError === true,
          kind: cell.kind,
          label: cell.text,
          lane: laneFor(cell.kind),
          ...(groupKey === undefined ? {} : { groupKey }),
          ...(cell.behaviorOrder === undefined ? {} : { behaviorOrder: cell.behaviorOrder }),
        }]
      }),
    )
    return rawSpans.length === 0 ? [] : [{ turn: turn.turn, rawSpans }]
  })
  const rawSpans = timedTurns.flatMap(turn => turn.rawSpans)
  if (rawSpans.length === 0) return null
  // Duration mode already abandons absolute wall-clock identity via idle
  // compression. Relocating late prep there keeps ledger phase order without
  // desyncing shared-axis toWall selection (analysis browse uses time/actual).
  if (compressIdle) placePrepSpansBeforeModel(rawSpans)

  const removedIdleBySpan = new Map<TimedRawSpan, number>()
  let removedIdle = 0
  let coveredUntil: number | null = null
  for (const span of [...rawSpans].sort((left, right) =>
    left.start - right.start || left.end - right.end)) {
    if (compressIdle && coveredUntil !== null && span.start > coveredUntil) {
      removedIdle += span.start - coveredUntil
    }
    removedIdleBySpan.set(span, removedIdle)
    coveredUntil = coveredUntil === null ? span.end : Math.max(coveredUntil, span.end)
  }

  const spans: TrajectoryTimelineSpan[] = []
  const turnBoundaries: TrajectoryTimelineTurnBoundary[] = []
  for (const turn of timedTurns) {
    const projected = turn.rawSpans.map((span): TrajectoryTimelineSpan => {
      const offset = removedIdleBySpan.get(span) ?? 0
      // Drop only the internal grouping keys; keep segment / splitFraction /
      // coveredIndexes (and any future public fields) for the overview.
      const { groupKey: _groupKey, behaviorOrder: _behaviorOrder, ...publicSpan } = span
      return {
        ...publicSpan,
        start: span.start - offset,
        end: (actualDuration ? span.end : span.start) - offset,
      }
    })
    spans.push(...projected)
    if (turn.turn !== null) {
      turnBoundaries.push({
        turn: turn.turn,
        time: Math.min(...projected.map(span => span.start)),
      })
    }
  }

  return {
    start: Math.min(...spans.map(span => span.start)),
    end: Math.max(...spans.map(span => span.end)),
    spans,
    turnBoundaries,
  }
}

/**
 * Browse-only display realignment for wall-clock projections (`time` / `actual`).
 *
 * Duration mode relocates inside `deriveTimedTimeline` because idle compression
 * already drops absolute wall identity. Wall-clock modes keep recorded span
 * times for shared-axis `toWall` brush selection; the overview may still slide
 * late prep for ledger-phase readability while left-drag is free to pan
 * (`lockViewport === false`). Diagnosis selection locks the viewport and must
 * keep the unrelocated model so brushed wall ranges cover real record times.
 */
export function alignPrepSpansBeforeModel(
  model: TrajectoryTimelineModel | null,
  turns: readonly TrajectoryTurnModel[],
): TrajectoryTimelineModel | null {
  if (model === null || model.spans.length === 0) return model
  const metaByIndex = new Map<number, { groupKey?: string; behaviorOrder?: number }>()
  for (const turn of turns) {
    for (const group of turn.groups) {
      for (const cell of group.cells) {
        if (cell.requestOnly === true) continue
        const groupKey = requestGroupKey(cell)
        metaByIndex.set(cell.index, {
          ...(groupKey === undefined ? {} : { groupKey }),
          ...(cell.behaviorOrder === undefined ? {} : { behaviorOrder: cell.behaviorOrder }),
        })
      }
    }
  }
  const spans: PrepTimedSpan[] = model.spans.map((span) => {
    const meta = metaByIndex.get(span.index)
    return {
      ...span,
      ...(meta?.groupKey === undefined ? {} : { groupKey: meta.groupKey }),
      ...(meta?.behaviorOrder === undefined ? {} : { behaviorOrder: meta.behaviorOrder }),
    }
  })
  placePrepSpansBeforeModel(spans)
  return {
    ...model,
    spans: spans.map((span): TrajectoryTimelineSpan => {
      const { groupKey: _groupKey, behaviorOrder: _behaviorOrder, ...publicSpan } = span
      return publicSpan
    }),
  }
}

/**
 * Place a lane on a shared domain.
 *
 * Without `mapTime`, span coordinates stay absolute and only the domain ends
 * change. With `mapTime`, spans move onto that domain (used to drop idle gaps
 * that no agent occupies) so every lane shares one ruler.
 */
export function withSharedWallClockDomain(
  model: TrajectoryTimelineModel | null,
  domain: TrajectoryTimeRange | null,
  mapTime?: (time: number) => number,
): TrajectoryTimelineModel | null {
  if (model === null || domain === null || !(domain.end > domain.start)) return model
  if (mapTime === undefined) return { ...model, start: domain.start, end: domain.end }
  const map = (time: number) => {
    const mapped = mapTime(time)
    return Number.isFinite(mapped) ? mapped : time
  }
  return {
    ...model,
    start: domain.start,
    end: domain.end,
    spans: model.spans.map((span) => {
      const start = map(span.start)
      const end = map(span.end)
      return { ...span, start: Math.min(start, end), end: Math.max(start, end) }
    }),
    turnBoundaries: model.turnBoundaries.map(boundary => ({
      ...boundary,
      time: map(boundary.time),
    })),
  }
}

/**
 * Identify records active at any point inside an inclusive selected interval.
 * @param turns - Unfiltered trajectory layout.
 * @param range - Selected interval in the active projection.
 * @param mode - Equal/recorded duration, compressed/complete time, or token-cost projection.
 * @returns Record indexes inside the focus interval.
 */
export function trajectoryTimelineFocusIndexes(
  turns: readonly TrajectoryTurnModel[],
  range: TrajectoryTimeRange,
  mode: TrajectoryTimelineMode = 'sequence',
  nowMilliseconds?: number,
): ReadonlySet<number> {
  const model = deriveTrajectoryTimeline(turns, mode, nowMilliseconds)
  return new Set(
    model?.spans
      .filter(span => span.start <= range.end && span.end >= range.start)
      .flatMap(span => span.coveredIndexes ?? [span.index]),
  )
}

/**
 * Shared wall-clock domain and the diagnosis ranges built from it.
 *
 * Free of React and browser APIs so node --test can import it.
 */

export interface WallClockSample {
  startedAt?: number | null;
  timeSeconds?: number | null;
  requestOnly?: boolean;
  status?: 'complete' | 'running' | 'error' | null;
  /** Projected cell kind; used to detect chat user prompts for `user_turn`. */
  kind?: string;
  /** Projected message source; ask_user resumes are not a new user prompt. */
  messageSource?: unknown;
  /** Cell text / input body; team harness envelopes are not a new user prompt. */
  text?: string | null;
  inputDetail?: string | null;
}

export interface WallClockDomain {
  startMs: number;
  endMs: number;
}

/** One stretch of wall-clock time that still contains agent activity. */
export interface CompressedSegment {
  startMs: number;
  endMs: number;
  /** Offset of `startMs` on the axis after earlier idle gaps are removed. */
  compressedStartMs: number;
}

/**
 * Shared axis after idle gaps are removed.
 *
 * A gap counts as idle only when no sample from any agent covers it. The
 * visual domain is the sum of the remaining stretches, starting at 0.
 */
export interface CompressedTimeline {
  durationMs: number;
  segments: CompressedSegment[];
}

export interface DiagnosisSubjectChoice {
  subjectId: string;
  subjectLabel: string;
}

export interface DiagnosisRangeChoice {
  subjectId: string;
  subjectLabel: string;
  startMs: number;
  endMs: number;
}

export interface AxisTick {
  offsetMs: number;
  fraction: number;
  label: string;
}

/** Preferred tick spacing; pick the coarsest step that still yields ≤ ~180 marks. */
const AXIS_STEP_MS = [
  100,
  200,
  500,
  1_000,
  2_000,
  5_000,
  10_000,
  15_000,
  30_000,
  60_000,
  120_000,
  300_000,
  600_000,
  900_000,
  1_800_000,
  3_600_000,
  7_200_000,
];

/** Target tick count across the visible window (~3× denser than the prior ≤60). */
const AXIS_TICK_BUDGET = 180;

const NANOSECONDS_PER_MILLISECOND = 1_000_000n;

/** Minimal OTLP span fields needed to place one record on the diagnosis axis. */
export interface OtelSpanTiming {
  startTimeUnixNano: string;
  endTimeUnixNano?: string;
}

/**
 * One raw trajectory detail record that may carry a single OTLP span.
 *
 * Only the first span of the export request is used (same as the raw inspector).
 */
export interface RawOtelSpanCarrier {
  otlp: {
    resourceSpans?: readonly {
      scopeSpans?: readonly {
        spans?: readonly OtelSpanTiming[];
      }[];
    }[];
  } | null;
}

/** Wall-clock start/end of one OTLP span; missing end collapses to the start. */
export function otelSpanWallInterval(span: OtelSpanTiming): { startMs: number; endMs: number } | null {
  try {
    const startMs = Number(BigInt(span.startTimeUnixNano) / NANOSECONDS_PER_MILLISECOND);
    if (!Number.isFinite(startMs) || startMs < 0) return null;
    const endRaw = span.endTimeUnixNano;
    const endMs = endRaw === undefined || endRaw === ''
      ? startMs
      : Number(BigInt(endRaw) / NANOSECONDS_PER_MILLISECOND);
    if (!Number.isFinite(endMs)) return { startMs, endMs: startMs };
    return { startMs, endMs: Math.max(startMs, endMs) };
  } catch {
    return null;
  }
}

/** Wall-clock interval of the first span on a raw OTel record, or `null` if unusable. */
export function rawOtelSpanInterval(record: RawOtelSpanCarrier): { startMs: number; endMs: number } | null {
  const span = record.otlp?.resourceSpans
    ?.flatMap(resource => resource.scopeSpans ?? [])
    .flatMap(scope => scope.spans ?? [])[0];
  if (span === undefined) return null;
  return otelSpanWallInterval(span);
}

/** Every timed raw OTel span in order; records without a usable span are skipped. */
export function rawOtelSpanIntervals(
  records: readonly RawOtelSpanCarrier[],
): { startMs: number; endMs: number }[] {
  const intervals: { startMs: number; endMs: number }[] = [];
  for (const record of records) {
    const interval = rawOtelSpanInterval(record);
    if (interval !== null) intervals.push(interval);
  }
  return intervals;
}

/**
 * Start and end of one timed node.
 *
 * `requestOnly` rows are not nodes on the lane. A node without a duration is
 * the single instant it started.
 */
export function timedNodeInterval(cell: WallClockSample): { startMs: number; endMs: number } | null {
  if (cell.requestOnly === true) return null;
  const started = cell.startedAt;
  if (started === null || started === undefined || !Number.isFinite(started) || started < 0) return null;
  const seconds = cell.timeSeconds;
  const end = seconds !== null && seconds !== undefined && Number.isFinite(seconds)
    ? started + Math.max(0, seconds) * 1_000
    : started;
  return { startMs: started, endMs: Math.max(started, end) };
}

function isTrajectoryRecord(cell: WallClockSample): boolean {
  if (cell.requestOnly === true) return false;
  const kind = cell.kind;
  return kind === 'user'
    || kind === 'message'
    || kind === 'tool'
    || kind === 'subtool'
    || kind === 'compacted';
}

/**
 * Wall-clock intervals of the trajectory records on one lane.
 *
 * One record is one user, assistant, or tool row. The two context reminders
 * beside a user message are not records. A record without a start time is
 * omitted, matching cells the swimlane cannot select.
 */
export function trajectoryRecordIntervals(
  turns: readonly { groups: readonly { cells: readonly WallClockSample[] }[] }[],
): { startMs: number; endMs: number }[] {
  const intervals: { startMs: number; endMs: number }[] = [];
  for (const turn of turns) {
    for (const group of turn.groups) {
      for (const cell of group.cells) {
        if (!isTrajectoryRecord(cell)) continue;
        const interval = timedNodeInterval(cell);
        if (interval !== null) intervals.push(interval);
      }
    }
  }
  return intervals;
}

/**
 * Trajectory records shown when a lane's ledger is fully expanded.
 *
 * Same set as `trajectoryRecordIntervals`. System prompts, context reminders,
 * and request-only markers stay in the ledger but are not nodes. Raw OTel
 * spans are a separate total.
 */
export function trajectoryRecordCount(
  turns: readonly { groups: readonly { cells: readonly WallClockSample[] }[] }[],
): number {
  return trajectoryRecordIntervals(turns).length;
}

/** Every timed node on a lane, in snapshot order. */
export function timedNodeIntervals(
  turns: readonly { groups: readonly { cells: readonly WallClockSample[] }[] }[],
  kinds?: readonly string[],
): { startMs: number; endMs: number }[] {
  const intervals: { startMs: number; endMs: number }[] = [];
  for (const turn of turns) {
    for (const group of turn.groups) {
      for (const cell of group.cells) {
        if (kinds !== undefined && !kinds.includes((cell as { kind?: string }).kind ?? '')) continue;
        const interval = timedNodeInterval(cell);
        if (interval !== null) intervals.push(interval);
      }
    }
  }
  return intervals;
}

/**
 * Start and end of the main agent's last reply node.
 *
 * Only assistant reply cells (`kind: 'message'`) participate, mirroring the
 * backend delivery node: the leader's last assistant reply, ordered by start
 * and then end (`task_parse.py DeliveryRound.delivery_nodes`). User, system
 * and context cells carry context-window commit replay times that can land
 * after the reply's inference start, so they never claim "last".
 */
export function lastTimedNodeInterval(
  turns: readonly { groups: readonly { cells: readonly (WallClockSample & { kind?: string })[] }[] }[],
): { startMs: number; endMs: number } | null {
  let found: { startMs: number; endMs: number } | null = null;
  for (const interval of timedNodeIntervals(turns, ['message'])) {
    if (found === null) {
      found = interval;
      continue;
    }
    if (interval.startMs < found.startMs) continue;
    if (interval.startMs === found.startMs && interval.endMs <= found.endMs) continue;
    found = interval;
  }
  return found;
}

/** The main agent's last node, or the team leader's when the session has no main agent. */
export function mainAgentLastNode(
  lanes: readonly {
    subjectId: string;
    kind: string;
    turns: readonly { groups: readonly { cells: readonly WallClockSample[] }[] }[];
  }[],
): { subjectId: string; startMs: number; endMs: number } | null {
  const lane = lanes.find(item => item.kind === 'main_agent')
    ?? lanes.find(item => item.kind === 'team_leader');
  if (lane === undefined) return null;
  const interval = lastTimedNodeInterval(lane.turns);
  return interval === null ? null : { subjectId: lane.subjectId, ...interval };
}

/** Flatten turn groups into the samples the shared axis measures. */
export function collectWallClockSamples(
  turns: readonly { groups: readonly { cells: readonly WallClockSample[] }[] }[],
): WallClockSample[] {
  return turns.flatMap(turn => turn.groups.flatMap(group => group.cells));
}

/** Wall-clock start of one numbered turn (earliest timed cell in that turn). */
export interface TurnWallStart {
  turn: number;
  wallMs: number;
}

/** Wall-clock span of one numbered turn (earliest start … latest end). */
export interface TurnWallRange {
  turn: number;
  startWallMs: number;
  endWallMs: number;
}

/**
 * Earliest wall-clock start per numbered turn, for shared-axis turn markers.
 * Unnumbered / empty turns are skipped. Order follows the turns array.
 */
export function collectTurnWallStarts(
  turns: readonly {
    turn: number | null;
    groups: readonly { cells: readonly WallClockSample[] }[];
  }[],
): TurnWallStart[] {
  return collectTurnWallRanges(turns).map(range => ({
    turn: range.turn,
    wallMs: range.startWallMs,
  }));
}

/**
 * Earliest start and latest end per numbered turn (request-only cells skipped).
 * Used for shared-axis start/end marks and clock labels.
 */
export function collectTurnWallRanges(
  turns: readonly {
    turn: number | null;
    groups: readonly { cells: readonly WallClockSample[] }[];
  }[],
  nowMs: number = Date.now(),
): TurnWallRange[] {
  const ranges: TurnWallRange[] = [];
  for (const turn of turns) {
    if (turn.turn === null) continue;
    let startWallMs: number | null = null;
    let endWallMs: number | null = null;
    for (const group of turn.groups) {
      for (const cell of group.cells) {
        const interval = sampleInterval(cell, nowMs);
        if (interval === null) continue;
        startWallMs = startWallMs === null
          ? interval.startMs
          : Math.min(startWallMs, interval.startMs);
        endWallMs = endWallMs === null
          ? interval.endMs
          : Math.max(endWallMs, interval.endMs);
      }
    }
    if (startWallMs !== null && endWallMs !== null) {
      ranges.push({ turn: turn.turn, startWallMs, endWallMs });
    }
  }
  return ranges;
}

/** Team harness envelopes injected as USER rows are not a chat prompt. */
const TEAM_HARNESS_USER_MARK = /<team-(?:clock|event|inbound)\b/i;

type NumberedTurnSamples = {
  turn: number | null;
  groups: readonly { cells: readonly WallClockSample[] }[];
};

/**
 * Team harness / internal envelopes that are not real agent work.
 *
 * Dropped from shared-axis compression samples and from `user_turn` wall
 * extents so overnight idle can collapse like a single-Agent session, and so
 * gap clocks follow real work rather than `team-clock` heartbeats.
 */
export function isTeamHarnessNoiseCell(cell: WallClockSample): boolean {
  const source = cell.messageSource;
  if (source !== null && typeof source === 'object' && !Array.isArray(source)) {
    const origin = (source as { origin?: unknown }).origin;
    if (origin === 'harness_internal') return true;
  }
  if (cell.kind !== 'user') return false;
  const text = `${cell.text ?? ''}\n${cell.inputDetail ?? ''}`;
  return TEAM_HARNESS_USER_MARK.test(text);
}

/**
 * True when the cell is an external chat user prompt.
 *
 * `ask_user` resumes and team harness USER rows (`team-clock` / `team-event` /
 * `team-inbound`) do not open a new `user_turn`. Projector `turn` numbers are
 * unchanged by this check.
 */
export function isExternalUserPromptCell(cell: WallClockSample): boolean {
  if (cell.kind !== 'user') return false;
  if (isTeamHarnessNoiseCell(cell)) return false;
  const source = cell.messageSource;
  if (source !== null && typeof source === 'object' && !Array.isArray(source)) {
    const kind = (source as { kind?: unknown }).kind;
    if (kind === 'trajectory_ask_user') return false;
  }
  return true;
}

function turnHasExternalUserPrompt(turn: NumberedTurnSamples): boolean {
  for (const group of turn.groups) {
    for (const cell of group.cells) {
      if (isExternalUserPromptCell(cell)) return true;
    }
  }
  return false;
}

/** Wall span of one user-interaction turn (`user_turn`). */
export interface UserTurnWallRange {
  userTurn: number;
  startWallMs: number;
  endWallMs: number;
}

/**
 * Wall ranges for analysis gap chrome: one range per chat user prompt
 * (`user_turn`), not per internal ReAct / member `turn` number.
 *
 * A new `user_turn` starts on a numbered turn that carries an external user
 * prompt; following numbered turns without such a prompt stay in the same
 * range until the next prompt. Does not change projector turn numbering.
 */
export function collectUserInteractionWallRanges(
  turns: readonly NumberedTurnSamples[],
  nowMs: number = Date.now(),
): UserTurnWallRange[] {
  const ranges: UserTurnWallRange[] = [];
  let current: UserTurnWallRange | null = null;
  let userTurn = 0;
  for (const turn of turns) {
    if (turn.turn === null) continue;
    let startWallMs: number | null = null;
    let endWallMs: number | null = null;
    const hasUserPrompt = turnHasExternalUserPrompt(turn);
    for (const group of turn.groups) {
      for (const cell of group.cells) {
        if (isTeamHarnessNoiseCell(cell)) continue;
        const interval = sampleInterval(cell, nowMs);
        if (interval === null) continue;
        startWallMs = startWallMs === null
          ? interval.startMs
          : Math.min(startWallMs, interval.startMs);
        endWallMs = endWallMs === null
          ? interval.endMs
          : Math.max(endWallMs, interval.endMs);
      }
    }
    if (startWallMs === null || endWallMs === null) continue;
    if (current === null || hasUserPrompt) {
      if (current !== null) ranges.push(current);
      userTurn += 1;
      current = { userTurn, startWallMs, endWallMs };
      continue;
    }
    current = {
      ...current,
      endWallMs: Math.max(current.endWallMs, endWallMs),
    };
  }
  if (current !== null) ranges.push(current);
  return ranges;
}

/**
 * One turn range already projected onto the shared axis, with its real wall times.
 *
 * Analysis gap markers pass `user_turn` in `turn`; selection snap uses the same
 * ranges. Projector model-turn numbering is separate.
 */
export interface SharedTurnAxisRange {
  turn: number;
  startAxisMs: number;
  endAxisMs: number;
  startWallMs: number;
  endWallMs: number;
}

/**
 * Map a shared-axis pick to wall clock for selection labels / diagnosis scope.
 *
 * Compressed `toWall` collapses a segment boundary onto the next stretch's start,
 * so turn-end labels must use the stored wall end instead. Picks that land in the
 * visual gap between turns snap to the next turn's start (selection start) or the
 * previous turn's end (selection end).
 */
export function resolveSelectionWallMs(input: {
  axisMs: number;
  role: 'start' | 'end';
  turns: readonly SharedTurnAxisRange[];
  toWall: (axisMs: number) => number;
  /** Half the visual turn gap, in shared-axis units. */
  gapHalfAxisMs: number;
}): number {
  const turns = [...input.turns].sort((left, right) => (
    left.startAxisMs - right.startAxisMs || left.turn - right.turn
  ));
  if (turns.length === 0) return input.toWall(input.axisMs);
  const gapHalf = Math.max(0, input.gapHalfAxisMs);
  for (let index = 0; index < turns.length - 1; index += 1) {
    const prev = turns[index];
    const next = turns[index + 1];
    if (prev === undefined || next === undefined) continue;
    const axisGap = next.startAxisMs - prev.endAxisMs;
    const abuts = axisGap <= Math.max(gapHalf, 1e-6);
    const inGap = abuts
      ? (() => {
        const junction = (prev.endAxisMs + next.startAxisMs) / 2;
        return input.axisMs >= junction - gapHalf && input.axisMs <= junction + gapHalf;
      })()
      : input.axisMs > prev.endAxisMs && input.axisMs < next.startAxisMs;
    if (inGap) {
      return input.role === 'start' ? next.startWallMs : prev.endWallMs;
    }
  }
  // Prefer recorded wall times on the projected turn edges (toWall collapses
  // segment ends onto the next stretch's start).
  const matches: Array<{ wallMs: number; distance: number; edge: 'start' | 'end' }> = [];
  for (const turn of turns) {
    for (const edge of [
      { edge: 'start' as const, axisMs: turn.startAxisMs, wallMs: turn.startWallMs },
      { edge: 'end' as const, axisMs: turn.endAxisMs, wallMs: turn.endWallMs },
    ]) {
      const distance = Math.abs(input.axisMs - edge.axisMs);
      if (distance > Math.max(gapHalf, 1e-6)) continue;
      matches.push({ wallMs: edge.wallMs, distance, edge: edge.edge });
    }
  }
  if (matches.length > 0) {
    matches.sort((left, right) => left.distance - right.distance);
    const bestDistance = matches[0]?.distance ?? 0;
    const closest = matches.filter(item => item.distance === bestDistance);
    const preferred = closest.find(item => item.edge === input.role) ?? closest[0];
    if (preferred !== undefined) return preferred.wallMs;
  }
  return input.toWall(input.axisMs);
}

function sampleInterval(
  sample: WallClockSample,
  nowMs: number,
): { startMs: number; endMs: number } | null {
  if (sample.requestOnly === true) return null;
  const started = sample.startedAt;
  if (started === null || started === undefined || !Number.isFinite(started) || started < 0) return null;
  const seconds = sample.timeSeconds;
  let end = started;
  if (seconds !== null && seconds !== undefined && Number.isFinite(seconds)) {
    end = started + Math.max(0, seconds) * 1_000;
  } else if (sample.status === 'running' && Number.isFinite(nowMs)) {
    // Only a record that is still running reaches the current clock.
    end = Math.max(started, nowMs);
  }
  return { startMs: started, endMs: Math.max(started, end) };
}

function mergeIntervals(
  intervals: readonly { startMs: number; endMs: number }[],
): { startMs: number; endMs: number }[] {
  const sorted = [...intervals].sort((left, right) => left.startMs - right.startMs || left.endMs - right.endMs);
  const merged: { startMs: number; endMs: number }[] = [];
  for (const interval of sorted) {
    const last = merged[merged.length - 1];
    if (last === undefined || interval.startMs > last.endMs) {
      merged.push({ startMs: interval.startMs, endMs: interval.endMs });
    } else if (interval.endMs > last.endMs) {
      last.endMs = interval.endMs;
    }
  }
  return merged;
}

/**
 * Drop wall-clock gaps that no agent occupies, and keep the active stretches.
 *
 * A finished sample with no duration is a point. It does not run forward to
 * `nowMs`; only `status: 'running'` does.
 */
export function compressSharedTimeline(
  samples: readonly WallClockSample[],
  nowMs: number,
): CompressedTimeline | null {
  const intervals = [];
  for (const sample of samples) {
    const interval = sampleInterval(sample, nowMs);
    if (interval !== null) intervals.push(interval);
  }
  const merged = mergeIntervals(intervals);
  if (merged.length === 0) return null;
  let cursor = 0;
  const segments = merged.map((interval) => {
    const compressedStartMs = cursor;
    cursor += interval.endMs - interval.startMs;
    return { startMs: interval.startMs, endMs: interval.endMs, compressedStartMs };
  });
  return { durationMs: cursor <= 0 ? 1 : cursor, segments };
}

/** Map a wall-clock instant onto the compressed axis. Gaps snap to the next stretch. */
export function wallToCompressed(timeline: CompressedTimeline, wallMs: number): number {
  const first = timeline.segments[0];
  if (first === undefined || wallMs <= first.startMs) return 0;
  for (const segment of timeline.segments) {
    if (wallMs <= segment.endMs) {
      if (wallMs < segment.startMs) return segment.compressedStartMs;
      return segment.compressedStartMs + (wallMs - segment.startMs);
    }
  }
  return timeline.durationMs;
}

/** Map a compressed-axis instant back to wall clock, for the diagnosis request. */
export function compressedToWall(timeline: CompressedTimeline, compressedMs: number): number {
  const first = timeline.segments[0];
  const last = timeline.segments[timeline.segments.length - 1];
  if (first === undefined || last === undefined || compressedMs <= 0) return first?.startMs ?? compressedMs;
  for (let index = 0; index < timeline.segments.length; index += 1) {
    const segment = timeline.segments[index];
    if (segment === undefined) continue;
    const length = segment.endMs - segment.startMs;
    if (length <= 0) continue;
    const compressedEnd = segment.compressedStartMs + length;
    const isLast = index === timeline.segments.length - 1;
    // A shared boundary is the start of the next active stretch, not the idle gap before it.
    if (compressedMs < compressedEnd || (isLast && compressedMs <= compressedEnd)) {
      return segment.startMs + (compressedMs - segment.compressedStartMs);
    }
  }
  return last.endMs;
}

/**
 * Earliest start and latest end across samples, in Unix milliseconds.
 *
 * Finished samples stay at their recorded end. A zero-width domain is widened
 * by one millisecond so a fraction along the axis is defined.
 */
export function sharedWallClockDomain(
  samples: readonly WallClockSample[],
  nowMs: number,
): WallClockDomain | null {
  const timeline = compressSharedTimeline(samples, nowMs);
  const first = timeline?.segments[0];
  const last = timeline?.segments[timeline.segments.length - 1];
  if (timeline === undefined || timeline === null || first === undefined || last === undefined) return null;
  const endMs = last.endMs <= first.startMs ? first.startMs + 1 : last.endMs;
  return { startMs: first.startMs, endMs };
}

/**
 * One identical window for every subject the user left checked.
 *
 * An empty exclusion set keeps every subject. No window yields no ranges.
 */
export function diagnosisSelectionRanges(input: {
  range: { startMs: number; endMs: number } | null;
  subjects: readonly DiagnosisSubjectChoice[];
  excludedSubjectIds: ReadonlySet<string>;
}): DiagnosisRangeChoice[] {
  const range = input.range;
  if (range === null) return [];
  if (!Number.isFinite(range.startMs) || !Number.isFinite(range.endMs) || range.endMs < range.startMs) {
    return [];
  }
  return input.subjects
    .filter(subject => !input.excludedSubjectIds.has(subject.subjectId))
    .map(subject => ({
      subjectId: subject.subjectId,
      subjectLabel: subject.subjectLabel,
      startMs: range.startMs,
      endMs: range.endMs,
    }));
}

/** Elapsed time as `m:ss` or `h:mm:ss`, measured from the axis origin. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1_000));
  const hours = Math.floor(total / 3_600);
  const minutes = Math.floor((total % 3_600) / 60);
  const seconds = total % 60;
  const ss = String(seconds).padStart(2, '0');
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${ss}`;
  return `${minutes}:${ss}`;
}

/**
 * Visible blank between one `user_turn` end and the next start, as a fraction
 * of the currently visible shared-axis width. Swimlane pads and axis marks
 * stay in sync via `placeUserTurnAxisEdges`.
 */
export const USER_TURN_GAP_FRACTION = 0.02;

/** Half the proportional user_turn gap, in the same units as the visible domain. */
export function userTurnGapHalfAxisMs(visibleDurationMs: number): number {
  if (!(visibleDurationMs > 0) || !Number.isFinite(visibleDurationMs)) return 0;
  return visibleDurationMs * (USER_TURN_GAP_FRACTION / 2);
}

/** One start/end edge of a `user_turn` after the visible junction gap is applied. */
export interface UserTurnAxisEdge {
  turn: number;
  edge: 'start' | 'end';
  /** Original compressed-axis position before the junction gap. */
  axisMs: number;
  /** Draw position after enforcing the gap to the neighboring user_turn. */
  displayAxisMs: number;
  wallMs: number;
}

/**
 * Place every `user_turn` start/end on the axis and force a visible gap between
 * consecutive turns when compression (or raw timing) would leave less than
 * `USER_TURN_GAP_FRACTION` of the visible width between them.
 */
export function placeUserTurnAxisEdges(
  ranges: readonly SharedTurnAxisRange[],
  visibleDurationMs: number,
): UserTurnAxisEdge[] {
  const gapHalf = userTurnGapHalfAxisMs(visibleDurationMs);
  const minGap = gapHalf * 2;
  const edges: UserTurnAxisEdge[] = [];
  for (const range of ranges) {
    edges.push({
      turn: range.turn,
      edge: 'start',
      axisMs: range.startAxisMs,
      displayAxisMs: range.startAxisMs,
      wallMs: range.startWallMs,
    });
    edges.push({
      turn: range.turn,
      edge: 'end',
      axisMs: range.endAxisMs,
      displayAxisMs: range.endAxisMs,
      wallMs: range.endWallMs,
    });
  }
  const ordered = [...ranges].sort((left, right) => (
    left.startAxisMs - right.startAxisMs
    || left.turn - right.turn
  ));
  for (let index = 0; index < ordered.length - 1; index += 1) {
    const current = ordered[index];
    const next = ordered[index + 1];
    if (current === undefined || next === undefined) continue;
    const endEdge = edges.find(edge => edge.turn === current.turn && edge.edge === 'end');
    const startEdge = edges.find(edge => edge.turn === next.turn && edge.edge === 'start');
    if (endEdge === undefined || startEdge === undefined) continue;
    const span = startEdge.axisMs - endEdge.axisMs;
    if (span >= minGap) continue;
    const mid = (endEdge.axisMs + startEdge.axisMs) / 2;
    endEdge.displayAxisMs = mid - gapHalf;
    startEdge.displayAxisMs = mid + gapHalf;
  }
  return edges;
}

/** Axis interval used when clipping swimlane spans against display blanks. */
export interface AxisInterval {
  startMs: number;
  endMs: number;
}

/**
 * Blank intervals between consecutive `user_turn` display edges.
 *
 * Covers forced 2% junctions and wider natural blanks so a long tool/model
 * span cannot paint through the gap chrome.
 */
export function userTurnDisplayGaps(
  edges: readonly UserTurnAxisEdge[],
): AxisInterval[] {
  const startByTurn = new Map<number, UserTurnAxisEdge>();
  const endByTurn = new Map<number, UserTurnAxisEdge>();
  for (const edge of edges) {
    if (edge.edge === 'start') startByTurn.set(edge.turn, edge);
    else endByTurn.set(edge.turn, edge);
  }
  const turns = [...endByTurn.keys()].sort((left, right) => {
    const leftStart = startByTurn.get(left)?.axisMs ?? 0;
    const rightStart = startByTurn.get(right)?.axisMs ?? 0;
    return leftStart - rightStart || left - right;
  });
  const gaps: AxisInterval[] = [];
  for (let index = 0; index < turns.length - 1; index += 1) {
    const currentTurn = turns[index];
    const nextTurn = turns[index + 1];
    if (currentTurn === undefined || nextTurn === undefined) continue;
    const end = endByTurn.get(currentTurn);
    const start = startByTurn.get(nextTurn);
    if (end === undefined || start === undefined) continue;
    if (start.displayAxisMs > end.displayAxisMs) {
      gaps.push({ startMs: end.displayAxisMs, endMs: start.displayAxisMs });
    }
  }
  return gaps;
}

/**
 * Subtract `user_turn` display gaps from a span.
 *
 * Spans wholly inside a gap disappear; spans that cross a gap split into one
 * piece on each side (call site keeps the same record identity). A zero-duration
 * mark stays, unless that instant lies strictly inside a gap. Input-lane rows
 * are recorded as instants and still need the mark.
 */
export function clipIntervalAgainstGaps(
  startMs: number,
  endMs: number,
  gaps: readonly AxisInterval[],
): AxisInterval[] {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) {
    return [];
  }
  let pieces: AxisInterval[] = [{ startMs, endMs }];
  for (const gap of gaps) {
    if (!(gap.endMs > gap.startMs)) continue;
    const next: AxisInterval[] = [];
    for (const piece of pieces) {
      if (piece.endMs <= gap.startMs || piece.startMs >= gap.endMs) {
        next.push(piece);
        continue;
      }
      if (piece.endMs === piece.startMs) continue;
      if (piece.startMs < gap.startMs) {
        next.push({ startMs: piece.startMs, endMs: gap.startMs });
      }
      if (piece.endMs > gap.endMs) {
        next.push({ startMs: gap.endMs, endMs: piece.endMs });
      }
    }
    pieces = next;
  }
  return pieces.filter(piece => piece.endMs >= piece.startMs);
}

/** Wall-clock instant as local `HH:mm:ss`, or an empty string when invalid. */
export function formatAxisClock(ms: number): string {
  if (!Number.isFinite(ms)) return '';
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * Wall-clock label for `user_turn` start/end marks.
 *
 * Date (month-day, no year) and time sit on separate lines so compressed
 * cross-day junctions stay readable without a long single-line string.
 */
export function formatUserTurnClock(ms: number): string {
  if (!Number.isFinite(ms)) return '';
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())}\n`
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * Keep a shared zoom window inside the compressed domain.
 *
 * A window that already shows essentially the whole domain collapses back to
 * the full view (`null`). An in-range window is returned unchanged.
 */
export function clampSharedViewport(
  viewport: { start: number; end: number } | null,
  domainStart: number,
  domainEnd: number,
): { start: number; end: number } | null {
  if (viewport === null || !(domainEnd > domainStart)) return null;
  const full = domainEnd - domainStart;
  const duration = viewport.end - viewport.start;
  if (!Number.isFinite(duration) || duration <= 0 || duration >= full * 0.999) return null;
  const bounded = Math.min(duration, full);
  const start = Math.min(Math.max(viewport.start, domainStart), domainEnd - bounded);
  const end = start + bounded;
  if (start === viewport.start && end === viewport.end) return viewport;
  return { start, end };
}

/** Tick marks across one visible window. Labels stay absolute from the axis origin. */
export function visibleAxisTicks(startMs: number, endMs: number): AxisTick[] {
  const duration = endMs - startMs;
  return axisTicks(duration).map(tick => ({
    offsetMs: startMs + tick.offsetMs,
    fraction: tick.fraction,
    label: formatElapsed(startMs + tick.offsetMs),
  }));
}

/** Tick marks from 0:00 through the end of `durationMs`. */
export function axisTicks(durationMs: number): AxisTick[] {
  if (!Number.isFinite(durationMs) || durationMs <= 0) {
    return [{ offsetMs: 0, fraction: 0, label: '0:00' }];
  }
  const step = AXIS_STEP_MS.find(item => durationMs / item <= AXIS_TICK_BUDGET)
    ?? AXIS_STEP_MS[AXIS_STEP_MS.length - 1];
  const ticks: AxisTick[] = [];
  for (let offset = 0; offset < durationMs; offset += step) {
    ticks.push({ offsetMs: offset, fraction: offset / durationMs, label: formatElapsed(offset) });
  }
  ticks.push({ offsetMs: durationMs, fraction: 1, label: formatElapsed(durationMs) });
  if (ticks.length >= 2) {
    const last = ticks[ticks.length - 1];
    const previous = ticks[ticks.length - 2];
    const minGap = (step / durationMs) * 0.55;
    if (
      last !== undefined
      && previous !== undefined
      && previous.offsetMs !== 0
      && last.fraction - previous.fraction < minGap
    ) {
      ticks.splice(ticks.length - 2, 1);
    }
  }
  return ticks;
}

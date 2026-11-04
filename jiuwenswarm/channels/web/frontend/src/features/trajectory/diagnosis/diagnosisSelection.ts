/**
 * What the user has picked to diagnose, and the request ranges it becomes.
 *
 * Two ways to pick, never both at once: one window on the shared time axis
 * (applied to every agent left checked), or a window drawn on a single agent's
 * lane (kept per agent). Ranges are wall-clock Unix milliseconds.
 *
 * Kept free of React and browser APIs so it can be tested with node --test.
 */

import { msRangeToUnixNano } from './diagnosisState';
import type { DiagnosisScope, SelectionRange } from './diagnosisTypes';
import {
  diagnosisSelectionRanges,
  type DiagnosisRangeChoice,
  type DiagnosisSubjectChoice,
} from './diagnosisTimeSelection';

export interface WallRange {
  startMs: number;
  endMs: number;
}

export interface DiagnosisSelectionState {
  /** `axis`: one window for all agents; `lanes`: a window per agent. */
  mode: 'none' | 'axis' | 'lanes';
  axisRange: WallRange | null;
  laneRanges: ReadonlyMap<string, WallRange>;
  /** Agents unchecked while picking on the time axis. */
  excluded: ReadonlySet<string>;
}

const NO_LANES: ReadonlyMap<string, WallRange> = new Map();
const NO_AGENTS: ReadonlySet<string> = new Set();

export const EMPTY_DIAGNOSIS_SELECTION: DiagnosisSelectionState = {
  mode: 'none',
  axisRange: null,
  laneRanges: NO_LANES,
  excluded: NO_AGENTS,
};

function validRange(range: WallRange | null): range is WallRange {
  return range !== null
    && Number.isFinite(range.startMs)
    && Number.isFinite(range.endMs)
    && range.endMs >= range.startMs;
}

/** Pick (or with `null` clear) the shared window. Per-agent windows are dropped. */
export function selectOnAxis(state: DiagnosisSelectionState, range: WallRange | null): DiagnosisSelectionState {
  if (!validRange(range)) return { ...EMPTY_DIAGNOSIS_SELECTION, excluded: state.excluded };
  return { mode: 'axis', axisRange: range, laneRanges: NO_LANES, excluded: state.excluded };
}

/**
 * Pick (or with `null` clear) one agent's own window.
 *
 * The first lane pick drops the shared window, and the agent ticks stop
 * meaning "excluded": from then on only agents with a window count.
 */
export function selectOnLane(
  state: DiagnosisSelectionState,
  subjectId: string,
  range: WallRange | null,
): DiagnosisSelectionState {
  const next = new Map(state.mode === 'lanes' ? state.laneRanges : NO_LANES);
  if (validRange(range)) next.set(subjectId, range);
  else next.delete(subjectId);
  if (next.size === 0) return { ...EMPTY_DIAGNOSIS_SELECTION, excluded: state.excluded };
  return { mode: 'lanes', axisRange: null, laneRanges: next, excluded: state.excluded };
}

/**
 * One window covering every interval of an agent (earliest start to latest end).
 *
 * Empty or invalid input yields `null`. Used by the per-agent "select all"
 * control so a click picks the whole lane without dragging.
 */
export function unionWallRange(intervals: readonly WallRange[]): WallRange | null {
  let startMs = Number.POSITIVE_INFINITY;
  let endMs = Number.NEGATIVE_INFINITY;
  for (const interval of intervals) {
    if (!validRange(interval)) continue;
    if (interval.startMs < startMs) startMs = interval.startMs;
    if (interval.endMs > endMs) endMs = interval.endMs;
  }
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return null;
  return { startMs, endMs };
}

function sameWallRange(left: WallRange, right: WallRange): boolean {
  return left.startMs === right.startMs && left.endMs === right.endMs;
}

/** Whether the pick already covers this agent's whole timed span. */
export function isAgentFullySelected(
  state: DiagnosisSelectionState,
  subjectId: string,
  intervals: readonly WallRange[],
): boolean {
  const full = unionWallRange(intervals);
  if (full === null) return false;
  const current = laneRangeOf(state, subjectId);
  return current !== null && sameWallRange(current, full);
}

/**
 * Select or clear one agent's whole timed span.
 *
 * A second click on the same full span drops that agent from the pick.
 */
export function toggleSelectAllOnLane(
  state: DiagnosisSelectionState,
  subjectId: string,
  intervals: readonly WallRange[],
): DiagnosisSelectionState {
  const full = unionWallRange(intervals);
  if (full === null) return state;
  if (isAgentFullySelected(state, subjectId, intervals)) {
    return selectOnLane(state, subjectId, null);
  }
  return selectOnLane(state, subjectId, full);
}

/** Whether an agent counts in the current pick; drives its checkbox. */
export function isAgentIncluded(state: DiagnosisSelectionState, subjectId: string): boolean {
  // Nothing picked yet: leave every lane unticked until the user draws a window.
  if (state.mode === 'none') return false;
  if (state.mode === 'lanes') return state.laneRanges.has(subjectId);
  return !state.excluded.has(subjectId);
}

/**
 * Flip an agent's checkbox.
 *
 * On the shared window it excludes or restores the agent. With per-agent
 * windows, unticking drops that agent's window; a window cannot be created by
 * ticking, only by drawing it. With nothing picked yet, ticking is ignored.
 */
export function toggleAgent(state: DiagnosisSelectionState, subjectId: string): DiagnosisSelectionState {
  if (state.mode === 'none') return state;
  if (state.mode === 'lanes') {
    return state.laneRanges.has(subjectId) ? selectOnLane(state, subjectId, null) : state;
  }
  const excluded = new Set(state.excluded);
  if (excluded.has(subjectId)) excluded.delete(subjectId);
  else excluded.add(subjectId);
  return { ...state, excluded };
}

/**
 * Whether the pick contains a whole OTel span of one agent.
 *
 * An OTel span counts only when both its start and end on the swimlane lie
 * inside the agent's window (the shared axis window, unless that agent is
 * unchecked). Covering only one of the two moments is not enough. The whole
 * trajectory contains every span.
 */
export function selectionCoversInterval(
  state: DiagnosisSelectionState,
  subjectId: string,
  interval: WallRange,
  fullScope: boolean,
): boolean {
  if (!Number.isFinite(interval.startMs) || !Number.isFinite(interval.endMs) || interval.endMs < interval.startMs) {
    return false;
  }
  if (fullScope) return true;
  const range = laneRangeOf(state, subjectId);
  if (range === null) return false;
  return interval.startMs >= range.startMs && interval.endMs <= range.endMs;
}

/** The window to draw on one agent's lane, or `null` for none. */
export function laneRangeOf(state: DiagnosisSelectionState, subjectId: string): WallRange | null {
  if (state.mode === 'lanes') return state.laneRanges.get(subjectId) ?? null;
  if (state.mode === 'axis' && !state.excluded.has(subjectId)) return state.axisRange;
  return null;
}

/** Ranges to send, one per agent, in the order of `subjects`. Agents not in `subjects` are dropped. */
export function selectionRanges(
  state: DiagnosisSelectionState,
  subjects: readonly DiagnosisSubjectChoice[],
): DiagnosisRangeChoice[] {
  if (state.mode === 'axis') {
    return diagnosisSelectionRanges({
      range: state.axisRange,
      subjects,
      excludedSubjectIds: state.excluded,
    });
  }
  if (state.mode !== 'lanes') return [];
  const ranges: DiagnosisRangeChoice[] = [];
  for (const subject of subjects) {
    const range = state.laneRanges.get(subject.subjectId);
    if (range === undefined || !validRange(range)) continue;
    ranges.push({
      subjectId: subject.subjectId,
      subjectLabel: subject.subjectLabel,
      startMs: range.startMs,
      endMs: range.endMs,
    });
  }
  return ranges;
}

/** A diagnosis may cover at most this many OTel spans. One more and it cannot start. */
export const DIAGNOSIS_NODE_LIMIT = 1_500;

/**
 * Whether the per-agent include checkbox should appear for one agent.
 *
 * Shown for a shared axis brush (exclude members), and for a lane that already
 * owns a window (uncheck clears that pick). Hidden for a full-trajectory pick
 * and when nothing is picked — those are not "include in brush" moments.
 *
 * `fullScope` is accepted for call-site symmetry; it never shows the toggle.
 */
export function shouldShowIncludeToggle(
  state: DiagnosisSelectionState,
  fullScope: boolean,
  subjectId?: string,
): boolean {
  void fullScope;
  if (state.mode === 'axis') return true;
  if (state.mode === 'lanes' && subjectId !== undefined) {
    return state.laneRanges.has(subjectId);
  }
  return false;
}

/**
 * How many OTel spans the pick contains.
 *
 * A span counts only when the pick covers it whole on the swimlane — both
 * start and end (see `selectionCoversInterval`). The whole trajectory
 * contains every span.
 */
export function selectedNodeCount(
  state: DiagnosisSelectionState,
  lanes: readonly { subjectId: string; intervals: readonly WallRange[] }[],
  fullScope: boolean,
): number {
  let count = 0;
  for (const lane of lanes) {
    for (const interval of lane.intervals) {
      if (selectionCoversInterval(state, lane.subjectId, interval, fullScope)) count += 1;
    }
  }
  return count;
}

/** Whether anything would be diagnosed. */
export function hasSelection(
  state: DiagnosisSelectionState,
  subjects: readonly DiagnosisSubjectChoice[],
): boolean {
  return selectionRanges(state, subjects).length > 0;
}

/**
 * The request scope for the current pick, or 
ull when there is nothing to send.
 *
 * ullScope ignores the pick. Windows that cannot be written as Unix
 * nanoseconds are skipped, and a pick that loses all of them sends nothing.
 */
export function buildDiagnosisScope(
  state: DiagnosisSelectionState,
  subjects: readonly DiagnosisSubjectChoice[],
  fullScope: boolean,
): DiagnosisScope | null {
  if (fullScope) return { kind: 'full' };
  const ranges: SelectionRange[] = [];
  for (const choice of selectionRanges(state, subjects)) {
    const nano = msRangeToUnixNano(choice.startMs, choice.endMs);
    if (nano !== null) ranges.push({ subject_id: choice.subjectId, ...nano });
  }
  return ranges.length === 0 ? null : { kind: 'selection', ranges };
}
/**
 * Display models of the diagnosis sub-page: stage list, record list rows and
 * the diagnosed target.
 *
 * Kept free of React and browser APIs so it can be tested with node --test.
 */

import type {
  DiagnosisRecord,
  DiagnosisStage,
  DiagnosisStatus,
  DiagnosisSummary,
  StageStatus,
} from './diagnosisTypes';

/** Stages the engine runs, in order. Shown as waiting until the engine starts them. */
export const PLANNED_STAGE_IDS = ['task_parse', 'diagnose', 'attribute'] as const;

export type StageViewStatus = StageStatus | 'pending';

export interface StageView {
  stageId: string;
  status: StageViewStatus;
  /** The engine's stage, or `null` for a waiting placeholder. */
  stage: DiagnosisStage | null;
}

/**
 * Stages to draw for a record.
 *
 * While the diagnosis runs, planned stages the engine has not started yet
 * appear as waiting placeholders, and stages outside the plan follow the
 * planned ones. A finished diagnosis lists exactly what the engine reported.
 */
export function stageViews(stages: readonly DiagnosisStage[], running: boolean): StageView[] {
  if (!running) {
    return stages.map(stage => ({ stageId: stage.stage_id, status: stage.status, stage }));
  }
  const byId = new Map(stages.map(stage => [stage.stage_id, stage] as const));
  const planned = new Set<string>(PLANNED_STAGE_IDS);
  const views: StageView[] = PLANNED_STAGE_IDS.map((stageId) => {
    const stage = byId.get(stageId);
    return stage === undefined
      ? { stageId, status: 'pending', stage: null }
      : { stageId, status: stage.status, stage };
  });
  for (const stage of stages) {
    if (!planned.has(stage.stage_id)) {
      views.push({ stageId: stage.stage_id, status: stage.status, stage });
    }
  }
  return views;
}

/**
 * Whether a stage body is open.
 *
 * Without a user choice, running, failed and skipped stages are open and
 * completed ones fold away. A waiting stage has no body.
 */
export function isStageExpanded(status: StageViewStatus, userChoice: boolean | undefined): boolean {
  if (status === 'pending') return false;
  if (userChoice !== undefined) return userChoice;
  return status !== 'completed';
}

export type StatusIconKind = 'running' | 'completed' | 'failed';

/** Icon of a record in the record list: cancelled and interrupted count as unsuccessful. */
export function recordIconKind(status: DiagnosisStatus): StatusIconKind {
  if (status === 'running') return 'running';
  return status === 'completed' ? 'completed' : 'failed';
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0');
}

function dateOf(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function clockOf(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** `YYYY-MM-DD HH:mm:ss` in local time, or an empty string for a missing or invalid timestamp. */
export function formatRecordTime(value: string | undefined): string {
  if (value === undefined || value === '') return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return `${dateOf(date)} ${clockOf(date)}`;
}

/** Second line of a record row: the engine's headline, else the scope kind. */
export function recordLabel(summary: Pick<DiagnosisSummary, 'headline' | 'scope_kind'>, scopeLabel: string): string {
  return summary.headline === undefined || summary.headline === '' ? scopeLabel : summary.headline;
}

/** Records whose time or label contains every word of the query, case-insensitive. */
export function filterRecords(
  items: readonly DiagnosisSummary[],
  query: string,
  scopeLabelOf: (kind: DiagnosisSummary['scope_kind']) => string,
): DiagnosisSummary[] {
  const words = query.toLowerCase().split(/\s+/).filter(word => word !== '');
  if (words.length === 0) return [...items];
  return items.filter((item) => {
    const haystack = `${formatRecordTime(item.created_at)} ${recordLabel(item, scopeLabelOf(item.scope_kind))}`
      .toLowerCase();
    return words.every(word => haystack.includes(word));
  });
}

/** Unix nanoseconds, as the API carries them, to Unix milliseconds; `null` when malformed. */
export function unixNanoToMs(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const ms = Number(BigInt(value) / BigInt(1_000_000));
  return Number.isFinite(ms) ? ms : null;
}

export interface DiagnosisTargetGroup {
  startMs: number;
  endMs: number;
  subjectIds: string[];
}

export interface DiagnosisTarget {
  /** The whole trajectory was diagnosed, so there is no window to show. */
  full: boolean;
  /** Agents sharing one window; a single group when the selection came from the time axis. */
  groups: DiagnosisTargetGroup[];
}

/** What a record diagnosed: the whole trajectory, or windows grouped by identical time span. */
export function diagnosisTarget(record: Pick<DiagnosisRecord, 'input'>): DiagnosisTarget {
  const scope = record.input.scope;
  if (scope.kind === 'full') return { full: true, groups: [] };
  const groups: DiagnosisTargetGroup[] = [];
  for (const range of scope.ranges) {
    const startMs = unixNanoToMs(range.start_unix_nano);
    const endMs = unixNanoToMs(range.end_unix_nano);
    if (startMs === null || endMs === null) continue;
    const group = groups.find(item => item.startMs === startMs && item.endMs === endMs);
    if (group === undefined) groups.push({ startMs, endMs, subjectIds: [range.subject_id] });
    else group.subjectIds.push(range.subject_id);
  }
  return { full: false, groups };
}

/** `YYYY-MM-DD HH:mm:ss – HH:mm:ss`; the end repeats the date only when it falls on another day. */
export function formatTimeWindow(startMs: number, endMs: number): string {
  const start = new Date(startMs);
  const end = new Date(endMs);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '';
  const sameDay = dateOf(start) === dateOf(end);
  return `${dateOf(start)} ${clockOf(start)} – ${sameDay ? '' : `${dateOf(end)} `}${clockOf(end)}`;
}

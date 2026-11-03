/**
 * Project Team `main` control records into a task timeline.
 *
 * `main` stays out of the member lanes. These events are rendered on the same
 * shared wall-clock axis as the lanes so task transitions keep their natural
 * timing relationship with member execution.
 */

import { OPENJIUWEN_ATTRIBUTES, OPENJIUWEN_SEMCONV } from './semconv/constants';
import type { OtlpSpan } from './shared/otlp';
import type { TrajectoryDetailRecord } from './trajectoryClient';
import { rawOtelSpanInterval } from './diagnosis/diagnosisTimeSelection';
import { detailRecordIdentity, spansOf } from './trajectoryWindow';

export type ScheduledTaskPhase =
  | 'pending'
  | 'running'
  | 'in_review'
  | 'rework'
  | 'completed'
  | 'escalated'
  | 'cancelled'
  | 'unknown';

export type ScheduledControlEventKind =
  | 'created'
  | 'updated'
  | 'unblocked'
  | 'started'
  | 'submitted'
  | 'vote'
  | 'revision'
  | 'verified'
  | 'cancelled'
  | 'escalated'
  | 'anomaly'
  | 'team_completed';

export type ScheduledReviewDecision =
  | { kind: 'pass' }
  | { kind: 'fail'; feedback?: string }
  | { kind: 'score'; value: number }
  | { kind: 'pending' };

export interface ScheduledControlEvent {
  eventId: string;
  recordId: string;
  kind: ScheduledControlEventKind;
  spanName: string;
  timestampMs: number;
  taskId?: string;
  memberName?: string;
  reviewer?: string;
  reviewers?: string[];
  reviewRound?: number;
  decision?: ScheduledReviewDecision;
  feedback?: string;
  summary?: string;
  detector?: string;
  severity?: string;
}

export interface ScheduledReviewRun {
  key: string;
  taskId: string;
  reviewer: string;
  round: number;
  submittedAtMs?: number;
  decidedAtMs?: number;
  decision: ScheduledReviewDecision;
  feedback?: string;
}

export interface ScheduledTask {
  taskId: string;
  phase: ScheduledTaskPhase;
  assignee?: string;
  createdAtMs?: number;
  updatedAtMs?: number;
  completedAtMs?: number;
  events: ScheduledControlEvent[];
  reviews: ScheduledReviewRun[];
}

export interface ScheduledAnomaly {
  eventId: string;
  recordId: string;
  timestampMs: number;
  memberName?: string;
  detector?: string;
  severity?: string;
  summary?: string;
}

export interface TeamControlModel {
  tasks: ScheduledTask[];
  anomalies: ScheduledAnomaly[];
  controlRecordCount: number;
  knownEventCount: number;
  reworkCount: number;
  completedTaskCount: number;
  teamCompletedAtMs?: number;
}

interface ParsedControlRecord {
  event: ScheduledControlEvent;
  status?: string;
}

interface MutableTask {
  taskId: string;
  phase: ScheduledTaskPhase;
  assignee?: string;
  createdAtMs?: number;
  updatedAtMs?: number;
  completedAtMs?: number;
  events: ScheduledControlEvent[];
  reviews: Map<string, ScheduledReviewRun>;
}

const MAIN_SUBJECT_ID = 'main';
const TASK_CREATED = /^task\.([^.]+)\.created$/;
const TASK_CHANGED = /^task\.([^.]+)\.(updated|unblocked|cancelled)$/;

function firstSpan(record: TrajectoryDetailRecord): OtlpSpan | null {
  return record.otlp === null ? null : (spansOf(record.otlp)[0] ?? null);
}

function isMainControlSpan(span: OtlpSpan): boolean {
  const attributes = new Map(
    (span.attributes ?? []).map(attribute => {
      const value = attribute.value;
      const normalized = 'stringValue' in value
        ? value.stringValue
        : 'intValue' in value
          ? value.intValue
          : 'doubleValue' in value
            ? String(value.doubleValue)
            : 'boolValue' in value
              ? String(value.boolValue)
              : undefined;
      return [attribute.key, normalized] as const;
    }),
  );
  const subjectId = attributes.get(OPENJIUWEN_ATTRIBUTES.executionSubjectId);
  return subjectId === undefined || subjectId === MAIN_SUBJECT_ID;
}

function readPayload(span: OtlpSpan, key: string): Record<string, unknown> {
  const raw = (span.attributes ?? [])
    .find(attribute => attribute.key === key)
    ?.value;
  if (raw === undefined || !('stringValue' in raw)) return {};
  try {
    const parsed: unknown = JSON.parse(raw.stringValue);
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function attributeText(span: OtlpSpan, key: string): string | undefined {
  const value = (span.attributes ?? []).find(attribute => attribute.key === key)?.value;
  return value !== undefined && 'stringValue' in value ? value.stringValue.trim() || undefined : undefined;
}

function taskAssignee(span: OtlpSpan, data: Record<string, unknown>): string | undefined {
  return readString(data.assignee)
    ?? readString(data.member_name)
    ?? attributeText(span, OPENJIUWEN_SEMCONV.AT_TASK_ASSIGNEE);
}

function readString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined;
}

function readNumber(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'string' || value.trim() === '') return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function readReviewers(value: unknown): string[] {
  if (typeof value === 'string' && value.trim() !== '') return [value.trim()];
  if (!Array.isArray(value)) return [];
  return value
    .map(item => readString(item))
    .filter((item): item is string => item !== undefined);
}

function normalizeDecision(value: unknown): ScheduledReviewDecision | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return { kind: 'score', value };
  }
  const text = readString(value)?.toLowerCase();
  if (text === 'pass') return { kind: 'pass' };
  if (text === 'fail') {
    return { kind: 'fail' };
  }
  const score = Number(text);
  return Number.isFinite(score) ? { kind: 'score', value: score } : undefined;
}

function phaseFromStatus(status: string | undefined): ScheduledTaskPhase | undefined {
  switch (status) {
    case 'pending':
    case 'blocked':
      return 'pending';
    case 'in_progress':
    case 'running':
      return 'running';
    case 'in_review':
      return 'in_review';
    case 'rework':
    case 'revision_requested':
      return 'rework';
    case 'completed':
    case 'verified':
      return 'completed';
    case 'escalated':
      return 'escalated';
    case 'cancelled':
      return 'cancelled';
    default:
      return undefined;
  }
}

function recordTime(record: TrajectoryDetailRecord): number | undefined {
  const interval = rawOtelSpanInterval(record);
  if (interval !== null) return interval.startMs;
  const observed = record.observed_time_unix_nano;
  if (observed === undefined) return undefined;
  try {
    return Number(BigInt(observed) / 1_000_000n);
  } catch {
    return undefined;
  }
}

function parseControlRecord(record: TrajectoryDetailRecord): ParsedControlRecord | null {
  const span = firstSpan(record);
  if (span === null || !isMainControlSpan(span)) return null;
  const timestampMs = recordTime(record);
  if (timestampMs === undefined) return null;
  const input = readPayload(span, OPENJIUWEN_ATTRIBUTES.spanInput);
  const output = readPayload(span, 'openjiuwen.span.output');
  const data = Object.keys(input).length > 0 ? input : output;
  const base = {
    eventId: attributeText(span, OPENJIUWEN_ATTRIBUTES.trajectoryEventId)
      ?? `${span.traceId}:${span.spanId}`,
    recordId: detailRecordIdentity(record) ?? `${span.traceId}:${span.spanId}`,
    spanName: span.name,
    timestampMs,
  };

  const created = TASK_CREATED.exec(span.name);
  if (created !== null) {
    return {
      event: {
        ...base,
        kind: 'created',
        taskId: created[1],
        memberName: taskAssignee(span, data),
      },
      status: readString(data.status) ?? attributeText(span, 'agentteam.task.status'),
    };
  }

  const changed = TASK_CHANGED.exec(span.name);
  if (changed !== null) {
    return {
      event: {
        ...base,
        kind: changed[2] as 'updated' | 'unblocked' | 'cancelled',
        taskId: changed[1],
        memberName: taskAssignee(span, data),
      },
      status: readString(data.status) ?? attributeText(span, 'agentteam.task.status'),
    };
  }

  if (span.name === 'event.anomaly.detected') {
    return {
      event: {
        ...base,
        kind: 'anomaly',
        memberName: readString(data.member_name),
        detector: readString(data.detector),
        severity: readString(data.severity),
        summary: readString(data.summary),
      },
    };
  }

  if (span.name === 'team.completed') {
    return { event: { ...base, kind: 'team_completed' } };
  }

  const taskId = readString(data.task_id) ?? attributeText(span, 'agentteam.task.id');
  if (taskId === undefined) return null;
  const memberName = taskAssignee(span, data);
  const reviewRound = readNumber(data.review_round);

  switch (span.name) {
    case 'event.task.started':
      return { event: { ...base, kind: 'started', taskId, memberName } };
    case 'event.task.submitted.for.review': {
      const reviewers = readReviewers(data.reviewer);
      return {
        event: {
          ...base,
          kind: 'submitted',
          taskId,
          memberName,
          reviewRound,
          ...(reviewers.length === 0 ? {} : { reviewers }),
        },
      };
    }
    case 'event.task.review.vote':
      return {
        event: {
          ...base,
          kind: 'vote',
          taskId,
          memberName,
          reviewer: readString(data.reviewer),
          reviewRound,
          decision: normalizeDecision(data.decision),
          feedback: readString(data.feedback),
        },
      };
    case 'event.task.revision.requested':
      return {
        event: {
          ...base,
          kind: 'revision',
          taskId,
          memberName,
          reviewRound,
          feedback: readString(data.feedback),
          summary: readString(data.summary),
        },
      };
    case 'event.task.verified':
      return { event: { ...base, kind: 'verified', taskId, memberName, reviewRound } };
    case 'event.task.escalated':
      return { event: { ...base, kind: 'escalated', taskId, memberName, reviewRound } };
    default:
      return null;
  }
}

function upsertReview(
  task: MutableTask,
  reviewer: string,
  round: number,
  update: Partial<ScheduledReviewRun>,
): void {
  const key = `${task.taskId}:${round}:${reviewer}`;
  const current = task.reviews.get(key);
  task.reviews.set(key, {
    key,
    taskId: task.taskId,
    reviewer,
    round,
    decision: current?.decision ?? { kind: 'pending' },
    ...current,
    ...update,
  });
}

function applyRecord(
  tasks: Map<string, MutableTask>,
  event: ScheduledControlEvent,
  status?: string,
): void {
  if (event.taskId === undefined) return;
  let task = tasks.get(event.taskId);
  if (task === undefined) {
    task = {
      taskId: event.taskId,
      phase: 'unknown',
      events: [],
      reviews: new Map(),
    };
    tasks.set(event.taskId, task);
  }
  task.events.push(event);
  task.updatedAtMs = Math.max(task.updatedAtMs ?? 0, event.timestampMs);
  task.assignee = event.memberName ?? task.assignee;

  if (event.kind === 'created') {
    task.createdAtMs = Math.min(task.createdAtMs ?? event.timestampMs, event.timestampMs);
    task.phase = 'pending';
  } else if (event.kind === 'started') {
    task.phase = 'running';
  } else if (event.kind === 'submitted') {
    task.phase = 'in_review';
  } else if (event.kind === 'revision') {
    task.phase = 'rework';
  } else if (event.kind === 'verified') {
    task.phase = 'completed';
    task.completedAtMs = event.timestampMs;
  } else if (event.kind === 'cancelled') {
    task.phase = 'cancelled';
  } else if (event.kind === 'escalated') {
    task.phase = 'escalated';
  }

  const statusPhase = phaseFromStatus(status);
  if (statusPhase !== undefined
    && event.kind !== 'verified'
    && event.kind !== 'cancelled'
    && event.kind !== 'escalated') {
    task.phase = statusPhase;
  }

  const round = event.reviewRound ?? 1;
  if (event.kind === 'submitted') {
    for (const reviewer of event.reviewers ?? []) {
      upsertReview(task, reviewer, round, { submittedAtMs: event.timestampMs });
    }
  } else if (event.kind === 'vote' && event.reviewer !== undefined) {
    const decision = event.decision?.kind === 'fail'
      ? { kind: 'fail' as const, ...(event.feedback === undefined ? {} : { feedback: event.feedback }) }
      : event.decision ?? { kind: 'pending' as const };
    upsertReview(task, event.reviewer, round, {
      decidedAtMs: event.timestampMs,
      decision,
      ...(event.feedback === undefined ? {} : { feedback: event.feedback }),
    });
  }
}

function finalizeTask(task: MutableTask): ScheduledTask {
  return {
    taskId: task.taskId,
    phase: task.phase,
    ...(task.assignee === undefined ? {} : { assignee: task.assignee }),
    ...(task.createdAtMs === undefined ? {} : { createdAtMs: task.createdAtMs }),
    ...(task.updatedAtMs === undefined ? {} : { updatedAtMs: task.updatedAtMs }),
    ...(task.completedAtMs === undefined ? {} : { completedAtMs: task.completedAtMs }),
    events: [...task.events].sort((left, right) => (
      left.timestampMs - right.timestampMs || left.recordId.localeCompare(right.recordId)
    )),
    reviews: [...task.reviews.values()].sort((left, right) => (
      left.round - right.round || left.reviewer.localeCompare(right.reviewer)
    )),
  };
}

export function buildTeamControlModel(
  records: readonly TrajectoryDetailRecord[],
): TeamControlModel {
  const parsed: ParsedControlRecord[] = [];
  let controlRecordCount = 0;
  for (const record of records) {
    const span = firstSpan(record);
    if (span === null || !isMainControlSpan(span)) continue;
    controlRecordCount += 1;
    const control = parseControlRecord(record);
    if (control !== null) parsed.push(control);
  }
  parsed.sort((left, right) => (
    left.event.timestampMs - right.event.timestampMs
    || left.event.recordId.localeCompare(right.event.recordId)
  ));

  const tasks = new Map<string, MutableTask>();
  const anomalies: ScheduledAnomaly[] = [];
  let teamCompletedAtMs: number | undefined;
  for (const control of parsed) {
    const event = control.event;
    if (event.kind === 'anomaly') {
      anomalies.push({
        eventId: event.eventId,
        recordId: event.recordId,
        timestampMs: event.timestampMs,
        ...(event.memberName === undefined ? {} : { memberName: event.memberName }),
        ...(event.detector === undefined ? {} : { detector: event.detector }),
        ...(event.severity === undefined ? {} : { severity: event.severity }),
        ...(event.summary === undefined ? {} : { summary: event.summary }),
      });
    } else if (event.kind === 'team_completed') {
      teamCompletedAtMs = event.timestampMs;
    } else {
      applyRecord(tasks, event, control.status);
    }
  }

  const finalized = [...tasks.values()]
    .map(finalizeTask)
    .sort((left, right) => (
      (left.createdAtMs ?? Number.MAX_SAFE_INTEGER)
      - (right.createdAtMs ?? Number.MAX_SAFE_INTEGER)
      || left.taskId.localeCompare(right.taskId)
    ));
  return {
    tasks: finalized,
    anomalies: anomalies.sort((left, right) => left.timestampMs - right.timestampMs),
    controlRecordCount,
    knownEventCount: parsed.length,
    reworkCount: parsed.filter(item => item.event.kind === 'revision').length,
    completedTaskCount: finalized.filter(task => task.phase === 'completed').length,
    ...(teamCompletedAtMs === undefined ? {} : { teamCompletedAtMs }),
  };
}

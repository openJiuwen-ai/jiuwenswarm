// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Retention checkpoints, read into the seeds the views start from.
 *
 * Retention removes a session's oldest turns as whole pages and leaves one
 * checkpoint per execution subject: the state the session-wide derivations had
 * reached over what it removed. The store documents the contract
 * (`jiuwenswarm/observability/retention.py`); this module reads it from the
 * API or an archive, rebuilds the content it refers to, and splits it into
 * what each consumer seeds itself with -- subject grouping, the projector and
 * the schema-v2 reducer -- so the turns that remain render as they did.
 */

import type {
  TrajectoryLineageSeed,
  TrajectoryProjectionCheckpoint,
} from './projector/otel-trajectory-projector';
import {
  parseTrajectoryV2SubjectSeed,
  type TrajectoryV2SubjectSeed,
} from './projector/trajectory-v2-reducer';
import type { OtlpExportTraceServiceRequest } from './shared/otlp';
import type { TrajectoryDetailRecord } from './trajectoryClient';
import {
  rebuildRecord,
  rebuildSequenceValue,
  type SequenceCache,
} from './trajectorySequences';
import type {
  TrajectoryRetiredSubject,
  TrajectorySubjectKind,
} from './trajectorySubjects';

export const TRAJECTORY_CHECKPOINT_STATE_VERSION = 1;

/** Every seed one session's checkpoints yield, by the id each consumer keys on. */
export interface TrajectoryRetentionCheckpoints {
  /** Projector seeds, by execution subject. */
  projections: ReadonlyMap<string, TrajectoryProjectionCheckpoint>;
  /** Grouping seeds, by execution subject. */
  retiredSubjects: ReadonlyMap<string, TrajectoryRetiredSubject>;
  /** Reducer seeds, by trajectory subject. */
  v2Seeds: ReadonlyMap<string, TrajectoryV2SubjectSeed>;
}

export const EMPTY_TRAJECTORY_CHECKPOINTS: TrajectoryRetentionCheckpoints = {
  projections: new Map(),
  retiredSubjects: new Map(),
  v2Seeds: new Map(),
};

const SUBJECT_KINDS = new Set<TrajectorySubjectKind>([
  'main_agent',
  'team_leader',
  'team_member',
  'subagent',
  'unassigned',
]);

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function count(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new Error('Trajectory checkpoint states an invalid count');
  }
  return Number(value);
}

function optionalText(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string') throw new Error('Trajectory checkpoint states an invalid subject');
  return value;
}

function addReference(heads: string[], value: unknown): void {
  const reference = object(value);
  if (typeof reference?.hash === 'string') heads.push(reference.hash);
}

/** Chains one checkpoint line refers to, so they can be resolved before it is read. */
export function trajectoryCheckpointHeads(line: unknown): string[] {
  const state = object(object(line)?.state);
  if (state === undefined) return [];
  const heads: string[] = [];
  for (const seed of Array.isArray(state.lineage) ? state.lineage : []) {
    for (const reference of Object.values(object(object(seed)?.sequences) ?? {})) {
      addReference(heads, reference);
    }
  }
  for (const stored of Object.values(object(state.v2) ?? {})) {
    const subject = object(stored);
    addReference(heads, object(subject?.window)?.messages);
    addReference(heads, subject?.epoch_baseline_base);
    addReference(heads, object(subject?.held)?.base);
    addReference(heads, object(subject?.held)?.operations);
  }
  return [...new Set(heads)];
}

/**
 * Rebuild one message list a checkpoint stores by reference.
 *
 * A depth of zero with no hash is the empty list; anything the cache cannot
 * rebuild is an error, because a seed missing its window would replay the
 * remaining turns from the wrong base.
 */
function resolvedList(value: unknown, cache: SequenceCache): unknown[] | undefined {
  if (value === null || value === undefined) return undefined;
  const reference = object(value);
  if (reference === undefined) throw new Error('Trajectory checkpoint states an invalid reference');
  if (reference.hash === null && reference.depth === 0) return [];
  if (typeof reference.hash !== 'string') {
    throw new Error('Trajectory checkpoint states an invalid reference');
  }
  const rebuilt = rebuildSequenceValue(cache, reference.hash);
  if (rebuilt === undefined) {
    throw new Error('Trajectory checkpoint content is missing');
  }
  const parsed: unknown = JSON.parse(rebuilt);
  if (!Array.isArray(parsed)) throw new Error('Trajectory checkpoint content is not a list');
  return parsed;
}

function retiredSubject(subjectId: string, value: unknown): TrajectoryRetiredSubject | undefined {
  if (value === null || value === undefined) return undefined;
  const subject = object(value);
  if (subject === undefined
    || typeof subject.display_name !== 'string'
    || typeof subject.kind !== 'string'
    || !SUBJECT_KINDS.has(subject.kind as TrajectorySubjectKind)
    || typeof subject.projected !== 'boolean'
    || typeof subject.first_observed_time_unix_nano !== 'string'
    || !/^\d+$/.test(subject.first_observed_time_unix_nano)) {
    throw new Error('Trajectory checkpoint states an invalid subject');
  }
  return {
    subject: {
      id: subjectId,
      displayName: subject.display_name,
      kind: subject.kind as TrajectorySubjectKind,
      parentId: optionalText(subject.parent_id),
      sessionId: optionalText(subject.session_id),
    },
    projected: subject.projected,
    firstObservedTimeUnixNano: subject.first_observed_time_unix_nano,
  };
}

function projectionCheckpoint(
  state: Record<string, unknown>,
  cache: SequenceCache,
): TrajectoryProjectionCheckpoint {
  const turns = object(state.turns) ?? {};
  const traceTurnIds = new Map<string, readonly string[]>();
  for (const [traceId, turnIds] of Object.entries(object(turns.trace_turn_ids) ?? {})) {
    if (!Array.isArray(turnIds) || !turnIds.every(turnId => typeof turnId === 'string')) {
      throw new Error('Trajectory checkpoint states invalid turn ids');
    }
    traceTurnIds.set(traceId, turnIds as string[]);
  }
  const requestOffsets = new Map(Object.entries(object(state.requests) ?? {}).map(
    ([key, value]) => [key, count(value)] as const,
  ));
  const lineage = (Array.isArray(state.lineage) ? state.lineage : []).map((value): TrajectoryLineageSeed => {
    const seed = object(value);
    const record = object(seed?.record);
    if (seed === undefined || record === undefined || !Array.isArray(record.resourceSpans)
      || typeof seed.handled_by_v2 !== 'boolean') {
      throw new Error('Trajectory checkpoint states an invalid lineage seed');
    }
    const rebuilt = rebuildRecord({
      ingest_seq: 0,
      otlp: record as unknown as OtlpExportTraceServiceRequest,
      raw_valid: true,
      sequences: object(seed.sequences) as TrajectoryDetailRecord['sequences'],
    }, cache);
    if (rebuilt.incomplete_sequences !== undefined) {
      throw new Error('Trajectory checkpoint content is missing');
    }
    return {
      record: rebuilt.otlp as OtlpExportTraceServiceRequest,
      handledByV2: seed.handled_by_v2,
    };
  });
  return {
    turns: {
      maxNumber: count(turns.max_number ?? 0),
      unnumbered: count(turns.unnumbered ?? 0),
      traceTurnIds,
    },
    requestOffsets,
    lineage,
  };
}

function v2Seed(value: unknown, cache: SequenceCache): TrajectoryV2SubjectSeed {
  const stored = object(value);
  if (stored === undefined) throw new Error('Trajectory checkpoint states an invalid chain');
  const window = object(stored.window);
  const held = object(stored.held);
  const seed = parseTrajectoryV2SubjectSeed({
    eventCount: stored.event_count,
    blocked: stored.blocked,
    ...(typeof stored.sequence_epoch === 'string' ? { sequenceEpoch: stored.sequence_epoch } : {}),
    ...(Number.isSafeInteger(stored.next_sequence) ? { nextSequence: stored.next_sequence } : {}),
    ...(window === undefined
      ? {}
      : { window: { id: window.id, messages: resolvedList(window.messages, cache) } }),
    ...(stored.epoch_baseline_base === null || stored.epoch_baseline_base === undefined
      ? {}
      : { epochBaselineBase: resolvedList(stored.epoch_baseline_base, cache) }),
    ...(held === undefined
      ? {}
      : { held: { base: resolvedList(held.base, cache), operations: resolvedList(held.operations, cache) } }),
  });
  if (seed === undefined) throw new Error('Trajectory checkpoint states an invalid chain');
  return seed;
}

/**
 * Read one session's checkpoint lines into the seeds each view starts from.
 *
 * @param lines Checkpoint objects as the API or an archive states them.
 * @param cache Content holding every chain the lines refer to.
 * @returns The seeds, keyed as their consumers look them up.
 * @throws Error when a line is malformed or its content cannot be rebuilt.
 */
export function resolveTrajectoryCheckpoints(
  lines: readonly unknown[],
  cache: SequenceCache,
): TrajectoryRetentionCheckpoints {
  if (lines.length === 0) return EMPTY_TRAJECTORY_CHECKPOINTS;
  const projections = new Map<string, TrajectoryProjectionCheckpoint>();
  const retiredSubjects = new Map<string, TrajectoryRetiredSubject>();
  const v2Seeds = new Map<string, TrajectoryV2SubjectSeed>();
  for (const line of lines) {
    const checkpoint = object(line);
    const state = object(checkpoint?.state);
    const subjectId = checkpoint?.subject_id;
    if (state === undefined || typeof subjectId !== 'string' || subjectId.length === 0) {
      throw new Error('Trajectory checkpoint is invalid');
    }
    if (state.version !== TRAJECTORY_CHECKPOINT_STATE_VERSION) {
      throw new Error(`Trajectory checkpoint version ${String(state.version)} is not supported`);
    }
    projections.set(subjectId, projectionCheckpoint(state, cache));
    const subject = retiredSubject(subjectId, state.subject);
    if (subject !== undefined) retiredSubjects.set(subjectId, subject);
    for (const [v2SubjectId, stored] of Object.entries(object(state.v2) ?? {})) {
      v2Seeds.set(v2SubjectId, v2Seed(stored, cache));
    }
  }
  return { projections, retiredSubjects, v2Seeds };
}

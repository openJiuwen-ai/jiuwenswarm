// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Explicit execution-subject grouping shared by live and archive trajectory views. */

import { OPENJIUWEN_ATTRIBUTES } from './semconv/constants';
import { stringAttribute, type OtlpExportTraceServiceRequest } from './shared/otlp';
import type { TrajectoryDetailRecord } from './trajectoryClient';
import { detailRecordIdentity, spansOf } from './trajectoryWindow';

export const MAIN_TRAJECTORY_SUBJECT_ID = 'main';
export const UNASSIGNED_TRAJECTORY_SUBJECT_ID = '__unassigned__';

export type TrajectorySubjectKind = 'main_agent' | 'team_leader' | 'team_member' | 'subagent' | 'unassigned';

export interface TrajectorySubject {
  id: string;
  displayName: string;
  kind: TrajectorySubjectKind;
  parentId: string | null;
  sessionId: string | null;
}

export interface TrajectorySubjectGroup {
  subject: TrajectorySubject;
  records: OtlpExportTraceServiceRequest[];
  rawRecords: TrajectoryDetailRecord[];
  lifecycleByRecordId: Map<string, 'running' | 'completed' | 'error'>;
  traceCount: number;
  firstObservedTimeUnixNano: string | null;
  label: string;
}

export interface TrajectorySubjectGroups {
  groups: TrajectorySubjectGroup[];
  byId: ReadonlyMap<string, TrajectorySubjectGroup>;
}

export interface TrajectorySubjectGroupingOptions {
  /** Team mode: skip the synthetic `main` group and treat team members as roots. */
  teamMode?: boolean;
}

export interface TrajectorySubjectView<TSnapshot> {
  groups: TrajectorySubjectGroups;
  snapshots: ReadonlyMap<string, TSnapshot>;
}

export interface TrajectorySubjectViewCache<TSnapshot> {
  update: (
    candidate: TrajectorySubjectGroups,
    project: (group: TrajectorySubjectGroup) => TSnapshot,
  ) => TrajectorySubjectView<TSnapshot>;
  clear: () => void;
}

const mainSubject: TrajectorySubject = {
  id: MAIN_TRAJECTORY_SUBJECT_ID,
  displayName: 'Main Agent',
  kind: 'main_agent',
  parentId: null,
  sessionId: null,
};

const unassignedSubject: TrajectorySubject = {
  id: UNASSIGNED_TRAJECTORY_SUBJECT_ID,
  displayName: 'Unassigned',
  kind: 'unassigned',
  parentId: null,
  sessionId: null,
};

function firstSpan(record: OtlpExportTraceServiceRequest | null | undefined) {
  if (record === null || record === undefined) return undefined;
  const spans = spansOf(record);
  return spans.length === 1 ? spans[0] : undefined;
}

export function trajectorySubjectOf(
  record: OtlpExportTraceServiceRequest | null | undefined,
): TrajectorySubject {
  const span = firstSpan(record);
  if (span === undefined) return unassignedSubject;
  const trajectorySchemaVersion = stringAttribute(
    span.attributes,
    OPENJIUWEN_ATTRIBUTES.trajectorySchemaVersion,
  );
  if (trajectorySchemaVersion === '2') {
    const trajectorySubjectId = stringAttribute(
      span.attributes,
      OPENJIUWEN_ATTRIBUTES.trajectorySubjectId,
    )?.trim();
    if (!trajectorySubjectId) return unassignedSubject;
    const legacyDisplayName = stringAttribute(
      span.attributes,
      OPENJIUWEN_ATTRIBUTES.executionSubjectDisplayName,
    )?.trim();
    const parentId = stringAttribute(
      span.attributes,
      OPENJIUWEN_ATTRIBUTES.executionSubjectParentId,
    )?.trim();
    const sessionId = stringAttribute(
      span.attributes,
      OPENJIUWEN_ATTRIBUTES.executionSubjectSessionId,
    )?.trim();
    const executionKind = stringAttribute(
      span.attributes,
      OPENJIUWEN_ATTRIBUTES.executionSubjectKind,
    )?.trim();
    if (trajectorySubjectId === MAIN_TRAJECTORY_SUBJECT_ID) {
      return {
        id: trajectorySubjectId,
        displayName: legacyDisplayName || mainSubject.displayName,
        kind: 'main_agent',
        parentId: parentId || null,
        sessionId: sessionId || null,
      };
    }
    if ((executionKind === 'team_leader' || executionKind === 'team_member')
      && legacyDisplayName) {
      return {
        id: trajectorySubjectId,
        displayName: legacyDisplayName,
        kind: executionKind,
        parentId: parentId || null,
        sessionId: sessionId || null,
      };
    }
    return {
      id: trajectorySubjectId,
      displayName: legacyDisplayName
        || trajectorySubjectId.replace(/^subagent:/, '')
        || trajectorySubjectId,
      kind: 'subagent',
      parentId: parentId || MAIN_TRAJECTORY_SUBJECT_ID,
      sessionId: sessionId || null,
    };
  }
  const id = stringAttribute(span.attributes, OPENJIUWEN_ATTRIBUTES.executionSubjectId);
  const displayName = stringAttribute(
    span.attributes,
    OPENJIUWEN_ATTRIBUTES.executionSubjectDisplayName,
  );
  const kind = stringAttribute(span.attributes, OPENJIUWEN_ATTRIBUTES.executionSubjectKind);
  const parentId = stringAttribute(
    span.attributes,
    OPENJIUWEN_ATTRIBUTES.executionSubjectParentId,
  );
  const subjectSessionId = stringAttribute(
    span.attributes,
    OPENJIUWEN_ATTRIBUTES.executionSubjectSessionId,
  );

  if (id === undefined && displayName === undefined && kind === undefined
    && parentId === undefined && subjectSessionId === undefined) {
    return mainSubject;
  }
  if (kind === 'main_agent' && id === MAIN_TRAJECTORY_SUBJECT_ID) {
    return {
      id,
      displayName: displayName?.trim() || mainSubject.displayName,
      kind,
      parentId: parentId?.trim() || null,
      sessionId: subjectSessionId?.trim() || null,
    };
  }
  if ((kind === 'team_leader' || kind === 'team_member')
    && id?.trim() && displayName?.trim()) {
    return {
      id: id.trim(),
      displayName: displayName.trim(),
      kind,
      parentId: parentId?.trim() || null,
      sessionId: subjectSessionId?.trim() || null,
    };
  }
  if (kind === 'subagent' && id?.trim() && displayName?.trim() && parentId?.trim()) {
    return {
      id,
      displayName: displayName.trim(),
      kind,
      parentId: parentId.trim(),
      sessionId: subjectSessionId?.trim() || null,
    };
  }
  return unassignedSubject;
}

function compareNano(left: string | null, right: string | null): number {
  if (left === right) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  if (left.length !== right.length) return left.length - right.length;
  return left.localeCompare(right);
}

function traceCount(records: readonly OtlpExportTraceServiceRequest[]): number {
  return new Set(records.flatMap(record => spansOf(record).map(span => span.traceId))).size;
}

function sameSubject(left: TrajectorySubject, right: TrajectorySubject): boolean {
  return left.id === right.id
    && left.displayName === right.displayName
    && left.kind === right.kind
    && left.parentId === right.parentId
    && left.sessionId === right.sessionId;
}

function sameArrayItems<T>(left: readonly T[], right: readonly T[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function sameLifecycle(
  left: ReadonlyMap<string, 'running' | 'completed' | 'error'>,
  right: ReadonlyMap<string, 'running' | 'completed' | 'error'>,
): boolean {
  return left.size === right.size
    && [...left].every(([identity, lifecycle]) => right.get(identity) === lifecycle);
}

function sameGroup(left: TrajectorySubjectGroup, right: TrajectorySubjectGroup): boolean {
  return sameSubject(left.subject, right.subject)
    && left.label === right.label
    && left.traceCount === right.traceCount
    && left.firstObservedTimeUnixNano === right.firstObservedTimeUnixNano
    && sameArrayItems(left.records, right.records)
    && sameArrayItems(left.rawRecords, right.rawRecords)
    && sameLifecycle(left.lifecycleByRecordId, right.lifecycleByRecordId);
}

function sameProjectionInput(
  left: TrajectorySubjectGroup,
  right: TrajectorySubjectGroup,
): boolean {
  return sameArrayItems(left.records, right.records)
    && sameLifecycle(left.lifecycleByRecordId, right.lifecycleByRecordId);
}

/** Preserve unchanged subject and projection identities across live publishes. */
export function createTrajectorySubjectViewCache<TSnapshot>(): TrajectorySubjectViewCache<TSnapshot> {
  let previousGroups: TrajectorySubjectGroups | null = null;
  let previousSnapshots = new Map<string, TSnapshot>();
  return {
    update: (candidate, project) => {
      const stableGroups = candidate.groups.map((group) => {
        const prior = previousGroups?.byId.get(group.subject.id);
        return prior !== undefined && sameGroup(prior, group) ? prior : group;
      });
      const groups = {
        groups: stableGroups,
        byId: new Map(stableGroups.map(group => [group.subject.id, group])),
      };
      const snapshots = new Map(stableGroups.map((group) => {
        const priorGroup = previousGroups?.byId.get(group.subject.id);
        const priorSnapshot = previousSnapshots.get(group.subject.id);
        return priorGroup !== undefined
          && sameProjectionInput(priorGroup, group)
          && previousSnapshots.has(group.subject.id)
          ? [group.subject.id, priorSnapshot as TSnapshot] as const
          : [group.subject.id, project(group)] as const;
      }));
      previousGroups = groups;
      previousSnapshots = snapshots;
      return { groups, snapshots };
    },
    clear: () => {
      previousGroups = null;
      previousSnapshots = new Map();
    },
  };
}

export function groupTrajectorySubjects(
  records: readonly OtlpExportTraceServiceRequest[],
  rawRecords: readonly TrajectoryDetailRecord[],
  lifecycleByRecordId: ReadonlyMap<string, 'running' | 'completed' | 'error'>,
  ownerSessionId?: string,
  options: TrajectorySubjectGroupingOptions = {},
): TrajectorySubjectGroups {
  const mutable = new Map<string, Omit<TrajectorySubjectGroup, 'label' | 'traceCount'>>();
  const ensure = (subject: TrajectorySubject, observedTime: string | null) => {
    const current = mutable.get(subject.id);
    if (current !== undefined) {
      if (compareNano(observedTime, current.firstObservedTimeUnixNano) < 0) {
        current.firstObservedTimeUnixNano = observedTime;
      }
      return current;
    }
    const group = {
      subject,
      records: [],
      rawRecords: [],
      lifecycleByRecordId: new Map<string, 'running' | 'completed' | 'error'>(),
      firstObservedTimeUnixNano: observedTime,
    };
    mutable.set(subject.id, group);
    return group;
  };

  if (!options.teamMode) ensure(mainSubject, null);
  const belongsToTeam = (subject: TrajectorySubject): boolean => (
    subject.kind === 'team_leader' || subject.kind === 'team_member'
  );
  for (const record of records) {
    const span = firstSpan(record);
    const subject = trajectorySubjectOf(record);
    // Team mode shows only concrete member lanes. Records without an
    // execution-subject block (team root / monitor spans) would otherwise fall
    // back to a synthetic "Main Agent" lane, so skip them.
    if (options.teamMode && !belongsToTeam(subject)) continue;
    if (subject.kind === 'subagent'
      && subject.sessionId !== null
      && ownerSessionId
      && !subject.sessionId.startsWith(`${ownerSessionId}_sub_`)) continue;
    ensure(subject, span?.startTimeUnixNano ?? null).records.push(record);
  }
  for (const rawRecord of rawRecords) {
    const span = firstSpan(rawRecord.otlp);
    const subject = trajectorySubjectOf(rawRecord.otlp);
    if (options.teamMode && !belongsToTeam(subject)) continue;
    if (subject.kind === 'subagent'
      && subject.sessionId !== null
      && ownerSessionId
      && !subject.sessionId.startsWith(`${ownerSessionId}_sub_`)) continue;
    const group = ensure(
      subject,
      rawRecord.observed_time_unix_nano ?? span?.startTimeUnixNano ?? null,
    );
    group.rawRecords.push(rawRecord);
    const identity = detailRecordIdentity(rawRecord);
    const lifecycle = identity === null ? undefined : lifecycleByRecordId.get(identity);
    if (identity !== null && lifecycle !== undefined) {
      group.lifecycleByRecordId.set(identity, lifecycle);
    }
  }

  const ordered = [...mutable.values()].sort((left, right) => {
    if (left.subject.kind === 'main_agent') return right.subject.kind === 'main_agent' ? 0 : -1;
    if (right.subject.kind === 'main_agent') return 1;
    if (left.subject.kind === 'team_leader') return right.subject.kind === 'team_leader' ? 0 : -1;
    if (right.subject.kind === 'team_leader') return 1;
    if (left.subject.kind === 'unassigned') return right.subject.kind === 'unassigned' ? 0 : 1;
    if (right.subject.kind === 'unassigned') return -1;
    return compareNano(left.firstObservedTimeUnixNano, right.firstObservedTimeUnixNano)
      || left.subject.id.localeCompare(right.subject.id);
  });
  const displayTotals = new Map<string, number>();
  for (const group of ordered) {
    if (group.subject.kind !== 'subagent') continue;
    displayTotals.set(
      group.subject.displayName,
      (displayTotals.get(group.subject.displayName) ?? 0) + 1,
    );
  }
  const displayOrdinals = new Map<string, number>();
  const groups = ordered.map((group): TrajectorySubjectGroup => {
    let label = group.subject.displayName;
    if (group.subject.kind === 'subagent'
      && (displayTotals.get(group.subject.displayName) ?? 0) > 1) {
      const ordinal = (displayOrdinals.get(group.subject.displayName) ?? 0) + 1;
      displayOrdinals.set(group.subject.displayName, ordinal);
      label = `${group.subject.displayName} ${ordinal}`;
    }
    return { ...group, label, traceCount: traceCount(group.records) };
  });
  return { groups, byId: new Map(groups.map(group => [group.subject.id, group])) };
}

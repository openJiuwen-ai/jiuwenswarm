// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Pure Team lane read model for parallel member swimlane navigation. */

import type { TrajectorySubjectGroup } from './trajectorySubjects';

export type TeamMemberLaneStatus = 'running' | 'completed' | 'error' | 'idle';

export interface TeamMemberLaneModel {
  subjectId: string;
  label: string;
  kind: TrajectorySubjectGroup['subject']['kind'];
  status: TeamMemberLaneStatus;
  traceCount: number;
  recordCount: number;
}

/**
 * Derive one member's swimlane status from its record lifecycle map.
 * Any running identity wins; otherwise an error identity marks the lane,
 * and an empty group is idle.
 */
export function laneStatusOf(group: TrajectorySubjectGroup): TeamMemberLaneStatus {
  let hasRecords = false;
  let hasRunning = false;
  let hasError = false;
  for (const lifecycle of group.lifecycleByRecordId.values()) {
    hasRecords = true;
    if (lifecycle === 'running') hasRunning = true;
    else if (lifecycle === 'error') hasError = true;
  }
  if (group.records.length > 0 || group.rawRecords.length > 0) hasRecords = true;
  if (!hasRecords) return 'idle';
  if (hasRunning) return 'running';
  if (hasError) return 'error';
  return 'completed';
}

/** Stable per-group metadata for the lane strip, ordered as the groups arrive. */
export function buildTeamMemberLanes(
  groups: readonly TrajectorySubjectGroup[],
): TeamMemberLaneModel[] {
  return groups.map((group): TeamMemberLaneModel => ({
    subjectId: group.subject.id,
    label: group.label,
    kind: group.subject.kind,
    status: laneStatusOf(group),
    traceCount: group.traceCount,
    recordCount: group.records.length,
  }));
}

/** First interactive lane (leader or earliest observed member) for team mode. */
export function defaultTeamMemberSubjectId(groups: readonly TrajectorySubjectGroup[]): string | null {
  const withRecords = groups.find(group => group.records.length > 0 || group.rawRecords.length > 0);
  return withRecords?.subject.id ?? groups[0]?.subject.id ?? null;
}

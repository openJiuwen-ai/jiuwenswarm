/** Display grouping for scheduled Team member and Review execution lanes. */

import type {
  ScheduledReviewDecision,
  ScheduledReviewRun,
  ScheduledTask,
  TeamControlModel,
} from './teamControlTimeline';
import {
  buildTeamMemberLanes,
  type TeamMemberLaneModel,
} from './teamTrajectoryLanes';
import type { TrajectorySubjectGroup } from './trajectorySubjects';

export interface ScheduledReviewerLane extends TeamMemberLaneModel {
  taskId: string;
  reviewer: string;
  round: number;
  attempt: number;
  decision?: ScheduledReviewDecision;
  feedback?: string;
  /** Full execution Subject label, kept for hover and accessibility. */
  fullLabel: string;
}

export interface ScheduledReviewerRound {
  round: number;
  lanes: ScheduledReviewerLane[];
}

export interface ScheduledReviewerTask {
  taskId: string;
  /** One-based ordinal from the scheduling control model, shared by both views. */
  taskIndex: number;
  phase?: string;
  assignee?: string;
  rounds: ScheduledReviewerRound[];
  runCount: number;
}

export interface ScheduledTeamLaneSections {
  members: TeamMemberLaneModel[];
  tasks: ScheduledReviewerTask[];
  ungroupedReviewers: ScheduledReviewerLane[];
  reviewRunCount: number;
}

export interface ScheduledTaskRun {
  key: string;
  tasks: ScheduledReviewerTask[];
  member?: TeamMemberLaneModel;
}

function memberForAssignee(
  assignee: string | undefined,
  members: readonly TeamMemberLaneModel[],
): TeamMemberLaneModel | undefined {
  const normalized = assignee?.trim();
  if (!normalized) return undefined;
  return members.find(member => (
    member.subjectId === normalized
    || member.subjectId.endsWith(`:${normalized}`)
    || member.label === normalized
  ));
}

export function scheduledTaskMember(
  task: ScheduledReviewerTask,
  members: readonly TeamMemberLaneModel[],
): TeamMemberLaneModel | undefined {
  return memberForAssignee(task.assignee, members);
}

export function groupScheduledTaskRuns(
  members: readonly TeamMemberLaneModel[],
  tasks: readonly ScheduledReviewerTask[],
): ScheduledTaskRun[] {
  const runs: ScheduledTaskRun[] = [];
  for (const task of tasks) {
    const member = scheduledTaskMember(task, members);
    const previous = runs[runs.length - 1];
    if (
      member !== undefined
      && previous?.member?.subjectId === member.subjectId
    ) {
      previous.tasks.push(task);
      continue;
    }
    runs.push({
      key: `task-run:${task.taskId}:${member?.subjectId ?? 'unassigned'}`,
      tasks: [task],
      ...(member === undefined ? {} : { member }),
    });
  }
  return runs;
}

export function scheduledMemberDisplayName(
  assignee: string,
  members: readonly TeamMemberLaneModel[],
): string {
  const normalized = assignee.trim();
  if (normalized === '') return assignee;
  return memberForAssignee(normalized, members)?.label ?? assignee;
}

interface ReviewerIdentity {
  taskId: string;
  reviewer: string;
  round: number;
  attempt: number;
}

const REVIEWER_PREFIX = 'team-reviewer:';

/**
 * Reviewer subjects end with `:{task}:{reviewer}:r{round}[:a{attempt}]`.
 * They are not split into a fixed number of fields because the session and
 * team identifiers may themselves contain separators.
 */
export function reviewerIdentity(subjectId: string): ReviewerIdentity | null {
  if (!subjectId.startsWith(REVIEWER_PREFIX)) return null;
  const parts = subjectId.split(':');
  const lastIndex = parts.length - 1;
  const trailingAttempt = /^a(\d+)$/.exec(parts[lastIndex] ?? '');
  const roundIndex = trailingAttempt === null ? lastIndex : lastIndex - 1;
  const roundMatch = /^r(\d+)$/.exec(parts[roundIndex] ?? '');
  if (roundMatch === null) return null;
  const reviewerIndex = roundIndex - 1;
  const taskIndex = reviewerIndex - 1;
  if (taskIndex < 1) return null;
  const taskId = parts[taskIndex];
  const reviewer = parts[reviewerIndex];
  if (!taskId || !reviewer) return null;
  return {
    taskId,
    reviewer,
    round: Number(roundMatch[1]),
    attempt: trailingAttempt === null ? 0 : Number(trailingAttempt[1]),
  };
}

function reviewerOrder(
  model: TeamControlModel | undefined,
  taskId: string,
  round: number,
): ReadonlyMap<string, number> {
  const order = new Map<string, number>();
  const task = model?.tasks.find(candidate => candidate.taskId === taskId);
  if (task === undefined) return order;
  for (const event of task.events) {
    if (event.kind !== 'submitted' || (event.reviewRound ?? 1) !== round) continue;
    for (const reviewer of event.reviewers ?? []) {
      if (!order.has(reviewer)) order.set(reviewer, order.size);
    }
  }
  return order;
}

function displayLabel(identity: ReviewerIdentity): string {
  const retry = identity.attempt === 0 ? '' : ` retry ${identity.attempt}`;
  return `R${identity.round} ${identity.reviewer}${retry}`;
}

function reviewKey(taskId: string, round: number, reviewer: string): string {
  return `${taskId}\0${round}\0${reviewer}`;
}

const MEANINGFUL_CANCEL_EVENTS = new Set([
  'submitted',
  'vote',
  'revision',
  'verified',
  'escalated',
  'anomaly',
]);

/**
 * A cancellation is worth a detailed trace card only when the task reached a
 * review/exception milestone. Create/start/unblock -> cancel churn is summary
 * data, not a useful expandable trace.
 */
export function shouldShowScheduledTaskDetail(
  task: ScheduledTask,
  hasReviewerLanes: boolean,
): boolean {
  if (task.phase !== 'cancelled') return true;
  if (hasReviewerLanes || task.reviews.length > 0) return true;
  return task.events.some(event => MEANINGFUL_CANCEL_EVENTS.has(event.kind));
}

/**
 * Keep every physical Reviewer subject independent while adding the display
 * hierarchy Task -> Round -> Reviewer -> Attempt.
 */
export function buildScheduledTeamLaneSections(
  groups: readonly TrajectorySubjectGroup[],
  model?: TeamControlModel,
): ScheduledTeamLaneSections {
  const lanes = buildTeamMemberLanes(groups);
  const reviews = new Map<string, ScheduledReviewRun>();
  for (const task of model?.tasks ?? []) {
    for (const review of task.reviews) {
      reviews.set(reviewKey(task.taskId, review.round, review.reviewer), review);
    }
  }
  const members: TeamMemberLaneModel[] = [];
  const reviewerByTask = new Map<string, ScheduledReviewerLane[]>();
  const ungroupedReviewers: ScheduledReviewerLane[] = [];

  for (const lane of lanes) {
    if (!lane.subjectId.startsWith(REVIEWER_PREFIX)) {
      members.push(lane);
      continue;
    }
    const identity = reviewerIdentity(lane.subjectId);
    if (identity === null) {
      ungroupedReviewers.push({
        ...lane,
        taskId: '',
        reviewer: lane.label,
        round: 0,
        attempt: 0,
        fullLabel: lane.label,
      });
      continue;
    }
    const review = reviews.get(reviewKey(identity.taskId, identity.round, identity.reviewer));
    const scheduled: ScheduledReviewerLane = {
      ...lane,
      label: displayLabel(identity),
      fullLabel: lane.label,
      taskId: identity.taskId,
      reviewer: identity.reviewer,
      round: identity.round,
      attempt: identity.attempt,
      ...(review === undefined
        ? {}
        : {
          decision: review.decision,
          ...(review.feedback === undefined ? {} : { feedback: review.feedback }),
        }),
    };
    reviewerByTask.set(identity.taskId, [
      ...(reviewerByTask.get(identity.taskId) ?? []),
      scheduled,
    ]);
  }

  const modeledTasks = model?.tasks ?? [];
  const modelTaskById = new Map(modeledTasks.map(task => [task.taskId, task]));
  const modeledTaskIds = modeledTasks.map(task => task.taskId);
  const taskEntries = [
    ...modeledTaskIds
      .map((taskId, index) => ({ taskId, taskIndex: index + 1 }))
      .filter(({ taskId }) => {
        const task = modelTaskById.get(taskId);
        const hasReviewerLanes = reviewerByTask.has(taskId);
        if (task !== undefined && !shouldShowScheduledTaskDetail(task, hasReviewerLanes)) {
          return false;
        }
        return hasReviewerLanes
          || memberForAssignee(task?.assignee, members) !== undefined;
      }),
    ...[...reviewerByTask.keys()]
      .filter(taskId => !modeledTaskIds.includes(taskId))
      .sort((left, right) => left.localeCompare(right))
      .map((taskId, index) => ({ taskId, taskIndex: modeledTaskIds.length + index + 1 })),
  ];

  const tasks = taskEntries.map(({ taskId, taskIndex }): ScheduledReviewerTask => {
    const task = model?.tasks.find(candidate => candidate.taskId === taskId);
    const taskLanes = reviewerByTask.get(taskId) ?? [];
    const byRound = new Map<number, ScheduledReviewerLane[]>();
    for (const lane of taskLanes) {
      byRound.set(lane.round, [...(byRound.get(lane.round) ?? []), lane]);
    }
    const rounds = [...byRound.entries()]
      .sort(([left], [right]) => left - right)
      .map(([round, roundLanes]): ScheduledReviewerRound => {
        const order = reviewerOrder(model, taskId, round);
        return {
          round,
          lanes: [...roundLanes].sort((left, right) => (
            (order.get(left.reviewer) ?? Number.MAX_SAFE_INTEGER)
            - (order.get(right.reviewer) ?? Number.MAX_SAFE_INTEGER)
            || left.reviewer.localeCompare(right.reviewer)
            || left.attempt - right.attempt
          )),
        };
      });
    return {
      taskId,
      taskIndex,
      ...(task === undefined ? {} : { phase: task.phase }),
      ...(task?.assignee === undefined ? {} : { assignee: task.assignee }),
      rounds,
      runCount: taskLanes.length,
    };
  });

  return {
    members,
    tasks,
    ungroupedReviewers,
    reviewRunCount: lanes.length - members.length,
  };
}

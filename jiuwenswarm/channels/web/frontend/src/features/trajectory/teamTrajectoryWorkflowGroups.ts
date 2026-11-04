/** Pure workflow-run grouping of team member lanes for swarmflow worker swimlanes. */

import type { TeamMemberLaneModel, TeamMemberLaneStatus } from './teamTrajectoryLanes';

/**
 * Minimal run summary the grouping reads from the session store's
 * `workflowRuns` (structural subset of the team-area `WorkflowRun`).
 */
export interface WorkflowRunLaneSummary {
  id: string;
  name?: string;
  status?: string;
}

export interface TeamTrajectoryWorkflowGroup {
  runId: string;
  name: string;
  shortId: string;
  status: TeamMemberLaneStatus;
  lanes: TeamMemberLaneModel[];
}

export interface TeamTrajectoryWorkflowLaneGrouping {
  ungrouped: TeamMemberLaneModel[];
  groups: TeamTrajectoryWorkflowGroup[];
}

const TEAM_MEMBER_SUBJECT_PREFIX = 'team-member:';

/**
 * Agent-core mints run ids as `wf_<12hex>` (`new_swarmflow_run_id`) and worker
 * member names as `{slug(run_id)}-{label_slug}-{ident}`, so every swarmflow
 * worker's member name opens with `wf-<12hex>-`. Runs absent from the store
 * (e.g. archive replay) still group through this pattern.
 */
const UNTRACKED_RUN_PATTERN = /^wf-([0-9a-f]{12})-/;

const RUN_STATUS_TO_LANE_STATUS: Record<string, TeamMemberLaneStatus> = {
  running: 'running',
  pending: 'running',
  planned: 'running',
  paused: 'running',
  waiting_for_human: 'running',
  failed: 'error',
  completed: 'completed',
  stopped: 'completed',
};

/** Mirror of agent-core `_run_id_prefix`: lowercase, non-alphanumerics to '-'. */
export function slugifyRunId(runId: string): string {
  return runId.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

/** Member name part of a `team-member:{session}:{team}:{member}` subject id. */
export function memberNameOfSubjectId(subjectId: string): string | null {
  if (!subjectId.startsWith(TEAM_MEMBER_SUBJECT_PREFIX)) return null;
  const rest = subjectId.slice(TEAM_MEMBER_SUBJECT_PREFIX.length);
  const sessionColon = rest.indexOf(':');
  if (sessionColon === -1) return null;
  const teamColon = rest.indexOf(':', sessionColon + 1);
  if (teamColon === -1) return null;
  const memberName = rest.slice(teamColon + 1);
  return memberName === '' ? null : memberName;
}

/** Map one `WorkflowStatus` onto the lane status dot vocabulary. */
export function workflowRunLaneStatus(status: string | undefined): TeamMemberLaneStatus {
  if (typeof status !== 'string') return 'idle';
  return RUN_STATUS_TO_LANE_STATUS[status] ?? 'idle';
}

export function shortWorkflowRunId(runId: string): string {
  return runId.length > 8 ? runId.slice(0, 8) : runId;
}

/**
 * Group workflow worker lanes by their swarmflow run.
 *
 * Lanes whose member name opens with a known run's slug (or the untracked
 * `wf-<12hex>-` pattern) collapse into one group per run; everything else —
 * main agent, leader, plain members, subagents — stays in `ungrouped` in the
 * original order. Groups follow the first appearance of their lanes. A run
 * known to the store contributes its `name` (task name) and `status`; unknown
 * runs fall back to the short run id.
 */
export function groupTeamMemberLanesByWorkflowRun(
  lanes: readonly TeamMemberLaneModel[],
  runs: readonly WorkflowRunLaneSummary[],
): TeamTrajectoryWorkflowLaneGrouping {
  const runsBySlug = new Map<string, WorkflowRunLaneSummary>();
  for (const run of runs) {
    if (typeof run?.id !== 'string' || run.id === '') continue;
    const slug = slugifyRunId(run.id);
    if (slug !== '') runsBySlug.set(slug, run);
  }
  // Longest slug first so a run whose slug prefixes another run's slug still
  // claims only its own workers.
  const orderedSlugs = [...runsBySlug.keys()].sort((left, right) => right.length - left.length);

  const ungrouped: TeamMemberLaneModel[] = [];
  const groups: TeamTrajectoryWorkflowGroup[] = [];
  const groupByRunId = new Map<string, TeamTrajectoryWorkflowGroup>();
  const groupFor = (runId: string, run: WorkflowRunLaneSummary | undefined): TeamTrajectoryWorkflowGroup => {
    const existing = groupByRunId.get(runId);
    if (existing !== undefined) return existing;
    const shortId = shortWorkflowRunId(runId);
    const name = typeof run?.name === 'string' && run.name.trim() !== '' ? run.name : shortId;
    const group: TeamTrajectoryWorkflowGroup = {
      runId,
      name,
      shortId,
      status: workflowRunLaneStatus(run?.status),
      lanes: [],
    };
    groupByRunId.set(runId, group);
    groups.push(group);
    return group;
  };

  for (const lane of lanes) {
    const memberName = memberNameOfSubjectId(lane.subjectId);
    let runId: string | null = null;
    let run: WorkflowRunLaneSummary | undefined;
    if (memberName !== null) {
      for (const slug of orderedSlugs) {
        if (memberName.startsWith(`${slug}-`)) {
          run = runsBySlug.get(slug);
          runId = run?.id ?? null;
          break;
        }
      }
      if (runId === null) {
        const untracked = UNTRACKED_RUN_PATTERN.exec(memberName);
        if (untracked !== null) runId = `wf_${untracked[1]}`;
      }
    }
    if (runId === null) {
      ungrouped.push(lane);
      continue;
    }
    groupFor(runId, run).lanes.push(lane);
  }

  return { ungrouped, groups };
}

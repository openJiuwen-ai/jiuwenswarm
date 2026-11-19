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
  /** Member currently hosting the run (leader included); null/absent = no
   * team context (legacy events) — the group header renders no badge. */
  executor?: string | null;
}

export interface TeamTrajectoryWorkflowGroup {
  runId: string;
  name: string;
  shortId: string;
  status: TeamMemberLaneStatus;
  executor?: string | null;
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

/** Swarmflow worker identity as the team member events carry it. */
export interface SwarmflowWorkerIdentity {
  /** Engine member id: `wf_<runid>:<call key JSON>`. */
  memberId: string;
  /** The engine label the worker's member name was minted from. */
  name: string;
}

/**
 * Swarmflow worker identities among session team members: entries whose
 * member id carries the engine's `wf_<runid>:<call key>` shape.
 */
export function swarmflowIdentitiesOfMembers(
  members: readonly { member_id: string; name?: string }[],
): SwarmflowWorkerIdentity[] {
  const identities: SwarmflowWorkerIdentity[] = [];
  for (const member of members) {
    if (parseEngineSwarmflowMemberId(member.member_id) === null) continue;
    identities.push({ memberId: member.member_id, name: member.name ?? '' });
  }
  return identities;
}

/** Split a `wf_<runid>:<call key>` member id into its two parts. */
export function parseEngineSwarmflowMemberId(
  memberId: string,
): { runId: string; callKey: string } | null {
  if (!memberId.startsWith('wf_')) return null;
  const colon = memberId.indexOf(':');
  if (colon === -1) return null;
  const runId = memberId.slice(0, colon);
  const callKey = memberId.slice(colon + 1);
  if (runId === '' || callKey === '') return null;
  return { runId, callKey };
}

/**
 * The worker member name the backend mints for an engine identity:
 * ``{slug(run_id)}-{slug(label)}-{ident}`` (TeamWorkerBackend._next_member_name,
 * where ``ident = sha256(call_key)[:12]``). The label slug mirrors the backend's
 * `_SLUG_RE` rules, which `slugifyRunId` already implements.
 */
export function swarmflowWorkerMemberName(
  runId: string,
  label: string,
  ident: string,
): string {
  const labelSlug = slugifyRunId(label) || 'worker';
  return `${slugifyRunId(runId)}-${labelSlug}-${ident}`;
}

/**
 * The hover "full name" of a lane: the worker's engine member id
 * (``wf_<runid>:[["par", 0, 0], ["call", 0]]`` — which branch and call the
 * operator is) once its worker member name is known, else the plain worker
 * member name; `null` for non-worker subjects so the caller keeps the label.
 */
export function swarmflowLaneFullName(
  lane: TeamMemberLaneModel,
  memberIdByWorkerName: ReadonlyMap<string, string>,
): string | null {
  const memberName = memberNameOfSubjectId(lane.subjectId);
  if (memberName === null) return null;
  const engineMemberId = memberIdByWorkerName.get(memberName);
  if (engineMemberId !== undefined) return engineMemberId;
  return UNTRACKED_RUN_PATTERN.test(memberName) ? memberName : null;
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
      // Run attribution rides the store's run summary: a known run carries its
      // executor (empty string / null normalize to undefined so the header
      // badge hides); an untracked run (archive replay, pattern match only)
      // has no summary and therefore no attribution.
      executor: typeof run?.executor === 'string' && run.executor.trim() !== ''
        ? run.executor
        : undefined,
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

export interface WorkflowGroupHostPlan {
  /** Groups keyed by the host lane object they matched (issue #6381). */
  attached: Map<TeamMemberLaneModel, TeamTrajectoryWorkflowGroup[]>;
  /** Groups whose executor matched no host lane. */
  trailing: TeamTrajectoryWorkflowGroup[];
}

/**
 * Attach each workflow run group under the lane of the member who executed it.
 *
 * Host lanes are the standalone member lanes plus the member lanes nested
 * inside scheduled task-run rows — a task assignee's lane lives there, not in
 * `ungrouped`. The map is keyed by the host lane object, not its subjectId: a
 * scheduled member can own several interleaved (non-adjacent) task runs whose
 * member lanes share one subjectId, and the group must render in the one lane
 * it attached to, never in every lane with that subjectId (AI review CR-001
 * on !10269/!10271). Consumers therefore look the map up with the very lane
 * object they render.
 */
export function attachWorkflowGroupsToHostLanes(
  workflowGroups: readonly TeamTrajectoryWorkflowGroup[],
  hostLanes: readonly TeamMemberLaneModel[],
): WorkflowGroupHostPlan {
  const attached = new Map<TeamMemberLaneModel, TeamTrajectoryWorkflowGroup[]>();
  const trailing: TeamTrajectoryWorkflowGroup[] = [];
  for (const group of workflowGroups) {
    const executor = group.executor;
    const host = executor === undefined
      ? undefined
      : hostLanes.find(lane => (
        memberNameOfSubjectId(lane.subjectId) === executor
        || lane.label === executor
      ));
    if (host === undefined) {
      trailing.push(group);
      continue;
    }
    const hosted = attached.get(host);
    if (hosted === undefined) attached.set(host, [group]);
    else hosted.push(group);
  }
  return { attached, trailing };
}

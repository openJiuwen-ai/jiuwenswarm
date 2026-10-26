/**
 * Team-task event normalisation helper.
 *
 * Threads the backend `title_truncated` / `title_original_size` /
 * `content_truncated` / `content_original_size` flags from a raw `team.task`
 * event through to the store.
 *
 * Behaviour contract (see OpenSpec change `fix-team-task-card-duplicate`,
 * Task 3 §3.2): the boolean flags pass through ONLY when the raw value is
 * literally `true`; the numeric sizes pass through ONLY when the raw value is
 * a finite number. In every other case (missing, false, NaN, Infinity) the
 * field is left `undefined` so a downstream `flag ?? existing.flag` merge
 * preserves whatever a prior created/updated event set — a status-only event
 * must NEVER reset the flags.
 *
 * Type-only imports keep the compiled module side-effect free.
 */
import type { TeamTaskStatus, TeamTaskUpsert } from './sessionStore';

const TEAM_TASK_STATUS_SET = new Set<TeamTaskStatus>([
  'pending',
  'blocked',
  'planning',
  'in_progress',
  'in_review',
  'completed',
  'cancelled',
]);

function normalizeTeamTaskStatus(
  status: unknown,
  fallback: TeamTaskStatus = 'pending'
): TeamTaskStatus {
  return typeof status === 'string' && TEAM_TASK_STATUS_SET.has(status as TeamTaskStatus)
    ? (status as TeamTaskStatus)
    : fallback;
}

function pickString(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value;
    }
  }
  return undefined;
}

function normalizeStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const normalized = value.filter(
    (item): item is string => typeof item === 'string' && item.trim().length > 0
  );
  return normalized.length ? normalized : undefined;
}

/**
 * Passes a boolean truncation flag through ONLY when the raw value is `true`.
 * Anything else (missing / false / non-boolean) -> `undefined`, so a downstream
 * `flag ?? existing.flag` keeps the prior value rather than clobbering to false.
 */
function pickTruncationFlag(value: unknown): true | undefined {
  return value === true ? true : undefined;
}

/**
 * Passes an original-size number through ONLY when it is a finite number.
 * NaN / Infinity / non-number -> `undefined` (same `?? existing` rationale).
 */
function pickFiniteSize(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

export function normalizeTaskEvent(value: unknown): TeamTaskUpsert | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const raw = value as Record<string, unknown>;
  const taskId = pickString(raw.task_id);
  if (!taskId) {
    return null;
  }
  // Status is resolved server-side (swarm layer) and read directly here — the
  // frontend no longer derives it from the event type. An absent status means
  // "no change"; the store preserves the task's existing status.
  const rawStatus = pickString(raw.status);
  return {
    task_id: taskId,
    title: pickString(raw.title, raw.name, raw.description),
    content: pickString(raw.content),
    status: rawStatus ? normalizeTeamTaskStatus(rawStatus) : undefined,
    assignee: pickString(
      raw.assignee,
      raw.member_id,
      raw.claimed_by,
      raw.claimedBy,
      raw.from_member
    ),
    team_id: pickString(raw.team_id),
    timestamp: typeof raw.timestamp === 'number' ? raw.timestamp : Date.now(),
    skills: normalizeStringArray(raw.skills),
    files: normalizeStringArray(raw.files),
    title_truncated: pickTruncationFlag(raw.title_truncated),
    title_original_size: pickFiniteSize(raw.title_original_size),
    content_truncated: pickTruncationFlag(raw.content_truncated),
    content_original_size: pickFiniteSize(raw.content_original_size),
  };
}

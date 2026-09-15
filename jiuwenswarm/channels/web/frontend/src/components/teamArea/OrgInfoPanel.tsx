/**
 * 组织信息面板 —— 界面右侧展示 task pool 与 organization 的内部信息。
 *
 * 数据来自后端 `org.snapshot` RPC（见 jiuwenswarm/server/agent_ws_server.py 的
 * `_handle_org_snapshot`）：organization 就是 openjiuwen 的 OrganizationSpec
 * （organization_id / display_name / owner_team_id / leaders / metadata），
 * tasks 是 OrgTask.brief() 的列表。
 *
 * 这里刻意不做轮询：org 快照只在"选中的 team 变了"和用户手动刷新时才拉，
 * 因为 task pool 的实时变动已经由 team.task 事件流覆盖了任务板，再叠一层定时
 * 请求只是白烧配额。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { RefreshCw } from 'lucide-react';
import clsx from 'clsx';
import { webClient } from '../../services/webClient';
import { useChatStore, useTeamSelectorStore } from '../../stores';

interface OrgTeamHandle {
  organization_id?: string | null;
  team_id?: string | null;
  capabilities?: string[];
}

interface OrgSpec {
  organization_id?: string | null;
  display_name?: string | null;
  description?: string | null;
  owner_team_id?: string | null;
  leaders?: OrgTeamHandle[];
  metadata?: Record<string, unknown>;
}

interface OrgTaskBrief {
  task_id?: string;
  parent_task_id?: string | null;
  root_task_id?: string | null;
  title?: string | null;
  description?: string | null;
  status?: string | null;
  task_type?: string | null;
  assignment?: Record<string, unknown> | null;
  required_capabilities?: string[];
  updated_at?: number | string | null;
}

interface PendingReviewEntry {
  review?: Record<string, unknown> | null;
  task?: OrgTaskBrief | null;
}

interface OrgSnapshotPayload {
  organization?: OrgSpec | null;
  tasks?: OrgTaskBrief[];
  unclaimed_tasks?: OrgTaskBrief[];
  pending_reviews?: PendingReviewEntry[];
  stats?: Record<string, number> & { by_status?: Record<string, number> };
}

/** 任务状态 → 标签配色。后端 OrgTask.status 是 OPEN / CLAIMED / ... 这类大写枚举。 */
function statusTone(status: string): string {
  const value = status.toUpperCase();
  if (value.includes('DONE') || value.includes('COMPLET') || value.includes('VERIF')) {
    return 'bg-emerald-500/12 text-emerald-600';
  }
  if (value.includes('REVIEW')) return 'bg-amber-500/14 text-amber-600';
  if (value.includes('FAIL') || value.includes('ERROR') || value.includes('CANCEL')) {
    return 'bg-red-500/12 text-red-600';
  }
  if (value.includes('CLAIM') || value.includes('RUN') || value.includes('PROGRESS')) {
    return 'bg-blue-500/12 text-blue-600';
  }
  return 'bg-foreground/8 text-text-muted';
}

/** assignment 是个结构体，表格里只想知道"谁在干"，取几个常见字段兜底。 */
function assignmentLabel(task: OrgTaskBrief): string {
  const assignment = task.assignment;
  if (!assignment) return '';
  for (const key of ['team_id', 'team_name', 'assignee', 'member_id', 'leader_id']) {
    const value = assignment[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (Array.isArray(value) && value.length) return value.join(', ');
  }
  return '';
}

function formatUpdatedAt(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '';
  let ms: number;
  if (typeof value === 'number') {
    ms = value < 10_000_000_000 ? value * 1000 : value;
  } else {
    const parsed = Date.parse(value);
    if (Number.isNaN(parsed)) return String(value);
    ms = parsed;
  }
  return new Date(ms).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function InfoRow({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="flex min-w-0 items-baseline gap-2 text-xs">
      <span className="shrink-0 text-text-muted">{label}</span>
      <span className="min-w-0 break-all font-medium text-text" title={value}>
        {value}
      </span>
    </div>
  );
}

export interface OrgInfoPanelProps {
  className?: string;
}

export function OrgInfoPanel({ className }: OrgInfoPanelProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const selectedTeamId = useTeamSelectorStore((s) =>
    activeSessionId ? s.runtimes[activeSessionId]?.selectedTeamId ?? null : null,
  );
  const runtimeTeams = useTeamSelectorStore((s) =>
    activeSessionId ? s.runtimes[activeSessionId]?.teams : undefined,
  );

  const [snapshot, setSnapshot] = useState<OrgSnapshotPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeqRef = useRef(0);

  const load = useCallback(
    async (sessionId: string, teamId: string | null) => {
      const requestSeq = ++requestSeqRef.current;
      setSnapshot(null);
      setError(null);
      setLoading(true);
      try {
        const payload = await webClient.request<OrgSnapshotPayload>(
          'org.snapshot',
          { session_id: sessionId, team_id: teamId ?? undefined },
          { timeoutMs: 8000 },
        );
        if (requestSeq !== requestSeqRef.current) return;
        setSnapshot(payload ?? null);
      } catch (e) {
        if (requestSeq !== requestSeqRef.current) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (requestSeq === requestSeqRef.current) setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!activeSessionId) {
      requestSeqRef.current += 1;
      setSnapshot(null);
      setError(null);
      setLoading(false);
      return;
    }
    void load(activeSessionId, selectedTeamId);
  }, [activeSessionId, selectedTeamId, load]);

  const organization = snapshot?.organization ?? null;
  const tasks = snapshot?.tasks ?? [];
  const reviews = snapshot?.pending_reviews ?? [];
  const stats = snapshot?.stats ?? {};
  const runtimeTeamById = useMemo(
    () => new Map((runtimeTeams ?? []).map((team) => [team.team_id, team])),
    [runtimeTeams],
  );

  const statCards = useMemo(
    () => [
      { key: 'total', label: t('team.org.statsTotal'), value: stats.total ?? tasks.length },
      { key: 'open', label: t('team.org.statsOpen'), value: stats.open ?? 0 },
      { key: 'unclaimed', label: t('team.org.statsUnclaimed'), value: stats.unclaimed ?? 0 },
      { key: 'reviews', label: t('team.org.statsPendingReviews'), value: stats.pending_reviews ?? reviews.length },
    ],
    [reviews.length, stats.open, stats.pending_reviews, stats.total, stats.unclaimed, t, tasks.length],
  );

  if (!activeSessionId) return null;

  return (
    <div
      className={clsx('flex h-full min-h-0 flex-col overflow-hidden bg-card', className)}
      data-testid="org-info-panel"
    >
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-text" data-testid="org-info-title">
            {t('team.org.title')}
          </div>
          <div className="truncate text-xs text-text-muted" data-testid="org-info-org-name">
            {organization?.display_name?.trim() ||
              organization?.organization_id ||
              t('team.org.noOrganization')}
          </div>
        </div>
        <button
          type="button"
          className="shrink-0 rounded p-1.5 text-text-muted hover:bg-secondary hover:text-text disabled:opacity-50"
          data-testid="org-info-refresh"
          title={t('team.org.refresh')}
          disabled={loading}
          onClick={() => void load(activeSessionId, selectedTeamId)}
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : undefined} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3" data-testid="org-info-body">
        {!organization && !loading && (
          <div className="py-6 text-center text-xs text-text-muted" data-testid="org-info-empty">
            {t('team.org.empty')}
          </div>
        )}

        {error && (
          <div className="mb-3 rounded border border-red-500/30 bg-red-500/5 px-2 py-1.5 text-xs text-red-600">
            {error}
          </div>
        )}

        {organization && (
          <section className="mb-4 space-y-1.5" data-testid="org-info-organization">
            <InfoRow label={t('team.org.organizationId')} value={organization.organization_id} />
            <InfoRow label={t('team.org.displayName')} value={organization.display_name} />
            <InfoRow label={t('team.org.ownerTeam')} value={organization.owner_team_id} />
            {organization.description?.trim() && (
              <div className="pt-1 text-xs leading-relaxed text-text-muted">
                {organization.description.trim()}
              </div>
            )}

            {(organization.leaders?.length ?? 0) > 0 && (
              <div className="pt-2" data-testid="org-info-teams">
                <div className="mb-1.5 text-xs font-medium text-text-muted">
                  {t('team.org.teams')}
                </div>
                <div className="space-y-1">
                  {organization.leaders!.map((team, index) => {
                    const runtimeTeam = team.team_id
                      ? runtimeTeamById.get(team.team_id)
                      : undefined;
                    const teamName =
                      team.team_id ||
                      runtimeTeam?.team_name?.trim() ||
                      '-';
                    const capabilities =
                      team.capabilities?.length
                        ? team.capabilities
                        : runtimeTeam?.capabilities ?? [];
                    return (
                      <div
                        key={team.team_id ?? index}
                        className="rounded border border-border bg-secondary/40 px-2 py-1.5"
                        data-testid="org-info-team-row"
                      >
                        <div className="truncate text-xs font-medium text-text" title={teamName}>
                          {teamName}
                        </div>
                        {capabilities.length > 0 && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {capabilities.map((capability) => (
                              <span
                                key={capability}
                                className="rounded bg-foreground/8 px-1.5 py-0.5 text-[10px] text-text-muted"
                              >
                                {capability}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </section>
        )}

        <section className="mb-4" data-testid="org-info-stats">
          <div className="grid grid-cols-2 gap-2">
            {statCards.map((card) => (
              <div
                key={card.key}
                className="rounded border border-border bg-secondary/40 px-2 py-1.5"
                data-testid="org-info-stat-card"
                data-variant={card.key}
              >
                <div className="text-[10px] text-text-muted">{card.label}</div>
                <div className="text-sm font-semibold text-text">{card.value}</div>
              </div>
            ))}
          </div>
        </section>

        <section data-testid="org-info-task-pool">
          <div className="mb-1.5 flex items-baseline justify-between">
            <span className="text-xs font-medium text-text-muted">{t('team.org.taskPool')}</span>
            <span className="text-[10px] text-text-muted">{tasks.length}</span>
          </div>

          {tasks.length === 0 ? (
            <div className="py-3 text-center text-xs text-text-muted" data-testid="org-info-task-pool-empty">
              {t('team.org.taskPoolEmpty')}
            </div>
          ) : (
            <div className="space-y-1.5">
              {tasks.map((task) => (
                <div
                  key={task.task_id}
                  className="rounded border border-border px-2 py-1.5"
                  data-testid="org-info-task-row"
                  data-task-id={task.task_id}
                  data-task-status={task.status ?? ''}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span
                      className="min-w-0 flex-1 truncate text-xs font-medium text-text"
                      title={task.description || task.title || task.task_id}
                    >
                      {task.title?.trim() || task.task_id}
                    </span>
                    {task.status && (
                      <span
                        className={clsx(
                          'shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium',
                          statusTone(task.status),
                        )}
                        data-testid="org-info-task-status"
                      >
                        {task.status}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-text-muted">
                    <span className="font-mono" data-testid="org-info-task-id">
                      {task.task_id}
                    </span>
                    {task.task_type && <span>{task.task_type}</span>}
                    {assignmentLabel(task) && (
                      <span data-testid="org-info-task-assignee">{assignmentLabel(task)}</span>
                    )}
                    {task.parent_task_id && (
                      <span>
                        {t('team.org.taskParent')}: {task.parent_task_id}
                      </span>
                    )}
                    {formatUpdatedAt(task.updated_at) && <span>{formatUpdatedAt(task.updated_at)}</span>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="mt-4" data-testid="org-info-pending-reviews">
          <div className="mb-1.5 flex items-baseline justify-between">
            <span className="text-xs font-medium text-text-muted">{t('team.org.pendingReviews')}</span>
            <span className="text-[10px] text-text-muted">{reviews.length}</span>
          </div>
          {reviews.length === 0 ? (
            <div className="py-2 text-center text-[11px] text-text-muted">
              {t('team.org.pendingReviewsEmpty')}
            </div>
          ) : (
            <div className="space-y-1">
              {reviews.map((entry, index) => {
                const task = entry.task;
                const reviewer =
                  typeof entry.review?.reviewer_member_name === 'string'
                    ? entry.review.reviewer_member_name
                    : typeof entry.review?.reviewer_id === 'string'
                      ? entry.review.reviewer_id
                      : '';
                return (
                  <div
                    key={`${task?.task_id ?? index}`}
                    className="flex items-baseline justify-between gap-2 rounded border border-border px-2 py-1 text-[11px]"
                    data-testid="org-info-pending-review-row"
                  >
                    <span className="min-w-0 flex-1 truncate text-text" title={task?.title ?? ''}>
                      {task?.title?.trim() || task?.task_id || '-'}
                    </span>
                    {reviewer && <span className="shrink-0 text-text-muted">{reviewer}</span>}
                  </div>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

export default OrgInfoPanel;

/** Organization task tree using the same compact disclosures as the SwarmFlow task view. */
import { useMemo, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { CheckCircle2, ChevronRight, CircleDot, CircleX, Clock3, FileText, Info, LoaderCircle, Users } from 'lucide-react';
import clsx from 'clsx';
import { useChatStore, useTeamSelectorStore } from '../../stores';
import { useOrganizationSnapshot } from '../../features/teamOrganization/useOrganizationSnapshot';
import { useTeamOrganizationUiEnabled } from '../../features/teamOrganization/featureConfig';
import {
  resolveOrganizationTeamIdentity,
  resolveOrganizationTeamLabel,
  useOrganizationGroupCatalog,
} from '../../features/teamOrganization/teamIdentity';
import { buildOrganizationTaskTree, buildOrganizationTeamTree, type OrganizationTask, type OrganizationTaskNode } from './organizationTree';

interface OrgTeamHandle {
<<<<<<< HEAD
  available?: boolean;
  organization_id?: string | null;
||||||| e487e9a30
  organization_id?: string | null;
=======
  available?: boolean;
>>>>>>> origin/develop
  team_id?: string | null;
  leader_id?: string | null;
  leader_member_name?: string | null;
  capabilities?: string[];
}
interface OrgSpec {
  organization_id?: string | null;
  display_name?: string | null;
  description?: string | null;
  owner_team_id?: string | null;
  leaders?: OrgTeamHandle[];
}
interface OrgSnapshotPayload {
  organization?: OrgSpec | null;
  tasks?: OrganizationTask[];
  pending_reviews?: { review?: Record<string, unknown> | null; task?: OrganizationTask | null }[];
}

function statusTone(status: string): string {
  switch (status.toUpperCase()) {
    case 'COMPLETED': return 'text-ok bg-ok-subtle';
    case 'FAILED': case 'CANCELLED': case 'EXPIRED': return 'text-danger bg-danger-subtle';
    case 'IN_PROGRESS': case 'CLAIMED': return 'text-info bg-info/10';
    case 'WAITING_SOURCES': case 'DELEGATED': return 'text-warn bg-warn-subtle';
    default: return 'text-text-muted bg-secondary';
  }
}
function TaskStatusIcon({ status }: { status?: string | null }) {
  const value = status?.toUpperCase() || '';
  const className = clsx('h-4 w-4 shrink-0', statusTone(value).split(' ')[0]);
  if (value === 'COMPLETED') return <CheckCircle2 className={className} aria-hidden="true" />;
  if (['FAILED', 'CANCELLED', 'EXPIRED'].includes(value)) return <CircleX className={className} aria-hidden="true" />;
  if (value === 'IN_PROGRESS') return <LoaderCircle className={clsx(className, 'organization-team__spinner')} aria-hidden="true" />;
  if (value === 'WAITING_SOURCES') return <Clock3 className={className} aria-hidden="true" />;
  return <CircleDot className={className} aria-hidden="true" />;
}
function InfoRow({ label, value, title, testId }: {
  label: string; value?: string | null; title?: string; testId?: string;
}) {
  if (!value) return null;
  return (
    <div className="organization-tree__field" data-testid={testId}>
      <span className="text-text-muted">{label}</span>
      <span className="min-w-0 whitespace-pre-wrap break-words text-text" title={title || value}>{value}</span>
    </div>
  );
}
function TreeDetails({ children, testId, variant }: { children: ReactNode; testId: string; variant?: string }) {
  const { t } = useTranslation();
  return (
    <details className="organization-tree__details" data-testid={testId} data-variant={variant}>
      <summary className="organization-tree__detail-trigger" data-testid={`${testId}-toggle`} data-variant={variant}>
        <Info size={12} aria-hidden="true" />{t('team.org.details')}
      </summary>
      <div className="organization-tree__fields">{children}</div>
    </details>
  );
}
function TaskNode({ node, teamLabel, teamAvatar, taskLabel, root = false }: {
  node: OrganizationTaskNode;
  teamLabel: (id: string) => string;
  teamAvatar: (id: string) => string | undefined;
  taskLabel: (id: string) => string;
  root?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const { task, children } = node;
  const teamId = typeof task.assignment?.team_id === 'string' ? task.assignment.team_id : '';
  const avatar = teamId ? teamAvatar(teamId) : undefined;
  const assignedBy = typeof task.assignment?.assigned_by_team_id === 'string' ? task.assignment.assigned_by_team_id : '';
  const status = task.status?.toUpperCase() || '';
  const stamp = task.updated_at;
  const date = stamp == null || stamp === '' ? null : new Date(typeof stamp === 'number' ? stamp * (stamp < 1e10 ? 1000 : 1) : stamp);
  const updatedAt = date && !Number.isNaN(date.getTime()) ? date.toLocaleString(i18n.language) : '';
  return (
    <details open className={clsx('organization-tree__node', root && 'organization-tree__root')}
      data-testid="team-area-org-info-task-row" data-variant={root ? 'root' : 'sub'}
      data-task-id={task.task_id} data-task-status={status}>
      <summary className="organization-tree__row" data-testid="team-area-org-task-toggle" data-variant={node.key}>
        <ChevronRight size={16} className="organization-tree__chevron" aria-hidden="true" />
        <TaskStatusIcon status={status} />
        <span className="min-w-0 flex-1 break-words text-sm font-medium text-text" data-testid="team-area-org-task-title" data-variant={node.key}>
          {task.title?.trim() || task.task_id || t('team.org.taskTitle')}
        </span>
        {teamId && (
          <span className="organization-tree__assignee" title={teamLabel(teamId)}>
            {avatar ? (
              <img src={avatar} alt="" className="h-5 w-5 shrink-0 rounded-full object-contain"
                data-testid="team-area-org-task-team-avatar" data-variant={node.key} />
            ) : <Users size={16} className="shrink-0" aria-hidden="true" />}
            <span className="min-w-0 truncate">{teamLabel(teamId)}</span>
          </span>
        )}
        {status && <span className={clsx('organization-tree__badge', statusTone(status))} data-testid="team-area-org-info-task-status">
          {t(`team.org.status.${status}`, { defaultValue: status })}
        </span>}
        {children.length > 0 && <span className="organization-tree__progress" data-testid="team-area-org-task-progress" data-variant={node.key}
          title={t('team.org.treeProgress', { completed: node.completed, total: node.total })}>
          <span className="organization-tree__track"><span style={{ width: `${node.completed / node.total * 100}%` }} /></span>
          {node.completed}/{node.total}
        </span>}
      </summary>
      <div className="organization-tree__node-body">
        <TreeDetails testId="team-area-org-info-task-details" variant={task.task_id}>
          <InfoRow label={t('team.org.descriptionExcerpt')} value={task.description} />
          <InfoRow label={t('team.org.taskAssignee')} value={teamId ? teamLabel(teamId) : t('team.org.taskUnclaimed')} testId="team-area-org-info-task-assignee" />
          <InfoRow label={t('team.org.assignedBy')} value={assignedBy ? teamLabel(assignedBy) : ''} />
          <InfoRow label={t('team.org.updatedAt')} value={updatedAt} />
          <InfoRow label={t('team.org.aggregationMode')} value={task.aggregation_mode
            ? t(`team.org.aggregation.${task.aggregation_mode}`, { defaultValue: task.aggregation_mode }) : ''} />
          <InfoRow label={t('team.org.failureReason')} value={task.failure_code
            ? t(`team.org.failure.${task.failure_code}`, { defaultValue: task.failure_code }) : ''} />
          <InfoRow label={t('team.org.taskParent')} value={task.parent_task_id ? taskLabel(task.parent_task_id) : ''} />
          <InfoRow label={t('team.org.rootTask')} value={task.root_task_id && task.root_task_id !== task.task_id ? taskLabel(task.root_task_id) : ''} />
          <InfoRow label={t('team.org.recreatedFrom')} value={task.recreated_from_task_id ? taskLabel(task.recreated_from_task_id) : ''} />
          <InfoRow label={t('team.org.taskType')} value={task.task_type} />
          <InfoRow label={t('team.org.taskId')} value={task.task_id} testId="team-area-org-info-task-id" />
        </TreeDetails>
        {children.length > 0 && <div className="organization-tree__children" data-testid="team-area-org-task-children" data-variant={node.key}>
          {children.map((child) => <div className="organization-tree__branch" key={child.key}>
            <TaskNode node={child} teamLabel={teamLabel} teamAvatar={teamAvatar} taskLabel={taskLabel} />
          </div>)}
        </div>}
      </div>
    </details>
  );
}

export interface OrgInfoPanelProps { className?: string }
export function OrgInfoPanel({ className }: OrgInfoPanelProps) {
  const { t } = useTranslation();
  const enabled = useTeamOrganizationUiEnabled();
  const sessionId = useChatStore((s) => s.activeSessionId);
  const teams = useTeamSelectorStore((s) => sessionId ? s.runtimes[sessionId]?.teams : undefined);
  const { snapshot, loading, error, noTeam } = useOrganizationSnapshot<OrgSnapshotPayload>(sessionId);
  const organization = snapshot?.organization;
  const groups = useOrganizationGroupCatalog(enabled && Boolean(organization));
  const tasks = snapshot?.tasks;
  const forest = useMemo(() => buildOrganizationTaskTree(tasks ?? []), [tasks]);
  const byTeam = useMemo(() => new Map((teams ?? []).map((team) => [team.team_id, team])), [teams]);
  const byTask = useMemo(() => new Map((tasks ?? []).filter((task) => task.task_id).map((task) => [task.task_id!, task])), [tasks]);
  const teamLabel = (id: string) => resolveOrganizationTeamLabel(byTeam.get(id), groups, id);
  const teamAvatar = (id: string): string | undefined =>
    resolveOrganizationTeamIdentity(byTeam.get(id), groups)?.avatarUrl ?? undefined;
  const taskLabel = (id: string) => byTask.get(id)?.title?.trim() || id;
  const leaders = [...(organization?.leaders ?? [])].sort((a, b) =>
    Number(b.team_id === organization?.owner_team_id) - Number(a.team_id === organization?.owner_team_id));
  const reviews = snapshot?.pending_reviews ?? [];
<<<<<<< HEAD
  const runtimeTeamById = useMemo(
    () => new Map((runtimeTeams ?? []).map((team) => [team.team_id, team])),
    [runtimeTeams],
  );

  const ownerTeam = organization?.owner_team_id ? runtimeTeamById.get(organization.owner_team_id) : undefined;
  const ownerName = resolveOrganizationTeamLabel(ownerTeam, groups, organization?.owner_team_id || '-');

  if (!organizationUiEnabled || !activeSessionId) return null;

  return (
    <div
      className={clsx('flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-card', className)}
      data-testid="team-area-org-info-panel"
    >
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-text" data-testid="team-area-org-info-title">
            {t('team.org.title')}
          </div>
          <div className="truncate text-xs text-text-muted" data-testid="team-area-org-info-org-name">
            {organization?.display_name?.trim() ||
              organization?.organization_id ||
              (error
                ? t('team.org.loadFailed')
                : loading
                  ? t('team.org.loading')
                  : noTeam
                    ? t('team.org.noTeam')
                    : snapshot
                      ? t('team.org.noOrganization')
                      : t('team.org.loading'))}
          </div>
        </div>
        <button
          type="button"
          className="shrink-0 rounded p-1.5 text-text-muted hover:bg-secondary hover:text-text disabled:opacity-50"
          data-testid="team-area-org-info-refresh"
          title={t('team.org.refresh')}
          disabled={loading}
          onClick={() => void refresh()}
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : undefined} />
        </button>
||||||| e487e9a30
  const runtimeTeamById = useMemo(
    () => new Map((runtimeTeams ?? []).map((team) => [team.team_id, team])),
    [runtimeTeams],
  );

  const ownerTeam = organization?.owner_team_id ? runtimeTeamById.get(organization.owner_team_id) : undefined;
  const ownerName =
    resolveOrganizationTeamIdentity(ownerTeam, groups)?.displayName ||
    ownerTeam?.agent_group_name?.trim() ||
    organization?.owner_team_id;

  if (!organizationUiEnabled || !activeSessionId) return null;

  return (
    <div
      className={clsx('flex h-full min-h-0 flex-col overflow-hidden bg-card', className)}
      data-testid="team-area-org-info-panel"
    >
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-text" data-testid="team-area-org-info-title">
            {t('team.org.title')}
          </div>
          <div className="truncate text-xs text-text-muted" data-testid="team-area-org-info-org-name">
            {organization?.display_name?.trim() ||
              organization?.organization_id ||
              (error
                ? t('team.org.loadFailed')
                : loading
                  ? t('team.org.loading')
                  : noTeam
                    ? t('team.org.noTeam')
                    : snapshot
                      ? t('team.org.noOrganization')
                      : t('team.org.loading'))}
          </div>
        </div>
        <button
          type="button"
          className="shrink-0 rounded p-1.5 text-text-muted hover:bg-secondary hover:text-text disabled:opacity-50"
          data-testid="team-area-org-info-refresh"
          title={t('team.org.refresh')}
          disabled={loading}
          onClick={() => void refresh()}
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : undefined} />
        </button>
=======
  const teamTree = buildOrganizationTeamTree(leaders, organization?.owner_team_id);
  const renderTeam = (leader: OrgTeamHandle, id: string) => {
    const runtime = byTeam.get(id);
    const identity = resolveOrganizationTeamIdentity(runtime, groups);
    const ownTasks = (tasks ?? []).filter((task) => task.assignment?.team_id === id);
    return <details className="organization-tree__node" data-testid="team-area-org-info-team-row" data-variant={organization?.owner_team_id === id ? 'owner' : 'sub'}>
      <summary className="organization-tree__row" data-testid="team-area-org-team-toggle" data-variant={id}>
        <ChevronRight size={16} className="organization-tree__chevron" aria-hidden="true" />
        {identity?.avatarUrl ? <img src={identity.avatarUrl} alt="" className="h-5 w-5 shrink-0 rounded-full object-contain" data-testid="team-area-org-info-team-avatar" data-variant={id} /> : <Users size={16} className="shrink-0 text-text-muted" aria-hidden="true" />}
        <span className="min-w-0 flex-1 text-sm text-text" data-testid="team-area-org-info-team-name" data-variant={id}>{teamLabel(id)}</span>
        {(leader.available === false || runtime?.available === false) && <span className="text-xs text-danger" data-testid="team-area-org-info-team-unavailable" data-variant={id}>{t('chat.teamSelect.unavailable')}</span>}
        <span className="text-xs tabular-nums text-text-muted">{ownTasks.length} {t('team.org.taskPool')}</span>
      </summary>
      <div className="organization-tree__fields">
        <InfoRow label={t('team.org.leadMember')} value={leader.leader_member_name || leader.leader_id || runtime?.leader_id} />
        <InfoRow label={t('team.org.capabilities')} value={(leader.capabilities?.length ? leader.capabilities : runtime?.capabilities)?.join(' · ')} />
        <InfoRow label={t('team.org.executionModel')} value={runtime?.execution_model_name} />
        <InfoRow label={t('team.org.taskTitle')} value={ownTasks.map((task) => task.title || task.task_id).join('\n')} />
        <InfoRow label={t('team.org.taskTeam')} value={leader.team_id} />
>>>>>>> origin/develop
      </div>
<<<<<<< HEAD

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3" data-testid="team-area-org-info-body">
        {(loading || noTeam) && !snapshot && !error && (
          <div
            className="py-6 text-center text-xs text-text-muted"
            data-testid="team-area-org-info-status"
            data-variant={noTeam ? 'no-team' : 'loading'}
          >
            {t(noTeam ? 'team.org.noTeam' : 'team.org.loading')}
          </div>
        )}
        {!organization && snapshot && !loading && !error && (
          <div className="py-6 text-center text-xs text-text-muted" data-testid="team-area-org-info-empty">
            {t('team.org.empty')}
          </div>
        )}

        {error && (
          <div
            className="mb-3 rounded border border-danger/30 bg-danger-subtle px-2 py-1.5 text-xs text-danger"
            data-testid="team-area-org-info-error"
          >
            {error}
          </div>
        )}

        {organization && (
          <section className="mb-4 space-y-1.5" data-testid="team-area-org-info-organization">
            <InfoRow label={t('team.org.organizationId')} value={organization.organization_id} />
            <InfoRow label={t('team.org.displayName')} value={organization.display_name} />
            <InfoRow
              label={t('team.org.ownerTeam')}
              value={ownerName}
              title={organization.owner_team_id || undefined}
              testId="team-area-org-info-owner-team"
            />
            {organization.description?.trim() && (
              <div className="pt-1 text-xs leading-relaxed text-text-muted">{organization.description.trim()}</div>
            )}

            {(organization.leaders?.length ?? 0) > 0 && (
              <div className="pt-2" data-testid="team-area-org-info-teams">
                <div className="mb-1.5 text-xs font-medium text-text-muted">{t('team.org.teams')}</div>
                <div className="space-y-1">
                  {organization.leaders!.map((team, index) => {
                    const runtimeTeam = team.team_id ? runtimeTeamById.get(team.team_id) : undefined;
                    const teamId = team.team_id || runtimeTeam?.team_name?.trim() || '-';
                    const identity = resolveOrganizationTeamIdentity(runtimeTeam, groups);
                    const teamName = resolveOrganizationTeamLabel(runtimeTeam, groups, teamId);
                    const capabilities = team.capabilities?.length
                      ? team.capabilities
                      : (runtimeTeam?.capabilities ?? []);
                    return (
                      <div
                        key={team.team_id ?? index}
                        className="rounded border border-border bg-secondary/40 px-2 py-1.5"
                        data-testid="team-area-org-info-team-row"
                      >
                        <div className="flex items-center gap-2 text-xs font-medium text-text" title={teamName}>
                          {identity?.avatarUrl && (
                            <img
                              src={identity.avatarUrl}
                              alt=""
                              className="h-5 w-5 shrink-0 rounded object-contain"
                              data-testid="team-area-org-info-team-avatar"
                              data-variant={teamId}
                            />
                          )}
                          <span className="truncate" data-testid="team-area-org-info-team-name" data-variant={teamId}>
                            {teamName}
                          </span>
                          {(team.available === false || runtimeTeam?.available === false) && (
                            <span className="text-xs text-danger" data-testid="team-area-org-info-team-unavailable" data-variant={teamId}>
                              {t('chat.teamSelect.unavailable')}
                            </span>
                          )}
                        </div>
                        {teamName !== teamId && (
                          <div className="truncate text-[10px] text-text-muted" title={teamId}>
                            {teamId}
                          </div>
                        )}
                        {capabilities.length > 0 && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {capabilities.map((capability) => (
                              <span
                                key={capability}
                                className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-text-muted"
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
||||||| e487e9a30

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3" data-testid="team-area-org-info-body">
        {(loading || noTeam) && !snapshot && !error && (
          <div
            className="py-6 text-center text-xs text-text-muted"
            data-testid="team-area-org-info-status"
            data-variant={noTeam ? 'no-team' : 'loading'}
          >
            {t(noTeam ? 'team.org.noTeam' : 'team.org.loading')}
          </div>
        )}
        {!organization && snapshot && !loading && !error && (
          <div className="py-6 text-center text-xs text-text-muted" data-testid="team-area-org-info-empty">
            {t('team.org.empty')}
          </div>
        )}

        {error && (
          <div
            className="mb-3 rounded border border-danger/30 bg-danger-subtle px-2 py-1.5 text-xs text-danger"
            data-testid="team-area-org-info-error"
          >
            {error}
          </div>
        )}

        {organization && (
          <section className="mb-4 space-y-1.5" data-testid="team-area-org-info-organization">
            <InfoRow label={t('team.org.organizationId')} value={organization.organization_id} />
            <InfoRow label={t('team.org.displayName')} value={organization.display_name} />
            <InfoRow
              label={t('team.org.ownerTeam')}
              value={ownerName}
              title={organization.owner_team_id || undefined}
              testId="team-area-org-info-owner-team"
            />
            {organization.description?.trim() && (
              <div className="pt-1 text-xs leading-relaxed text-text-muted">{organization.description.trim()}</div>
            )}

            {(organization.leaders?.length ?? 0) > 0 && (
              <div className="pt-2" data-testid="team-area-org-info-teams">
                <div className="mb-1.5 text-xs font-medium text-text-muted">{t('team.org.teams')}</div>
                <div className="space-y-1">
                  {organization.leaders!.map((team, index) => {
                    const runtimeTeam = team.team_id ? runtimeTeamById.get(team.team_id) : undefined;
                    const teamId = team.team_id || runtimeTeam?.team_name?.trim() || '-';
                    const identity = resolveOrganizationTeamIdentity(runtimeTeam, groups);
                    const teamName = identity?.displayName || runtimeTeam?.agent_group_name?.trim() || teamId;
                    const capabilities = team.capabilities?.length
                      ? team.capabilities
                      : (runtimeTeam?.capabilities ?? []);
                    return (
                      <div
                        key={team.team_id ?? index}
                        className="rounded border border-border bg-secondary/40 px-2 py-1.5"
                        data-testid="team-area-org-info-team-row"
                      >
                        <div className="flex items-center gap-2 text-xs font-medium text-text" title={teamName}>
                          {identity?.avatarUrl && (
                            <img
                              src={identity.avatarUrl}
                              alt=""
                              className="h-5 w-5 shrink-0 rounded object-contain"
                              data-testid="team-area-org-info-team-avatar"
                              data-variant={teamId}
                            />
                          )}
                          <span className="truncate" data-testid="team-area-org-info-team-name" data-variant={teamId}>
                            {teamName}
                          </span>
                        </div>
                        {teamName !== teamId && (
                          <div className="truncate text-[10px] text-text-muted" title={teamId}>
                            {teamId}
                          </div>
                        )}
                        {capabilities.length > 0 && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {capabilities.map((capability) => (
                              <span
                                key={capability}
                                className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-text-muted"
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
=======
    </details>;
  };
  if (!enabled || !sessionId) return null;
  return (
    <div className={clsx('flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-card', className)} data-testid="team-area-org-info-panel">
      <div className="organization-tree min-h-0 flex-1 overflow-y-auto px-6 pb-6" data-testid="team-area-org-info-body" key={`${sessionId}:${organization?.organization_id ?? ''}`}>
        {(loading || noTeam) && !snapshot && !error && <div className="py-6 text-center text-xs text-text-muted"
          data-testid="team-area-org-info-status" data-variant={noTeam ? 'no-team' : 'loading'}>{t(noTeam ? 'team.org.noTeam' : 'team.org.loading')}</div>}
        {!organization && snapshot && !loading && !error && <div className="py-6 text-center text-xs text-text-muted" data-testid="team-area-org-info-empty">{t('team.org.empty')}</div>}
        {error && <div className="my-3 rounded border border-danger/30 bg-danger-subtle p-2 text-xs text-danger" data-testid="team-area-org-info-error">{error}</div>}
        {snapshot && <>
          <section data-testid="team-area-org-info-task-pool">
            <div className="my-6 flex h-8 items-center justify-between gap-3">
              <h2 className="text-sm font-semibold leading-5 text-text-strong" data-testid="team-area-org-task-heading">{t('team.org.taskTree')}</h2>
              <span className="text-xs tabular-nums text-text-muted" data-testid="team-area-org-task-count">{t('team.org.treeProgress', {
                completed: (tasks ?? []).filter((task) => task.status?.toUpperCase() === 'COMPLETED').length, total: tasks?.length ?? 0,
              })}</span>
            </div>
            {forest.length ? <div className="space-y-3">{forest.map((node) => <TaskNode key={node.key} node={node} teamLabel={teamLabel} teamAvatar={teamAvatar} taskLabel={taskLabel} root />)}</div>
              : <div className="py-6 text-center text-xs text-text-muted" data-testid="team-area-org-info-task-pool-empty"><FileText size={24} className="mx-auto mb-2" aria-hidden="true" />{t('team.org.taskPoolEmpty')}</div>}
>>>>>>> origin/develop
          </section>
          <section className="mt-6" data-testid="team-area-org-info-pending-reviews">
            <h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-text-muted" data-testid="team-area-org-review-heading"><Clock3 size={14} aria-hidden="true" />{t('team.org.pendingReviews')}<span>{reviews.length}</span></h3>
            {reviews.length ? reviews.map((entry, index) => {
              const reviewer = entry.review?.reviewer_team_id ?? entry.review?.reviewer_member_name ?? entry.review?.reviewer_id;
              const changes = Array.isArray(entry.review?.required_changes) ? entry.review.required_changes.filter((value): value is string => typeof value === 'string') : [];
              return <details className="organization-tree__review" key={String(entry.review?.review_id || entry.task?.task_id || index)} data-testid="team-area-org-info-pending-review-row">
                <summary className="organization-tree__row" data-testid="team-area-org-review-toggle" data-variant={String(entry.review?.review_id || entry.task?.task_id || index)}>
                  <ChevronRight size={16} className="organization-tree__chevron" aria-hidden="true" />
                  <Clock3 size={14} className="shrink-0 text-warn" aria-hidden="true" />
                  <span className="min-w-0 flex-1 text-sm text-text">{entry.task?.title || entry.task?.task_id || t('team.org.taskTitle')}</span>
                  <span className="text-xs text-text-muted">{typeof reviewer === 'string' ? teamLabel(reviewer) : ''}</span>
                </summary>
                <div className="organization-tree__fields"><InfoRow label={t('team.org.reviewReviewer')} value={typeof reviewer === 'string' ? teamLabel(reviewer) : ''} /><InfoRow label={t('team.org.requiredChanges')} value={changes.join('\n')} /><InfoRow label={t('team.org.taskId')} value={entry.task?.task_id} /></div>
              </details>;
            }) : <p className="py-2 text-xs text-text-muted" data-testid="team-area-org-review-empty">{t('team.org.pendingReviewsEmpty')}</p>}
          </section>
        </>}
        {organization && <details className="organization-tree__organization mt-6" data-testid="team-area-org-info-details">
          <summary className="organization-tree__row" data-testid="team-area-org-info-details-toggle"><ChevronRight size={16} className="organization-tree__chevron" aria-hidden="true" /><Info size={14} className="shrink-0 text-text-muted" aria-hidden="true" /><span className="text-xs text-text-muted">{t('team.org.details')}</span><span className="ml-auto text-xs text-text-muted">{leaders.length} {t('team.org.teams')}</span></summary>
          <section className="organization-tree__fields" data-testid="team-area-org-info-organization">
            <InfoRow label={t('team.org.displayName')} value={organization.display_name?.trim() || organization.organization_id} testId="team-area-org-info-org-name" />
            <InfoRow label={t('team.org.description')} value={organization.description} />
            <InfoRow label={t('team.org.ownerTeam')} value={organization.owner_team_id ? teamLabel(organization.owner_team_id) : ''} title={organization.owner_team_id || undefined} testId="team-area-org-info-owner-team" />
            <InfoRow label={t('team.org.organizationId')} value={organization.organization_id} />
          </section>
          <div className="organization-tree__children" data-testid="team-area-org-info-teams">
            {teamTree.map((node) => <div className="organization-tree__branch" key={node.key}>
              {renderTeam(node.team, node.key)}
              {node.children.length > 0 && <div className="organization-tree__children" data-testid="team-area-org-info-subteams" data-variant={node.key}>
                {node.children.map((child) => <div className="organization-tree__branch" key={child.key}>
                  {renderTeam(child.team, child.key)}
                </div>)}
              </div>}
            </div>)}
          </div>
        </details>}
      </div>
    </div>
  );
}
export default OrgInfoPanel;

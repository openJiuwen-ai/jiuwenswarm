import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import {
  getTeamQuota,
  listTeams,
  stopTeam,
  type TeamEntity,
  type TeamQuotaSnapshot,
  type TeamStatus,
} from '../../services/teamApi';
import { isEnterprise } from '../../edition';
import { toDisplaySessionTitle } from '../../utils/documentMessage';

interface SessionListResponse {
  sessions?: unknown[];
}

interface TeamSessionRef {
  sessionId: string;
  title: string;
  lastMessageAt: number;
  createdAt: number;
}

interface TeamInfo {
  name: string;
  sessionCount: number;
  sessionTitle?: string;
  createdAt?: number;
  sessions?: TeamSessionRef[];
  teamId?: string;
  status?: TeamStatus;
  sessionId?: string;
  memberCount?: number;
}

interface TeamDeleteResponse {
  session_ids: string[];
}

interface TeamPanelProps {
  onSessionsDeleted?: (sessionIds: string[]) => void | Promise<void>;
}

const STOPPABLE_STATUSES: ReadonlySet<TeamStatus> = new Set(['creating', 'running', 'paused']);

function isListedTeamMode(mode: string): boolean {
  return mode === 'team' || mode.startsWith('team.');
}

function asTimestampMs(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    return value > 1e12 ? value : value * 1000;
  }
  if (typeof value === 'string' && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric) && numeric > 0) {
      return numeric > 1e12 ? numeric : numeric * 1000;
    }
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  }
  return 0;
}

function localClock(timestampMs: number): { year: string; month: string; day: string; hour: string; minute: string } | null {
  if (!timestampMs) return null;
  const date = new Date(timestampMs);
  if (Number.isNaN(date.getTime())) return null;
  const pad = (part: number) => String(part).padStart(2, '0');
  return {
    year: String(date.getFullYear()),
    month: pad(date.getMonth() + 1),
    day: pad(date.getDate()),
    hour: pad(date.getHours()),
    minute: pad(date.getMinutes()),
  };
}

function formatCreatedMinute(timestampMs: number): string {
  const clock = localClock(timestampMs);
  if (!clock) return '';
  return `${clock.month}-${clock.day} ${clock.hour}:${clock.minute}`;
}

function createdMinuteKey(timestampMs: number): string {
  const clock = localClock(timestampMs);
  if (!clock) return '';
  return `${clock.year}-${clock.month}-${clock.day} ${clock.hour}:${clock.minute}`;
}

function sessionSuffix(sessionId: string | undefined): string {
  const id = sessionId?.trim() ?? '';
  return id ? id.slice(-6) : '';
}

function teamKey(team: TeamInfo): string {
  return team.teamId || team.name;
}

function teamsNeedingSessionSuffix(teams: TeamInfo[]): Set<string> {
  const groups = new Map<string, string[]>();
  for (const team of teams) {
    const title = team.sessionTitle?.trim() || team.sessionId?.trim() || '';
    const key = `${title}\0${createdMinuteKey(team.createdAt ?? 0)}`;
    const id = teamKey(team);
    groups.set(key, [...(groups.get(key) ?? []), id]);
  }
  const needs = new Set<string>();
  for (const ids of groups.values()) {
    if (ids.length > 1) ids.forEach((id) => needs.add(id));
  }
  return needs;
}

function listedSession(rec: Record<string, unknown>): TeamSessionRef | null {
  const sessionId = typeof rec.session_id === 'string' ? rec.session_id.trim() : '';
  if (!sessionId) return null;
  const rawTitle = typeof rec.title === 'string' ? rec.title : '';
  const lastMessageAt = typeof rec.last_message_at === 'number' ? rec.last_message_at : 0;
  return {
    sessionId,
    title: toDisplaySessionTitle(rawTitle),
    lastMessageAt,
    createdAt: asTimestampMs(rec.created_at),
  };
}

function applyChosenSession(team: TeamInfo, preferredSessionId?: string): TeamInfo {
  const sessions = team.sessions ?? [];
  const preferred = preferredSessionId
    ? sessions.find((item) => item.sessionId === preferredSessionId)
    : undefined;
  const titled = sessions
    .filter((item) => item.title)
    .sort((a, b) => b.lastMessageAt - a.lastMessageAt);
  const chosen = preferred?.title ? preferred : (titled[0] ?? preferred ?? sessions[0]);
  return {
    ...team,
    sessionId: preferredSessionId || chosen?.sessionId || team.sessionId,
    sessionTitle: chosen?.title || '',
    createdAt: chosen?.createdAt || 0,
  };
}

function sortTeams(teams: TeamInfo[]): TeamInfo[] {
  return [...teams].sort(
    (a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0) || a.name.localeCompare(b.name),
  );
}

function toTeamInfos(raw: unknown[]): TeamInfo[] {
  const byName = new Map<string, TeamInfo>();
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const mode = typeof rec.mode === 'string' ? rec.mode : '';
    const teamName = typeof rec.team_name === 'string' ? rec.team_name.trim() : '';
    if (!isListedTeamMode(mode) || !teamName) continue;
    const existing = byName.get(teamName) ?? { name: teamName, sessionCount: 0, sessions: [] };
    existing.sessionCount += 1;
    const session = listedSession(rec);
    if (session) existing.sessions = [...(existing.sessions ?? []), session];
    byName.set(teamName, existing);
  }
  return sortTeams(Array.from(byName.values()).map((team) => applyChosenSession(team)));
}

function mergeControlPlaneTeams(base: TeamInfo[], entities: TeamEntity[]): TeamInfo[] {
  if (entities.length === 0) return base;
  const byName = new Map(base.map((team) => [team.name, { ...team }]));
  for (const entity of entities) {
    if (!entity?.team_name || entity.status === 'deleted') continue;
    const existing = byName.get(entity.team_name) ?? { name: entity.team_name, sessionCount: 0, sessions: [] };
    existing.teamId = entity.team_id;
    existing.status = entity.status;
    existing.memberCount = Array.isArray(entity.memberships) ? entity.memberships.length : undefined;
    if (existing.sessionCount === 0 && entity.team_session_id) existing.sessionCount = 1;
    const chosen = applyChosenSession(existing, entity.team_session_id || existing.sessionId);
    existing.sessionId = chosen.sessionId;
    existing.sessionTitle = chosen.sessionTitle;
    existing.createdAt = chosen.createdAt;
    byName.set(entity.team_name, existing);
  }
  return sortTeams(Array.from(byName.values()));
}

export function TeamPanel({ onSessionsDeleted }: TeamPanelProps) {
  const { t } = useTranslation();
  const enterprise = isEnterprise();
  const [teams, setTeams] = useState<TeamInfo[]>([]);
  const [quota, setQuota] = useState<TeamQuotaSnapshot | null>(null);
  const [quotaError, setQuotaError] = useState('');
  const [loadingTeams, setLoadingTeams] = useState(true);
  const [deletingTeamName, setDeletingTeamName] = useState('');
  const [stoppingTeamId, setStoppingTeamId] = useState('');
  const [error, setError] = useState<string | null>(null);

  const deletingTeam = Boolean(deletingTeamName);

  const loadTeams = useCallback(async () => {
    setLoadingTeams(true);
    try {
      const payload = await webRequest<SessionListResponse>('session.list', {});
      let nextTeams = Array.isArray(payload?.sessions) ? toTeamInfos(payload.sessions) : [];
      try {
        nextTeams = mergeControlPlaneTeams(nextTeams, await listTeams());
      } catch (controlError) {
        console.debug('[teams] control-plane list unavailable:', controlError);
      }
      if (enterprise) {
        try {
          setQuota(await getTeamQuota());
          setQuotaError('');
        } catch (quotaLoadError) {
          console.debug('[teams] quota snapshot unavailable:', quotaLoadError);
          setQuota(null);
          const message = quotaLoadError instanceof Error ? quotaLoadError.message : '';
          setQuotaError(message ? `${t('teams.quotaUnavailable')}：${message}` : t('teams.quotaUnavailable'));
        }
      } else {
        setQuota(null);
        setQuotaError('');
      }
      setTeams(nextTeams);
      setError(null);
    } catch (loadError) {
      console.error('Failed to load teams:', loadError);
      setTeams([]);
      setError(t('teams.errors.loadTeams'));
    } finally {
      setLoadingTeams(false);
    }
  }, [enterprise, t]);

  useEffect(() => {
    void loadTeams();
  }, [loadTeams]);

  const stopTeamRuntime = async (team: TeamInfo) => {
    if (!team.teamId || stoppingTeamId) return;
    setStoppingTeamId(team.teamId);
    try {
      await stopTeam({ team_id: team.teamId });
      await loadTeams();
    } catch (stopError) {
      console.error('Failed to stop team:', stopError);
      setError(t('teams.errors.stopTeam'));
    } finally {
      setStoppingTeamId('');
    }
  };

  const suffixTeams = teamsNeedingSessionSuffix(teams);

  const createdLineFor = (team: TeamInfo) => {
    const time = formatCreatedMinute(team.createdAt ?? 0);
    const suffix = suffixTeams.has(teamKey(team)) ? sessionSuffix(team.sessionId) : '';
    if (time && suffix) return t('teams.createdAtWithId', { time, suffix });
    if (time) return t('teams.createdAt', { time });
    if (suffix) return t('teams.sessionSuffix', { suffix });
    return '';
  };

  const labelFor = (team: TeamInfo) => {
    const title = team.sessionTitle?.trim() || team.sessionId?.trim() || t('teams.untitledSession');
    const created = createdLineFor(team);
    const name = t('teams.listLabel', { title });
    return created ? `${name} · ${created}` : name;
  };

  const deleteTeam = async (team: TeamInfo) => {
    if (!team.name || deletingTeam) return;
    const label = labelFor(team);
    const confirmed = window.confirm(t('teams.deleteConfirm', { team: label }));
    if (!confirmed) return;

    setDeletingTeamName(team.name);
    try {
      let response: TeamDeleteResponse;
      try {
        response = await webRequest<TeamDeleteResponse>('team.delete', {
          team_name: team.name,
          mode: 'team',
          ...(team.teamId ? { team_id: team.teamId } : {}),
        });
      } catch (deleteError) {
        console.error('Failed to delete team:', deleteError);
        setError(t('teams.errors.deleteTeam', { team: label }));
        return;
      }
      await loadTeams();
      try {
        await onSessionsDeleted?.(response.session_ids ?? []);
      } catch (syncError) {
        console.error('Failed to synchronize deleted team sessions:', syncError);
      }
    } finally {
      setDeletingTeamName('');
    }
  };

  return (
    <div className="flex h-full min-h-0 w-full flex-1" data-testid="team-panel">
      <div className="card flex h-full w-full flex-col">
        <div className="mb-4 flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold" data-testid="team-panel-title">
              {t('teams.title')}
            </h2>
            <p className="mt-1 text-sm text-text-muted" data-testid="team-panel-subtitle">
              {t('teams.subtitle')}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void loadTeams()}
            disabled={loadingTeams}
            className="btn !px-3 !py-1.5 disabled:cursor-not-allowed disabled:opacity-50"
            data-testid="team-panel-refresh"
          >
            {loadingTeams ? t('common.refreshing') : t('common.refresh')}
          </button>
        </div>

        {error ? (
          <div
            className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger"
            data-testid="team-panel-error"
          >
            {error}
          </div>
        ) : null}

        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-card/70 shadow-sm">
          <div className="border-b border-border bg-secondary/30 px-4 py-3">
            <h3 className="text-sm font-medium text-text" data-testid="team-panel-list-title">
              {t('teams.teamList')}
            </h3>
            <p className="mt-1 text-xs text-text-muted" data-testid="team-panel-summary">
              <span data-testid="team-panel-count">{t('teams.count', { count: teams.length })}</span>
              {quota?.teams ? (
                <>
                  {' · '}
                  <span data-testid="team-panel-quota">
                    {t('teams.quota', {
                      used: quota.teams.used,
                      limit: quota.teams.limit,
                      default: quota.teams.default,
                    })}
                  </span>
                </>
              ) : null}
              {quota?.org ? (
                <>
                  {' · '}
                  <span data-testid="team-panel-org-quota">
                    {t('teams.orgQuota', {
                      used: quota.org.used,
                      limit: quota.org.limit,
                    })}
                  </span>
                </>
              ) : null}
              {quotaError ? (
                <>
                  {' · '}
                  <span className="text-danger" data-testid="team-panel-quota-error">
                    {quotaError}
                  </span>
                </>
              ) : null}
            </p>
          </div>
          <div className="flex-1 space-y-1 overflow-auto p-2" data-testid="team-panel-list">
            {loadingTeams ? (
              <div className="flex h-full items-center justify-center">
                <div className="h-7 w-7 animate-spin rounded-full border-4 border-border border-t-accent" />
              </div>
            ) : teams.length === 0 ? (
              <div
                className="flex h-full items-center justify-center text-sm text-text-muted"
                data-testid="team-panel-empty"
              >
                {t('teams.empty')}
              </div>
            ) : (
              teams.map((team) => {
                const title = team.sessionTitle?.trim() || team.sessionId?.trim() || t('teams.untitledSession');
                const label = t('teams.listLabel', { title });
                const created = createdLineFor(team);
                return (
                <div
                  key={team.teamId || team.name}
                  className="flex w-full min-w-0 items-center gap-2 rounded-lg border border-transparent px-3 py-2 text-sm text-text hover:bg-secondary/40"
                  data-testid="team-panel-team-row"
                  data-variant={team.name}
                >
                  <div className="min-w-0 flex-1 text-left" title={team.teamId ? `${team.name} · ${team.teamId}` : team.name}>
                    <span className="block truncate font-medium" data-testid="team-panel-team-name">
                      {label}
                    </span>
                    <span className="mt-1 block text-[11px] text-text-muted">
                      {created ? (
                        <>
                          <span data-testid="team-panel-team-created">{created}</span>
                          {' · '}
                        </>
                      ) : null}
                      {t('teams.sessionCount', { count: team.sessionCount })}
                      {team.status ? (
                        <>
                          {' · '}
                          <span data-testid="team-panel-team-status">{t(`teams.status.${team.status}`)}</span>
                        </>
                      ) : null}
                      {typeof team.memberCount === 'number' && team.memberCount > 0 ? (
                        <>
                          {' · '}
                          {t('teams.humanMembers', { count: team.memberCount })}
                        </>
                      ) : null}
                    </span>
                  </div>
                  {team.teamId && team.status && STOPPABLE_STATUSES.has(team.status) ? (
                    <button
                      type="button"
                      onClick={() => void stopTeamRuntime(team)}
                      disabled={Boolean(stoppingTeamId)}
                      className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-text-muted hover:bg-secondary/40 disabled:cursor-not-allowed disabled:opacity-50"
                      title={t('teams.stopTeam')}
                      data-testid="team-panel-stop-button"
                      data-variant={team.teamId}
                    >
                      {stoppingTeamId === team.teamId ? t('teams.stopping') : t('teams.stopTeam')}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    onClick={() => void deleteTeam(team)}
                    disabled={deletingTeam}
                    className="shrink-0 rounded-md border border-danger/30 px-2 py-1 text-xs text-danger hover:bg-danger-subtle disabled:cursor-not-allowed disabled:opacity-50"
                    title={t('teams.deleteTeam')}
                    data-testid="team-panel-delete-button"
                    data-variant={team.name}
                  >
                    {deletingTeamName === team.name ? t('teams.deleting') : t('teams.deleteTeam')}
                  </button>
                </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

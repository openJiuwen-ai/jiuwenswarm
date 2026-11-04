import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight, Check, Crown, LoaderCircle, Users } from 'lucide-react';
import clsx from 'clsx';
import { conversationKey, useChatStore, useSessionStore, useTeamSelectorStore } from '../../stores';
import { useTeamOrganizationUiEnabled } from '../../features/teamOrganization/featureConfig';
import { useOrganizationProcessing } from '../../features/teamOrganization/processing';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import {
  resolveOrganizationTeamIdentity,
  resolveOrganizationTeamLabel,
  useOrganizationGroupCatalog,
} from '../../features/teamOrganization/teamIdentity';
import { resolveTeamGroupDisplayName } from '../../features/teamGroupIdentity';
import type { AgentGroupCatalogItem } from '../../features/agentManagement/types';
import type { RuntimeTeamInfo } from '../../stores/teamSelectorStore';
import { buildOrganizationTeamTree } from './organizationTree';

/** Uses the same session roster and conversation action as the composer TeamSelector. */
export function OrganizationTeamsList({ groups: providedGroups, variant = 'compact' }: {
  groups?: AgentGroupCatalogItem[];
  variant?: 'compact' | 'cards';
} = {}) {
  const { t, i18n } = useTranslation();
  const enabled = useTeamOrganizationUiEnabled();
  const sessionId = useChatStore((state) => state.activeSessionId);
  const runtime = useTeamSelectorStore((state) => sessionId ? state.runtimes[sessionId] : undefined);
  const sessionRuntimes = useSessionStore((state) => state.runtimes);
  const chatRuntimes = useChatStore((state) => state.runtimes);
  const organizationBusy = useOrganizationProcessing(sessionId);
  const fetchTeams = useTeamSelectorStore((state) => state.fetchTeams);
  const selectTeam = useTeamSelectorStore((state) => state.selectTeam);
  const realSession = enabled && Boolean(sessionId && sessionId !== 'new' && sessionId !== NEW_CONVERSATION_ID);
  const catalog = useOrganizationGroupCatalog(realSession && providedGroups === undefined && Boolean(runtime?.teams.length));
  const groups = providedGroups ?? catalog;

  useEffect(() => {
    if (!realSession || !sessionId) return;
    const current = useTeamSelectorStore.getState().runtimes[sessionId];
    if (!current?.loaded && !current?.loading) void fetchTeams(sessionId);
  }, [realSession, sessionId, runtime?.loaded, fetchTeams]);

  if (!realSession || !sessionId) return null;
  const teams = runtime?.teams ?? [];

  const renderTeam = (team: RuntimeTeamInfo) => {
    const identity = resolveOrganizationTeamIdentity(
      team,
      groups,
      sessionRuntimes[conversationKey(sessionId, team.is_owner ? null : team.team_id)]?.teamGroupIdentity,
    );
    const label = identity
      ? resolveTeamGroupDisplayName(identity, i18n.language)
      : resolveOrganizationTeamLabel(team, groups, team.team_id);
    const selected = runtime?.selectedTeamId === team.team_id;
    // Match the composer: a live Team runtime can remain "running" after team.idle.
    const key = conversationKey(sessionId, team.is_owner ? null : team.team_id);
    const running = team.available !== false && Boolean(
      chatRuntimes[key]?.isProcessing || (team.is_owner && organizationBusy),
    );
    return (
      <button
        key={team.team_id}
        type="button"
        className={clsx(
          'organization-team',
          selected && 'organization-team--selected',
          running && 'organization-team--running',
        )}
        data-testid="team-area-organization-team"
        data-variant={team.team_id}
        aria-pressed={selected}
        title={label}
        onClick={() => void selectTeam(sessionId, team.team_id)}
      >
        <span className="organization-team__avatar" aria-hidden="true">
          {identity?.avatarUrl ? (
            <img src={identity.avatarUrl} alt="" className="h-full w-full rounded-full object-contain" />
          ) : <Users size={20} />}
          {team.is_owner && <Crown size={12} className="organization-team__owner" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-text">{label}</span>
          <span className="organization-team__state" data-testid="team-area-organization-team-state" data-variant={team.team_id}>
            {running ? <LoaderCircle size={12} className="organization-team__spinner" aria-hidden="true" />
              : <span className="organization-team__dot" aria-hidden="true" />}
            {t(team.available === false ? 'chat.teamSelect.unavailable'
              : running ? 'team.org.stateRunning' : team.is_owner ? 'team.org.ownerLabel' : 'team.org.stateIdle')}
          </span>
        </span>
        {selected ? <Check size={15} className="organization-team__check" aria-hidden="true" />
          : <ArrowUpRight size={15} className="organization-team__arrow" aria-hidden="true" />}
      </button>
    );
  };
  return (
    <div className={clsx('organization-teams', variant === 'cards' && 'organization-teams--cards')} data-testid="team-area-organization-teams">
      {teams.length === 0 && (
        <div className="py-4 text-center text-xs text-text-muted" data-testid="team-area-organization-teams-status">
          {runtime?.error || t(runtime?.loading ? 'team.org.loading' : 'team.org.empty')}
        </div>
      )}
      {buildOrganizationTeamTree(teams).map((node) => (
        <div className="organization-teams__branch" key={node.key} data-testid="team-area-organization-team-branch" data-variant={node.key}>
          {renderTeam(node.team)}
          {node.children.length > 0 && (
            <div className="organization-teams__children" data-testid="team-area-organization-subteams" data-variant={node.key}>
              {node.children.map((child) => renderTeam(child.team))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

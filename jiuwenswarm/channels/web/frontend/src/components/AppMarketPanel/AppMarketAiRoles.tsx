import { useEffect, useState, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { PageCard } from '../ui/PageCard/PageCard';

interface AiRoleGroup {
  id: string;
  mode: 'agent' | 'team';
  session_description: string;
  leader?: string;
  roles: { id: string; name: string; description: string; avatar_url?: string }[];
}

type AiRolesState = { groups: AiRoleGroup[] } | { error: true } | null;

// Six declared roles in a row would be cramped; wrap the extra roles onto the
// next row and let CSS reduce the columns again on narrow viewports.
const MAX_ROLE_COLUMNS = 4;

function roleGridStyle(count: number): CSSProperties {
  return { '--app-market-ai-role-columns': Math.min(count, MAX_ROLE_COLUMNS) } as CSSProperties;
}

export function AppMarketAiRoles({ appId }: { appId: string }) {
  const { t } = useTranslation();
  const [result, setResult] = useState<AiRolesState>(null);

  useEffect(() => {
    let current = true;
    void webRequest<{ session_groups?: AiRoleGroup[] }>('app_market.ai_roles', { app_id: appId }).then(
      ({ session_groups }) => {
        if (current) setResult({ groups: session_groups ?? [] });
      },
      () => {
        if (current) setResult({ error: true });
      },
    );
    return () => { current = false; };
  }, [appId]);

  const groups = result && 'groups' in result ? result.groups : [];
  const agents = groups.filter(group => group.mode === 'agent');
  const teams = groups.filter(group => group.mode === 'team');

  function renderRoleCard(group: AiRoleGroup, role: AiRoleGroup['roles'][number]) {
    const labels = group.mode === 'team' ? [t('appMarket.aiRoles.teamBadge')] : undefined;
    if (group.leader === role.id) labels?.unshift(t('appMarket.aiRoles.leader'));
    const description =
      group.mode === 'agent'
        ? [role.description, group.session_description].filter(Boolean).join('\n\n')
        : role.description;
    const roleKey = `${group.id}/${role.id}`;
    return (
      <PageCard
        key={roleKey}
        avatar={{ name: role.name, iconUrl: role.avatar_url, testId: 'app-market-ai-role-avatar' }}
        title={role.name}
        label={labels}
        description={description}
        className="app-market-ai-role-card"
        testId="app-market-ai-role-card"
        variant={roleKey}
      />
    );
  }

  return (
    <section className="app-market-ai-roles" aria-label={t('appMarket.aiRoles.title')} data-testid="app-market-ai-roles">
      <h2 className="text-sm font-semibold">{t('appMarket.aiRoles.title')}</h2>
      {result === null ? (
        <p className="text-sm text-text-muted" role="status" data-testid="app-market-ai-roles-loading">
          {t('appMarket.loading')}
        </p>
      ) : 'error' in result ? (
        <p className="text-sm text-text-muted" role="status" data-testid="app-market-ai-roles-error">
          {t('appMarket.aiRoles.loadFailed')}
        </p>
      ) : groups.length === 0 ? (
        <p className="text-sm text-text-muted" data-testid="app-market-ai-roles-empty">{t('appMarket.aiRoles.empty')}</p>
      ) : (
        <>
          {agents.length > 0 && (
            <div
              className="app-market-ai-role-grid app-market-ai-role-grid--agent"
              style={roleGridStyle(agents.length)}
              data-testid="app-market-ai-agent-grid"
            >
              {agents.flatMap(group => group.roles.map(role => renderRoleCard(group, role)))}
            </div>
          )}
          {teams.map(group => (
            <div
              className="app-market-ai-role-group"
              key={group.id}
              data-testid="app-market-ai-role-group"
              data-variant={group.id}
              data-mode={group.mode}
            >
              <div className="app-market-ai-role-group__session">
                <h3 className="text-sm font-medium" data-testid="app-market-ai-role-mode">
                  {t('appMarket.aiRoles.team')}
                </h3>
                <p className="app-market-detail__description" data-testid="app-market-ai-role-session">
                  {group.session_description}
                </p>
              </div>
              <div
                className="app-market-ai-role-grid"
                style={roleGridStyle(group.roles.length)}
                data-testid="app-market-ai-team-grid"
                data-variant={group.id}
              >
                {group.roles.map(role => renderRoleCard(group, role))}
              </div>
            </div>
          ))}
        </>
      )}
    </section>
  );
}

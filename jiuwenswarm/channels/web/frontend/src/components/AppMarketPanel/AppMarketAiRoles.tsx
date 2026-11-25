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
      ) : result.groups.length === 0 ? (
        <p className="text-sm text-text-muted" data-testid="app-market-ai-roles-empty">{t('appMarket.aiRoles.empty')}</p>
      ) : result.groups.map(group => {
        const columns = Math.min(group.roles.length, MAX_ROLE_COLUMNS);
        return (
          <div
            className="app-market-ai-role-group"
            key={group.id}
            data-testid="app-market-ai-role-group"
            data-variant={group.id}
            data-mode={group.mode}
          >
            <div className="app-market-ai-role-group__session">
              <h3 className="text-sm font-medium" data-testid="app-market-ai-role-mode">
                {t(group.mode === 'team' ? 'appMarket.aiRoles.team' : 'appMarket.aiRoles.agent')}
              </h3>
              <p className="app-market-detail__description" data-testid="app-market-ai-role-session">
                {group.session_description}
              </p>
            </div>
            <div
              className="app-market-ai-role-grid"
              style={{ '--app-market-ai-role-columns': columns } as CSSProperties}
            >
              {group.roles.map(role => {
                const labels = [
                  ...(group.leader === role.id ? [t('appMarket.aiRoles.leader')] : []),
                  ...(group.mode === 'team' ? [t('appMarket.aiRoles.teamBadge')] : []),
                ];
                return (
                  <PageCard
                    key={role.id}
                    avatar={{ name: role.name, iconUrl: role.avatar_url, testId: 'app-market-ai-role-avatar' }}
                    title={role.name}
                    label={labels.length > 0 ? labels : undefined}
                    description={role.description}
                    className="app-market-ai-role-card"
                    testId="app-market-ai-role-card"
                    variant={`${group.id}/${role.id}`}
                  />
                );
              })}
            </div>
          </div>
        );
      })}
    </section>
  );
}

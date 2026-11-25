import { useEffect, useState } from 'react';
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
      ) : result.groups.map(group => (
        <div className="app-market-ai-role-group" key={group.id} data-testid="app-market-ai-role-group" data-variant={group.id}>
          <div className="app-market-ai-role-group__session">
            <h3 className="text-sm font-medium" data-testid="app-market-ai-role-mode">
              {t(group.mode === 'team' ? 'appMarket.aiRoles.team' : 'appMarket.aiRoles.agent')}
            </h3>
            <p className="app-market-detail__description" data-testid="app-market-ai-role-session">
              {group.session_description}
            </p>
          </div>
          <div className="card-grid-auto app-market-ai-role-grid">
            {group.roles.map(role => (
              <PageCard
                key={role.id}
                avatar={{ name: role.name, iconUrl: role.avatar_url, testId: 'app-market-ai-role-avatar' }}
                title={role.name}
                label={group.leader === role.id ? [t('appMarket.aiRoles.leader')] : undefined}
                description={role.description}
                className="app-market-ai-role-card"
                testId="app-market-ai-role-card"
                variant={`${group.id}/${role.id}`}
              />
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

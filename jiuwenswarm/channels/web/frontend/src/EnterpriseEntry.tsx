import { maintainBrowserSession } from './auth/manager/federation';
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from './components/ui/Button/Button';
import { isEnterprise } from './edition';
import { isLoginAuthSimulateEnabled } from './auth/config';
import { resolveEnterpriseAuthProvider } from './auth/providerRegistry';
import { EnterpriseAuthError, type EnterpriseAuthProvider } from './auth/types';
import {
  agentContextKey,
  EnterpriseContext,
  type EnterpriseAgentContext,
  type EnterpriseContextSnapshot,
  type EnterpriseContextValue,
} from './services/enterpriseContext';
import { parseRuntimeScope, setRuntimeScope } from './services/runtimeScope';

const CLUSTER_KEY = 'jiuwenclaw_active_cluster';

function normalizeContext(raw: Partial<EnterpriseAgentContext> | null | undefined): EnterpriseAgentContext | null {
  const botId = String(raw?.bot_id || '').trim();
  const groupId = String(raw?.group_id || '').trim();
  const userId = String(raw?.user_id || '').trim();
  if (!botId || !groupId || !userId) return null;
  return {
    bot_id: botId,
    group_id: groupId,
    user_id: userId,
    jiuwenclaw_id: String(raw?.jiuwenclaw_id || '').trim(),
    jiuwenclaw_name: String(raw?.jiuwenclaw_name || '').trim(),
    agent_name: String(raw?.agent_name || '').trim(),
    group_name: String(raw?.group_name || '').trim(),
  };
}

function chooseAgentContext(
  contexts: EnterpriseAgentContext[],
  preferred: { botId?: string; groupId?: string; userId?: string; jiuwenclawId?: string },
): EnterpriseAgentContext | null {
  if (!contexts.length) return null;
  const exact = contexts.find(
    (item) =>
      item.bot_id === preferred.botId &&
      item.group_id === preferred.groupId &&
      item.user_id === preferred.userId &&
      (!preferred.jiuwenclawId || item.jiuwenclaw_id === preferred.jiuwenclawId),
  );
  if (exact) return exact;
  return contexts.find((item) => item.bot_id === preferred.botId) ?? contexts[0] ?? null;
}

function contextUrl(selected: EnterpriseAgentContext, debugContext: boolean): string {
  const query = new URLSearchParams({
    user_id: selected.user_id,
    group_id: selected.group_id,
    bot_id: selected.bot_id,
  });
  if (selected.jiuwenclaw_id) query.set('jiuwenclaw_id', selected.jiuwenclaw_id);
  if (debugContext) query.set('debug_context', '1');
  return `${window.location.pathname}?${query.toString()}${window.location.hash}`;
}

function applyScope(selected: EnterpriseAgentContext): void {
  setRuntimeScope({
    userId: selected.user_id,
    groupId: selected.group_id,
    botId: selected.bot_id,
  });
}

async function ensureActiveCluster(provider: EnterpriseAuthProvider, jiuwenclawId: string): Promise<void> {
  const next = jiuwenclawId.trim();
  if (!next || !provider.setActiveCluster) return;
  await provider.setActiveCluster(next);
  sessionStorage.setItem(CLUSTER_KEY, next);
}

export function EnterpriseEntry({ children }: { children: ReactNode }) {
  const { t, i18n } = useTranslation();
  const enterprise = isEnterprise();
  const simulateLogin = enterprise && isLoginAuthSimulateEnabled();
  const provider = useMemo(
    () => enterprise ? resolveEnterpriseAuthProvider(simulateLogin) : null,
    [enterprise, simulateLogin],
  );
  const [phase, setPhase] = useState<'loading' | 'ready' | 'empty' | 'error'>(enterprise ? 'loading' : 'ready');
  const [error, setError] = useState<
    string | { key: 'enterpriseEntry.invalidIdentity' | 'enterpriseEntry.loadFailed' }
  >('');
  const [context, setContext] = useState<EnterpriseContextSnapshot | null>(null);
  const [contextError, setContextError] = useState('');
  const [contextSwitching, setContextSwitching] = useState(false);

  const logout = useCallback(() => {
    if (provider) void provider.logout();
  }, [provider]);

  useEffect(() => {
    if (!enterprise || !provider) return;
    if (!provider.isAuthenticated()) {
      provider.redirectToLogin();
      return;
    }
    let cancelled = false;
    const bootstrap = async () => {
      try {
        const preferred = parseRuntimeScope(window.location.search);
        const preferredCluster =
          new URLSearchParams(window.location.search).get('jiuwenclaw_id')?.trim() ||
          sessionStorage.getItem(CLUSTER_KEY) ||
          '';
        const debug = new URLSearchParams(window.location.search).get('debug_context') === '1';
        const user = await provider.getCurrentUser();
        if (cancelled) return;
        if (!user.user_id) {
          setError({ key: 'enterpriseEntry.invalidIdentity' });
          setPhase('error');
          return;
        }
        const listed = await provider.listAgentContexts();
        const contexts = listed
          .map((item) => normalizeContext(item))
          .filter((item): item is EnterpriseAgentContext => item !== null);
        if (cancelled) return;
        let selected = debug
          ? normalizeContext({
              bot_id: preferred.botId,
              group_id: preferred.groupId,
              user_id: preferred.userId || user.user_id,
              jiuwenclaw_id: preferredCluster,
            })
          : null;
        if (!selected) {
          selected = chooseAgentContext(contexts, {
            botId: preferred.botId,
            groupId: preferred.groupId,
            userId: preferred.userId,
            jiuwenclawId: preferredCluster,
          });
        }
        if (!selected) {
          setPhase('empty');
          return;
        }
        await ensureActiveCluster(provider, selected.jiuwenclaw_id);
        if (cancelled) return;
        applyScope(selected);
        window.history.replaceState(
          {},
          '',
          contextUrl(selected, debug && !contexts.some((item) => agentContextKey(item) === agentContextKey(selected!))),
        );
        setContext({ user, contexts, selected });
        setPhase('ready');
      } catch (bootstrapError) {
        if (cancelled) return;
        if (bootstrapError instanceof EnterpriseAuthError && bootstrapError.status === 401) {
          provider.redirectToLogin();
          return;
        }
        setError(
          bootstrapError instanceof Error ? bootstrapError.message : { key: 'enterpriseEntry.loadFailed' },
        );
        setPhase('error');
      }
    };
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [enterprise, provider]);

  useEffect(() => {
    if (!enterprise || simulateLogin || phase !== 'ready') return;
    let disposed = false;
    let stop: (() => void) | undefined;
    void maintainBrowserSession().then((cleanup) => {
      if (disposed) cleanup();
      else stop = cleanup;
    }).catch(() => undefined);
    return () => {
      disposed = true;
      stop?.();
    };
  }, [enterprise, simulateLogin, phase]);

  const contextValue = useMemo<EnterpriseContextValue | null>(() => {
    if (!context || !provider) return null;
    return {
      ...context,
      contextError,
      contextSwitching,
      onContextChange: (key) => {
        if (contextSwitching) return;
        const selected = context.contexts.find((item) => agentContextKey(item) === key);
        if (!selected || agentContextKey(selected) === agentContextKey(context.selected)) return;
        setContextSwitching(true);
        setContextError('');
        void ensureActiveCluster(provider, selected.jiuwenclaw_id)
          .then(() => {
            window.location.replace(contextUrl(selected, false));
          })
          .catch((switchError) => {
            setContextSwitching(false);
            setContextError(switchError instanceof Error ? switchError.message : t('enterpriseEntry.switchFailed'));
          });
      },
      onCustomContextApply: (input) => {
        if (contextSwitching) return;
        const botId = input.botId.trim();
        const groupId = input.groupId.trim();
        const userId = input.userId.trim();
        const jiuwenclawId = input.jiuwenclawId.trim();
        if (!botId || !groupId || !userId || !jiuwenclawId) {
          setContextError(t('sessionSidebar.enterpriseContext.customRequired'));
          return;
        }
        const matched = context.contexts.find((item) => item.jiuwenclaw_id === jiuwenclawId);
        setContextSwitching(true);
        setContextError('');
        const selected: EnterpriseAgentContext = {
          bot_id: botId,
          group_id: groupId,
          user_id: userId,
          jiuwenclaw_id: jiuwenclawId,
          jiuwenclaw_name: matched?.jiuwenclaw_name || jiuwenclawId,
          agent_name: botId,
          group_name: groupId,
        };
        void ensureActiveCluster(provider, jiuwenclawId)
          .then(() => {
            window.location.replace(contextUrl(selected, true));
          })
          .catch((switchError) => {
            setContextSwitching(false);
            setContextError(switchError instanceof Error ? switchError.message : t('enterpriseEntry.switchFailed'));
          });
      },
      onLogout: logout,
    };
  }, [context, contextError, contextSwitching, logout, provider, t]);

  if (!enterprise) return <>{children}</>;
  if (phase === 'ready' && contextValue) {
    return <EnterpriseContext.Provider value={contextValue}>{children}</EnterpriseContext.Provider>;
  }
  if (phase === 'empty') {
    const isZh = i18n.language.startsWith('zh');
    return (
      <main
        className="enterprise-entry-empty"
        data-testid="enterprise-entry"
        aria-labelledby="enterprise-entry-empty-title"
        lang={isZh ? 'zh' : 'en'}
      >
        <div className="enterprise-entry-empty__card">
          <div className="enterprise-entry-empty__header">
            <div className="enterprise-entry-empty__brand">
              JIUWEN<span>SWARM</span>
            </div>
            <div
              className="enterprise-entry-empty__language"
              role="group"
              aria-label={t('enterpriseEntry.language')}
            >
              <Button
                size="sm"
                variant={isZh ? 'primary' : 'quiet'}
                aria-pressed={isZh}
                lang="zh"
                onClick={() => void i18n.changeLanguage('zh')}
              >
                中
              </Button>
              <Button
                size="sm"
                variant={isZh ? 'quiet' : 'primary'}
                aria-pressed={!isZh}
                lang="en"
                onClick={() => void i18n.changeLanguage('en')}
              >
                En
              </Button>
            </div>
          </div>
          <div className="enterprise-entry-empty__eyebrow" lang="en">ENTERPRISE WORKSPACE</div>
          <h1 id="enterprise-entry-empty-title" data-testid="enterprise-entry-title">
            {t('enterpriseEntry.emptyTitle')}
          </h1>
          <p data-testid="enterprise-entry-status">{t('enterpriseEntry.noContext')}</p>
          <button
            className="enterprise-entry-empty__login"
            type="button"
            data-testid="enterprise-entry-login"
            onClick={() => provider?.redirectToLogin()}
          >
            {t('enterpriseEntry.backToLogin')}
          </button>
          <button
            className="enterprise-entry-empty__retry"
            type="button"
            data-testid="enterprise-entry-retry"
            onClick={() => window.location.reload()}
          >
            {t('enterpriseEntry.retry')}
          </button>
        </div>
      </main>
    );
  }
  const errorMessage = typeof error === 'string' ? error : t(error.key);
  return (
    <div
      data-testid="enterprise-entry"
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        background: 'var(--color-bg-primary)',
        color: 'var(--color-text-primary)',
      }}
    >
      <div style={{ width: 'min(100%, 420px)', padding: 32 }}>
        <h1 data-testid="enterprise-entry-title" style={{ margin: '0 0 12px', fontSize: 28 }}>
          {t(phase === 'error' ? 'enterpriseEntry.errorTitle' : 'enterpriseEntry.loadingTitle')}
        </h1>
        <p data-testid="enterprise-entry-status" style={{ margin: 0, lineHeight: 1.6 }}>
          {phase === 'error' ? errorMessage : t('enterpriseEntry.loading')}
        </p>
        {phase === 'error' && (
          <>
            <button data-testid="enterprise-entry-retry" onClick={() => window.location.reload()}>
              {t('enterpriseEntry.retry')}
            </button>
            <button data-testid="enterprise-entry-login" onClick={() => provider?.redirectToLogin()}>
              {t('enterpriseEntry.login')}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

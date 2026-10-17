import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isEnterprise } from './edition';
import {
  agentContextKey,
  EnterpriseContext,
  type EnterpriseAgentContext,
  type EnterpriseContextSnapshot,
  type EnterpriseContextValue,
  type EnterpriseUser,
} from './services/enterpriseContext';
import { getRuntimeScope, parseRuntimeScope, setRuntimeScope } from './services/runtimeScope';

const ACCESS_KEY = 'openjiuwen_access_token';
const REFRESH_KEY = 'openjiuwen_refresh_token';
const CLUSTER_KEY = 'jiuwenclaw_active_cluster';

type ManagerBody<T> = { code?: number; message?: string; data?: T };

function authHeaders(): Record<string, string> {
  const token = window.localStorage.getItem(ACCESS_KEY)?.trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function readManager<T>(url: string, init: RequestInit = {}): Promise<ManagerBody<T>> {
  const response = await fetch(url, {
    ...init,
    credentials: 'include',
    headers: { ...authHeaders(), ...(init.headers ?? {}) },
  });
  const body = await response.json().catch(() => ({})) as ManagerBody<T> & { detail?: string };
  if (!response.ok || (body.code != null && body.code !== 200)) {
    throw new Error(body.message || body.detail || `HTTP ${response.status}`);
  }
  return body;
}

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
  const exact = contexts.find(item =>
    item.bot_id === preferred.botId
    && item.group_id === preferred.groupId
    && item.user_id === preferred.userId
    && (!preferred.jiuwenclawId || item.jiuwenclaw_id === preferred.jiuwenclawId));
  if (exact) return exact;
  return contexts.find(item => item.bot_id === preferred.botId) ?? contexts[0] ?? null;
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

async function ensureActiveCluster(jiuwenclawId: string): Promise<void> {
  const next = jiuwenclawId.trim();
  if (!next) return;
  const prev = sessionStorage.getItem(CLUSTER_KEY) || '';
  if (prev === next) return;
  await readManager('/manager-api/v1/user-console/active-cluster', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jiuwenclaw_id: next }),
  });
  sessionStorage.setItem(CLUSTER_KEY, next);
}

function loginPath(): string {
  const pathname = window.location.pathname;
  const chatIndex = pathname.indexOf('/chat');
  const prefix = chatIndex !== -1 ? pathname.slice(0, chatIndex) : '';
  return prefix ? `${prefix}/auth` : '/auth';
}

export function EnterpriseEntry({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const enterprise = isEnterprise();
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>(enterprise ? 'loading' : 'ready');
  const [error, setError] = useState('');
  const [context, setContext] = useState<EnterpriseContextSnapshot | null>(null);
  const [contextError, setContextError] = useState('');
  const [contextSwitching, setContextSwitching] = useState(false);
  const [managerAccess, setManagerAccess] = useState(false);

  const logout = useCallback(() => {
    window.localStorage.removeItem(ACCESS_KEY);
    window.localStorage.removeItem(REFRESH_KEY);
    sessionStorage.removeItem(CLUSTER_KEY);
    void fetch('/manager-api/v1/user-console/active-cluster', { method: 'DELETE', credentials: 'include' })
      .finally(() => {
        window.location.replace(loginPath());
      });
  }, []);

  const switchToManager = useCallback(() => {
    window.location.href = '/manager';
  }, []);

  useEffect(() => {
    if (!enterprise) return;
    let cancelled = false;
    const bootstrap = async () => {
      try {
        const preferred = parseRuntimeScope(window.location.search);
        const preferredCluster = new URLSearchParams(window.location.search).get('jiuwenclaw_id')?.trim()
          || sessionStorage.getItem(CLUSTER_KEY)
          || '';
        const debug = new URLSearchParams(window.location.search).get('debug_context') === '1';
        let user: EnterpriseUser = {
          user_id: preferred.userId || '',
          display_name: preferred.userId || '',
        };
        try {
          const me = await readManager<EnterpriseUser>('/idp/v1/auth/me');
          if (me.data?.user_id) user = me.data;
        } catch {
          // 展示名读不到时仍用上下文里的 user_id。
        }
        try {
          const access = await readManager<{ manager_access?: boolean; is_platform_admin?: boolean }>(
            '/manager-api/v1/authz/me',
          );
          if (!cancelled) {
            setManagerAccess(Boolean(access.data?.manager_access || access.data?.is_platform_admin));
          }
        } catch {
          if (!cancelled) setManagerAccess(false);
        }
        const listed = await readManager<{ contexts?: Partial<EnterpriseAgentContext>[] }>(
          '/manager-api/v1/user-console/agent-contexts',
        );
        const contexts = (listed.data?.contexts ?? [])
          .map(item => normalizeContext(item))
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
        if (!selected) throw new Error('当前账号没有可用的 Agent，请先在管理面完成授权。');
        if (!user.user_id) user = { user_id: selected.user_id, display_name: selected.user_id };
        applyScope(selected);
        window.history.replaceState({}, '', contextUrl(selected, debug && !contexts.some(item => agentContextKey(item) === agentContextKey(selected!))));
        setContext({ user, contexts, selected });
        setPhase('ready');
      } catch (bootstrapError) {
        if (cancelled) return;
        const scope = getRuntimeScope();
        if (scope.userId && scope.groupId && scope.botId) {
          setPhase('ready');
          return;
        }
        setError(bootstrapError instanceof Error ? bootstrapError.message : '加载 Agent 上下文失败');
        setPhase('error');
      }
    };
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [enterprise]);

  const contextValue = useMemo<EnterpriseContextValue | null>(() => {
    if (!context) return null;
    return {
      ...context,
      managerAccess,
      contextError,
      contextSwitching,
      onContextChange: key => {
        if (contextSwitching) return;
        const selected = context.contexts.find(item => agentContextKey(item) === key);
        if (!selected || agentContextKey(selected) === agentContextKey(context.selected)) return;
        setContextSwitching(true);
        setContextError('');
        void ensureActiveCluster(selected.jiuwenclaw_id)
          .then(() => {
            window.location.replace(contextUrl(selected, false));
          })
          .catch(switchError => {
            setContextSwitching(false);
            setContextError(switchError instanceof Error ? switchError.message : '切换身份失败');
          });
      },
      onCustomContextApply: input => {
        if (contextSwitching) return;
        const botId = input.botId.trim();
        const groupId = input.groupId.trim();
        const userId = input.userId.trim();
        const jiuwenclawId = input.jiuwenclawId.trim();
        if (!botId || !groupId || !userId || !jiuwenclawId) {
          setContextError(t('sessionSidebar.enterpriseContext.customRequired'));
          return;
        }
        const matched = context.contexts.find(item => item.jiuwenclaw_id === jiuwenclawId);
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
        void ensureActiveCluster(jiuwenclawId)
          .then(() => {
            window.location.replace(contextUrl(selected, true));
          })
          .catch(switchError => {
            setContextSwitching(false);
            setContextError(switchError instanceof Error ? switchError.message : '切换身份失败');
          });
      },
      onSwitchToManager: switchToManager,
      onLogout: logout,
    };
  }, [context, contextError, contextSwitching, logout, managerAccess, switchToManager, t]);

  if (!enterprise) return <>{children}</>;
  if (phase === 'ready' && contextValue) {
    return <EnterpriseContext.Provider value={contextValue}>{children}</EnterpriseContext.Provider>;
  }
  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#0b1020', color: '#eef4ff' }}>
      <div style={{ width: 'min(100%, 420px)', padding: 32 }}>
        <h1 style={{ margin: '0 0 12px', fontSize: 28 }}>
          {phase === 'error' ? '无法进入工作空间' : '正在加载工作空间'}
        </h1>
        <p style={{ margin: 0, lineHeight: 1.6 }}>
          {phase === 'error' ? error : '正在读取当前账号可用的 Agent。'}
        </p>
      </div>
    </div>
  );
}

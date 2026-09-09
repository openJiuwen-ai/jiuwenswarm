import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isLoginAuthSimulateEnabled } from './auth/config';
import { resolveEnterpriseAuthProvider } from './auth/providerRegistry';
import { EnterpriseAuthError } from './auth/types';
import { isEnterprise } from './edition';
import {
  agentContextKey,
  EnterpriseContext,
  type EnterpriseAgentContext,
  type EnterpriseContextSnapshot,
  type EnterpriseContextValue,
} from './services/enterpriseContext';
import { parseRuntimeScope, setRuntimeScope } from './services/runtimeScope';

type EntryPhase = 'loading' | 'ready' | 'empty' | 'error' | 'redirecting' | 'login-required';

function movePreferredFirst(items: EnterpriseAgentContext[], preferredKey?: string): EnterpriseAgentContext[] {
  if (!preferredKey) return items;
  const preferredIndex = items.findIndex(item => agentContextKey(item) === preferredKey);
  if (preferredIndex <= 0) return items;
  return [items[preferredIndex], ...items.slice(0, preferredIndex), ...items.slice(preferredIndex + 1)];
}

export function chooseAgentContext(
  contexts: EnterpriseAgentContext[],
  preferred?: { botId?: string; groupId?: string; userId?: string },
): EnterpriseAgentContext | null {
  if (!contexts.length) return null;
  if (preferred?.botId && preferred.groupId && preferred.userId) {
    const exact = contexts.find(
      item =>
        item.bot_id === preferred.botId &&
        item.group_id === preferred.groupId &&
        item.user_id === preferred.userId,
    );
    if (exact) return exact;
  }
  if (preferred?.botId) {
    const byBot = contexts.find(item => item.bot_id === preferred.botId);
    if (byBot) return byBot;
  }
  return contexts[0] ?? null;
}

function entryPath(): string {
  const pathname = window.location.pathname;
  // 刷新落在具体会话路由上时必须保留原路径：引导完成后 activateContext 会
  // replaceState 回本函数拼出的地址，若把 /chat/<sessionId> 折叠成 /chat/，
  // App 挂载时就解析不到会话 id，刷新后历史不恢复（点侧栏才回来）。
  if (/^\/chat\/[^/]+/.test(pathname)) return pathname;
  return pathname.startsWith('/chat') ? '/chat/' : '/';
}

function contextUrl(selected: EnterpriseAgentContext, debugContext = false): string {
  const query = new URLSearchParams({
    user_id: selected.user_id,
    group_id: selected.group_id,
    bot_id: selected.bot_id,
  });
  if (debugContext) query.set('debug_context', '1');
  return `${entryPath()}?${query.toString()}`;
}

function activateContext(selected: EnterpriseAgentContext, navigate: boolean, debugContext = false): void {
  setRuntimeScope({
    userId: selected.user_id,
    groupId: selected.group_id,
    botId: selected.bot_id,
  });
  const nextUrl = contextUrl(selected, debugContext);
  if (navigate) window.location.replace(nextUrl);
  else window.history.replaceState({}, '', nextUrl);
}

export function isDebugContext(search: string): boolean {
  return new URLSearchParams(search).get('debug_context') === '1';
}

export function buildCustomContext(
  preferred: ReturnType<typeof parseRuntimeScope>,
  fallbackGatewayId = '',
): EnterpriseAgentContext | null {
  if (!preferred.groupId || !preferred.botId || !preferred.userId) return null;
  return {
    bot_id: preferred.botId,
    group_id: preferred.groupId,
    user_id: preferred.userId,
    jiuwenclaw_id: fallbackGatewayId,
    agent_name: preferred.botId,
    group_name: preferred.groupId,
  };
}

function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : '加载企业用户上下文失败';
}

function EntryStatus({ phase, error, onLogout }: { phase: EntryPhase; error: string; onLogout: () => void }) {
  const empty = phase === 'empty';
  const failed = phase === 'error';
  const loginRequired = phase === 'login-required';
  return (
    <div className="enterprise-entry">
      <div className="enterprise-entry__glow" />
      <div className="enterprise-entry__card">
        <div className="enterprise-entry__brand">
          JIUWEN<span>CLAW</span>
        </div>
        <div className="enterprise-entry__eyebrow">ENTERPRISE WORKSPACE</div>
        <h1>
          {empty
            ? '暂无可用 Agent'
            : failed
              ? '加载失败'
              : loginRequired
                ? '请从统一登录入口访问'
                : phase === 'redirecting'
                  ? '正在前往登录页'
                  : '正在加载工作空间'}
        </h1>
        <p>
          {empty
            ? '当前账号没有可用的 Agent 上下文组合，请联系管理员完成授权。'
            : failed
              ? error
              : loginRequired
                ? '当前 User Web 地址不提供登录页面，请访问正确的登录入口。'
                : '正在校验账号权限并选择一个可用 Agent。'}
        </p>
        {(empty || failed) && (
          <button type="button" className="enterprise-entry__button" onClick={onLogout}>
            返回登录页
          </button>
        )}
      </div>
    </div>
  );
}

export function EnterpriseEntry({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const enterprise = isEnterprise();
  const simulateLogin = enterprise && isLoginAuthSimulateEnabled();
  const provider = useMemo(
    () => (enterprise ? resolveEnterpriseAuthProvider(simulateLogin) : null),
    [enterprise, simulateLogin],
  );
  const [phase, setPhase] = useState<EntryPhase>(() => (provider && !provider.isAuthenticated() ? 'redirecting' : 'loading'));
  const [context, setContext] = useState<EnterpriseContextSnapshot | null>(null);
  const [error, setError] = useState('');
  const [contextError, setContextError] = useState('');
  const [contextSwitching, setContextSwitching] = useState(false);

  const logout = useCallback(() => {
    if (provider) void provider.logout();
  }, [provider]);

  useEffect(() => {
    if (!enterprise || !provider) return;
    console.info(provider.startupMessage);
    if (!provider.isAuthenticated()) {
      if (!provider.redirectToLogin()) setPhase('login-required');
      return;
    }

    let cancelled = false;
    const bootstrap = async () => {
      try {
        const preferred = parseRuntimeScope(window.location.search);
        const [user, contexts] = await Promise.all([provider.getCurrentUser(), provider.listAgentContexts()]);
        if (cancelled) return;

        let selected: EnterpriseAgentContext | null = null;
        if (isDebugContext(window.location.search)) {
          const matched = preferred.botId
            ? contexts.find(item => item.bot_id === preferred.botId)
            : undefined;
          // 旧版 debug URL 可省略 user_id（默认取登录用户），与 chooseAgentContext 对齐。
          selected = buildCustomContext(
            {
              ...preferred,
              userId: preferred.userId || user.user_id,
            },
            matched?.jiuwenclaw_id || contexts[0]?.jiuwenclaw_id || '',
          );
        }
        if (!selected) {
          selected = chooseAgentContext(
            movePreferredFirst(
              contexts,
              preferred.botId && preferred.groupId && preferred.userId
                ? agentContextKey({
                    bot_id: preferred.botId,
                    group_id: preferred.groupId,
                    user_id: preferred.userId,
                  })
                : undefined,
            ),
            {
              botId: preferred.botId,
              groupId: preferred.groupId,
              userId: preferred.userId || user.user_id,
            },
          );
        }
        if (!selected) {
          setPhase('empty');
          return;
        }
        activateContext(selected, false, isDebugContext(window.location.search));
        setContext({ user, contexts, selected });
        setPhase('ready');
      } catch (bootstrapError) {
        if (cancelled) return;
        if (bootstrapError instanceof EnterpriseAuthError && bootstrapError.status === 401) {
          if (!provider.redirectToLogin()) setPhase('login-required');
          return;
        }
        setError(errorText(bootstrapError));
        setPhase('error');
      }
    };
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [enterprise, provider]);

  const contextValue = useMemo<EnterpriseContextValue | null>(() => {
    if (!context) return null;
    return {
      ...context,
      contextError,
      contextSwitching,
      onContextChange: key => {
        if (contextSwitching) return;
        const selected = context.contexts.find(item => agentContextKey(item) === key);
        if (!selected) return;
        // 自定义（debug_context）下即使三元组碰巧与某授权项相同，点选列表项也要退出自定义。
        const sameIdentity = agentContextKey(selected) === agentContextKey(context.selected);
        if (sameIdentity && !isDebugContext(window.location.search)) return;
        setContextSwitching(true);
        setContextError('');
        activateContext(selected, true, false);
      },
      onCustomContextApply: input => {
        if (contextSwitching) return;
        const botId = input.botId.trim();
        const groupId = input.groupId.trim();
        const userId = input.userId.trim();
        if (!botId || !groupId || !userId) {
          setContextError(t('sessionSidebar.enterpriseContext.customRequired'));
          return;
        }
        setContextSwitching(true);
        setContextError('');
        activateContext(
          {
            bot_id: botId,
            group_id: groupId,
            user_id: userId,
            jiuwenclaw_id: context.selected.jiuwenclaw_id,
            agent_name: botId,
            group_name: groupId,
          },
          true,
          true,
        );
      },
      onLogout: logout,
    };
  }, [context, contextError, contextSwitching, logout, t]);

  if (!enterprise) return <>{children}</>;
  if (phase !== 'ready' || !contextValue) return <EntryStatus phase={phase} error={error} onLogout={logout} />;
  return <EnterpriseContext.Provider value={contextValue}>{children}</EnterpriseContext.Provider>;
}

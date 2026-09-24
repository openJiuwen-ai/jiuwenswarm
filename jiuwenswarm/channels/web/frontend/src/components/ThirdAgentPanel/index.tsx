/**
 * ThirdAgentPanel — 三方 Agent 管理面板（只读）。
 *
 * 数据来源：web RPC `third_agents.list`（后端读 ~/.jiuwenswarm/third_agents.json
 * 并实时探测进程/端口状态）。安装/卸载走聊天指令（install_third_agent /
 * uninstall_third_agent 工具），面板不提供写操作。
 */

import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';

interface ThirdAgentInfo {
  name: string;
  displayName: string;
  status: 'running' | 'stopped';
  url: string;
  installedAt: string;
}

interface ThirdAgentListResponse {
  agents?: unknown[];
}

function toThirdAgentInfos(raw: unknown[]): ThirdAgentInfo[] {
  const rows: ThirdAgentInfo[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const name = typeof rec.name === 'string' ? rec.name : '';
    const url = typeof rec.url === 'string' ? rec.url : '';
    if (!name || !url) continue;
    rows.push({
      name,
      displayName: typeof rec.display_name === 'string' && rec.display_name ? rec.display_name : name,
      status: rec.status === 'running' ? 'running' : 'stopped',
      url,
      installedAt: typeof rec.installed_at === 'string' ? rec.installed_at : '',
    });
  }
  return rows.sort((a, b) => a.displayName.localeCompare(b.displayName, 'zh-Hans-CN'));
}

function formatInstalledAt(iso: string): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString();
}

export function ThirdAgentPanel() {
  const { t } = useTranslation();
  const [agents, setAgents] = useState<ThirdAgentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const resp = await webRequest<ThirdAgentListResponse>('third_agents.list', {});
      setAgents(toThirdAgentInfos(resp?.agents ?? []));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="flex-1 min-h-0 relative overflow-y-auto" data-testid="third-agent-panel">
      <div className="mx-auto max-w-5xl px-6 py-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-text">{t('thirdAgents.title')}</h2>
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-sm text-text hover:bg-bg-hover"
          >
            {t('thirdAgents.refresh')}
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-lg border border-border bg-card px-4 py-3 text-sm text-red-500">
            {t('thirdAgents.loadFailed')}: {error}
          </div>
        )}

        {!error && loading && (
          <div className="mt-10 text-center text-sm text-text-muted">{t('thirdAgents.loading')}</div>
        )}

        {!error && !loading && agents.length === 0 && (
          <div className="mt-10 rounded-lg border border-dashed border-border px-6 py-10 text-center">
            <div className="text-sm text-text-muted">{t('thirdAgents.empty')}</div>
            <div className="mt-2 text-xs text-text-muted">{t('thirdAgents.emptyHint')}</div>
          </div>
        )}

        {!loading && agents.length > 0 && (
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {agents.map((agent) => (
              <button
                key={agent.name}
                type="button"
                onClick={() => window.open(agent.url, '_blank', 'noopener,noreferrer')}
                className="group rounded-xl border border-border bg-card p-4 text-left transition hover:border-accent hover:shadow-md"
                data-testid={`third-agent-card-${agent.name}`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-base font-medium text-text group-hover:text-accent">
                    {agent.displayName}
                  </span>
                  <span className="inline-flex items-center gap-1.5 text-xs text-text-muted">
                    <span
                      className={`inline-block h-2 w-2 rounded-full ${
                        agent.status === 'running' ? 'bg-ok' : 'bg-text-muted'
                      }`}
                    />
                    {agent.status === 'running'
                      ? t('thirdAgents.running')
                      : t('thirdAgents.stopped')}
                  </span>
                </div>
                <div className="mono mt-2 truncate text-xs text-text-muted">{agent.url}</div>
                {agent.installedAt && (
                  <div className="mt-1 text-xs text-text-muted">
                    {t('thirdAgents.installedAt')}: {formatInstalledAt(agent.installedAt)}
                  </div>
                )}
                <div className="mt-3 text-xs text-accent opacity-0 transition group-hover:opacity-100">
                  {t('thirdAgents.openHint')}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default ThirdAgentPanel;

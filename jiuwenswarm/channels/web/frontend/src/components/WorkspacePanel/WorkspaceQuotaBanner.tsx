import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { fetchWorkspaceUsage } from '../../features/workspace/workspaceApi';
import type { WorkspaceUsageData } from '../../features/workspace/workspaceTypes';

const POLL_MS = 60_000;

interface WorkspaceQuotaBannerProps {
  /** 隐藏横幅（例如当前已在工作空间页） */
  hidden?: boolean;
}

export function WorkspaceQuotaBanner({ hidden }: WorkspaceQuotaBannerProps) {
  const { t } = useTranslation();
  const [usage, setUsage] = useState<WorkspaceUsageData | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const next = await fetchWorkspaceUsage();
        if (!cancelled) setUsage(next);
      } catch {
        if (!cancelled) setUsage(null);
      }
    };
    void load();
    const timer = window.setInterval(() => {
      void load();
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  if (hidden || !usage || usage.status === 'ok' || usage.unlimited || usage.limit_bytes === -1) {
    return null;
  }

  const goAgents = () => {
    window.dispatchEvent(new CustomEvent<string>('jiuwen:nav', { detail: 'agents' }));
  };

  if (usage.status === 'block') {
    return (
      <div className="mx-3 mt-2 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium">{t('agent.quota.chatBlockTitle')}</div>
          <div className="text-xs mt-0.5 opacity-90">{t('agent.quota.chatBlockBody')}</div>
        </div>
        <button type="button" className="btn !px-3 !py-1.5 flex-shrink-0" onClick={goAgents}>
          {t('agent.quota.openWorkspace')}
        </button>
      </div>
    );
  }

  return (
    <div className="mx-3 mt-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="font-medium">{t('agent.quota.chatWarnTitle')}</div>
        <div className="text-xs mt-0.5 opacity-90">{t('agent.quota.chatWarnBody')}</div>
      </div>
      <button type="button" className="btn !px-3 !py-1.5 flex-shrink-0" onClick={goAgents}>
        {t('agent.quota.openWorkspace')}
      </button>
    </div>
  );
}

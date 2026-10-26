import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { fetchWorkspaceUsage } from '../../features/workspace/workspaceApi';
import type { WorkspaceUsageData } from '../../features/workspace/workspaceTypes';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import { useChatStore } from '../../stores/chatStore';

/** warn/block 需较及时；ok 时降频，减少无意义的 usage 占槽。 */
const POLL_MS_ALERT = 60_000;
const POLL_MS_OK = 5 * 60_000;

function isRealChatSession(sessionId: string | null | undefined): sessionId is string {
  const sid = sessionId?.trim();
  return Boolean(sid) && sid !== NEW_CONVERSATION_ID;
}

function pollIntervalMs(usage: WorkspaceUsageData | null): number {
  if (!usage || usage.unlimited || usage.limit_bytes === -1) {
    return POLL_MS_OK;
  }
  if (usage.status === 'warn' || usage.status === 'block') {
    return POLL_MS_ALERT;
  }
  return POLL_MS_OK;
}

interface WorkspaceQuotaBannerProps {
  /** 隐藏横幅（例如当前已在工作空间页） */
  hidden?: boolean;
}

/**
 * 仅在配额特性开启时由 ChatPanel 挂载。
 * - 方案 3：仅真实聊天 session 存在时才查/轮询（对齐 Skill，避免 new/空会话刷 webhttp_*）。
 * - 方案 1：ok 降频轮询，warn/block 保持 60s。
 */
export function WorkspaceQuotaBanner({ hidden }: WorkspaceQuotaBannerProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const [usage, setUsage] = useState<WorkspaceUsageData | null>(null);
  const usageRef = useRef<WorkspaceUsageData | null>(null);
  usageRef.current = usage;

  useEffect(() => {
    if (!isRealChatSession(activeSessionId)) {
      setUsage(null);
      return;
    }

    let cancelled = false;
    let timer: number | undefined;

    const scheduleNext = () => {
      if (cancelled) return;
      const delay = pollIntervalMs(usageRef.current);
      timer = window.setTimeout(() => {
        void load();
      }, delay);
    };

    const load = async () => {
      try {
        const next = await fetchWorkspaceUsage();
        if (cancelled) return;
        setUsage(next);
        usageRef.current = next;
      } catch {
        if (cancelled) return;
        setUsage(null);
        usageRef.current = null;
      } finally {
        scheduleNext();
      }
    };

    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [activeSessionId]);

  if (
    hidden ||
    !usage ||
    usage.status === 'ok' ||
    usage.unlimited ||
    usage.limit_bytes === -1
  ) {
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

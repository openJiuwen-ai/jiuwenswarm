/**
 * ToolPanel 组件
 *
 * 工具面板，显示 Todo 列表和状态信息
 */

import { useTranslation } from 'react-i18next';
import { useSessionStore } from '../../stores';
import { useEffect, useState } from 'react';
import { webRequest } from '../../services/webClient';
import { HeartbeatMessageModal } from '../../features/HeartbeatMessageModal';
import { TodoList } from '../TodoList';
import { TeamArea } from '../TeamArea';
import { TeamTaskEvents } from '../TeamTaskEvents';
import './ToolPanel.css';

export function ToolPanel() {
  const { t } = useTranslation();
  const {
    contextCompressionRate,
    contextCompressionBefore,
    contextCompressionAfter,
    isConnected,
    memoryUsage,
    setMemoryUsage,
    heartbeatState,
    heartbeatMessage,
    heartbeatUpdatedAt,
    mode,
    teamTaskEvents,
    teamMembers,
  } = useSessionStore();
  const [heartbeatModalOpen, setHeartbeatModalOpen] = useState(false);

  useEffect(() => {
    if (!isConnected) {
      setMemoryUsage(null);
      return;
    }

    let disposed = false;
    let timerId: number | null = null;

    const refreshMemoryUsage = async () => {
      try {
        const payload = await webRequest<Record<string, unknown>>('memory.compute');
        if (disposed) return;

        const rssMb =
          typeof payload.rss_mb === 'number' && Number.isFinite(payload.rss_mb)
            ? payload.rss_mb
            : null;
        const usedPercent =
          typeof payload.used_percent === 'number' && Number.isFinite(payload.used_percent)
            ? payload.used_percent
            : null;

        setMemoryUsage({ rssMb, usedPercent });
      } catch {
        if (!disposed) {
          setMemoryUsage(null);
        }
      }
    };

    void refreshMemoryUsage();
    timerId = window.setInterval(() => {
      void refreshMemoryUsage();
    }, 10000);

    return () => {
      disposed = true;
      if (timerId != null) {
        window.clearInterval(timerId);
      }
    };
  }, [isConnected, setMemoryUsage]);

  const hasHeartbeatMessage = Boolean(heartbeatMessage?.trim());
  const heartbeatClassName =
    heartbeatState === 'ok' || hasHeartbeatMessage
      ? 'text-ok border-[var(--border-ok)] bg-ok-subtle'
      : heartbeatState === 'alert'
        ? 'text-danger border-[var(--border-danger)] bg-danger-subtle'
        : 'text-text-muted border-border bg-secondary/40';

  const heartbeatDetail = heartbeatUpdatedAt
    ? new Date(heartbeatUpdatedAt).toLocaleTimeString(undefined, { hour12: false })
    : '--:--:--';
  const isHeartbeatOk = heartbeatMessage?.toUpperCase().includes('HEARTBEAT_OK') ?? false;
  const heartbeatDisplayMessage = !heartbeatMessage
    ? 'HEARTBEAT_UNKNOWN'
    : isHeartbeatOk
      ? heartbeatMessage
      : t('toolPanel.heartbeatClick');
  const canOpenHeartbeatModal = Boolean(heartbeatMessage) && !isHeartbeatOk;
  const memoryDisplay =
    memoryUsage.rssMb == null
      ? '--'
      : `${memoryUsage.rssMb.toFixed(1)} MB${memoryUsage.usedPercent == null ? '' : ` (${memoryUsage.usedPercent.toFixed(1)}%)`}`;
  const beforeK = ((contextCompressionBefore ?? 0) / 1000).toFixed(1);
  const afterK = ((contextCompressionAfter ?? 0) / 1000).toFixed(1);
  let compressionRateDisplay;
  if (contextCompressionBefore === 0 || contextCompressionBefore === null) {
    compressionRateDisplay = '--';
  } else if (contextCompressionAfter === contextCompressionBefore) {
    compressionRateDisplay = '0.0';
  } else {
    compressionRateDisplay = Number.isFinite(contextCompressionRate)
      ? contextCompressionRate.toFixed(1)
      : '0.0';
  }
  const compressionDisplay = `${afterK}K/${beforeK}K (${compressionRateDisplay}%)`;

  return (
    <div
      data-testid="tool-panel"
      className="bg-panel border-l border-border h-full overflow-hidden py-4 px-3 shrink-0"
      style={{ width: 'var(--tool-panel-width)' }}
    >
      <div className="h-full bg-panel flex flex-col overflow-hidden">
        {/* 任务事件日志 */}
        {mode === 'team' ? (
          <div className="flex-1 overflow-y-auto mb-4">
            <div className="bg-card rounded-lg border border-border overflow-hidden h-full">
              <TeamTaskEvents events={teamTaskEvents} />
            </div>
          </div>
        ) : (
          /* Todo 列表 */
          <div className="flex-1 overflow-y-auto mb-4">
            <div className="bg-card rounded-lg border border-border overflow-hidden h-full">
              <TodoList />
            </div>
          </div>
        )}

        {/* 团队区域 */}
        {mode === 'team' && (
          <div className="flex-1 overflow-y-auto">
            <div className="bg-card rounded-lg border border-border overflow-hidden h-full">
              <TeamArea 
                members={teamMembers}
              />
            </div>
          </div>
        )}

        {/* 状态显示 */}
        <div className="toolpanel-status-card">
          <h3 className="toolpanel-status-card__title">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
              <rect x="1" y="8" width="3" height="7" rx="0.5" fill="currentColor" opacity="0.5" />
              <rect x="6" y="4" width="3" height="11" rx="0.5" fill="currentColor" opacity="0.7" />
              <rect x="11" y="1" width="3" height="14" rx="0.5" fill="currentColor" />
            </svg>
            {t('toolPanel.status')}
          </h3>
          <div className="space-y-2">
            <div className="toolpanel-status-card__row">
              <span className="text-text-muted">{t('toolPanel.contextCompression')}</span>
              <span className="mono text-text">{compressionDisplay}</span>
            </div>
            <div className="toolpanel-status-card__row">
              <span className="text-text-muted">{t('toolPanel.memoryUsage')}</span>
              <span className="mono text-text">{memoryDisplay}</span>
            </div>

            <div className={`toolpanel-status-card__heartbeat ${heartbeatClassName}`}>
              <div className="toolpanel-status-card__heartbeat-row">
                <span>{t('toolPanel.message')}</span>
                {canOpenHeartbeatModal ? (
                  <button
                    type="button"
                    className="toolpanel-status-card__heartbeat-link mono"
                    onClick={() => setHeartbeatModalOpen(true)}
                  >
                    {heartbeatDisplayMessage}
                  </button>
                ) : (
                  <span className="toolpanel-status-card__heartbeat-value mono">
                    {heartbeatDisplayMessage}
                  </span>
                )}
              </div>
              <div className="toolpanel-status-card__heartbeat-row">
                <span>{t('toolPanel.time')}</span>
                <span className="toolpanel-status-card__heartbeat-value mono">
                  {heartbeatDetail}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* 底部信息区：与左侧版本信息保持一致 */}
        <div
          className="shrink-0 pt-4 text-text-muted text-center"
          style={{ fontSize: 'var(--font-size-xs)' }}
        >
          <div className="px-2.5">
            <span>{t('toolPanel.poweredBy')}</span>
          </div>
        </div>
      </div>
      <HeartbeatMessageModal
        open={heartbeatModalOpen}
        message={heartbeatMessage ?? ''}
        onClose={() => setHeartbeatModalOpen(false)}
      />
    </div>
  );
}

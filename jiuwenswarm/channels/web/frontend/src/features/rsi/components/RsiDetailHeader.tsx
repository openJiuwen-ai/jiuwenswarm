/**
 * RSI 详情 Header：实验名称 + Tag 信息区 + 右侧操作按钮（状态切换）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { executeDesktopSave, type DesktopSaveApiResult } from '../../../utils/desktopSave';
import completeIcon from '../../../assets/rsi/rsi-complete.svg';
import deleteIcon from '../../../assets/rsi/rsi-delete.svg';
import pauseIcon from '../../../assets/rsi/rsi-pause.svg';
import runningIcon from '../../../assets/rsi/rsi-runing.svg';
import waitingIcon from '../../../assets/rsi/rsi-waiting2.svg';
import type { RsiTaskGetResult, RsiReportGetResult, RsiTreeGetResult } from '../types';
import {
  actionsForStatus,
  typeDisplayLabel,
  statusBadgeInfo,
  formatDateTime,
  type StatusBadgeKind,
  type RsiActionKind,
} from '../rsiPresentation';
import { useRsiStore } from '../rsiStore';
import { usePluginPackageStore } from '../../../stores/pluginPackageStore';
import {
  rsiTaskDelete,
  rsiTrainingPause,
  rsiTrainingResume,
  rsiTrainingTerminate,
  rsiArtifactDownload,
  rsiArtifactDownloadUrl,
  rsiHarnessInstall,
} from '../rsiApi';

type DownloadCapableWindow = Window & {
  pywebview?: {
    api?: {
      download_file?: (url: string, filename: string) => DesktopSaveApiResult;
    };
  };
};

interface RsiDetailHeaderProps {
  task: RsiTaskGetResult;
  report: RsiReportGetResult | null;
  tree: RsiTreeGetResult | null;
  createdAt: string | null;
  onOpenConfig: () => void;
  onOpenArtifact: (path: string, title: string) => void;
}

export function RsiDetailHeader({
  task,
  report,
  tree,
  createdAt,
  onOpenConfig,
  onOpenArtifact,
}: RsiDetailHeaderProps) {
  const { t } = useTranslation();
  const patchTaskStatus = useRsiStore((s) => s.patchTaskStatus);
  const removeListItem = useRsiStore((s) => s.removeListItem);
  const markTaskInstalled = useRsiStore((s) => s.markTaskInstalled);
  const installedTask = useRsiStore((s) => Boolean(s.installedTaskIds[task.task_id]));
  const installed = task.status === 'COMPLETED' && installedTask;
  const [busy, setBusy] = useState(false);
  const [confirmAction, setConfirmAction] = useState<'delete' | 'pause' | 'stop' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const [failureReasonOpen, setFailureReasonOpen] = useState(false);
  const failureReasonAnchorRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!failureReasonOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!failureReasonAnchorRef.current?.contains(event.target as Node)) {
        setFailureReasonOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setFailureReasonOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [failureReasonOpen]);

  useEffect(() => {
    setFailureReasonOpen(false);
  }, [task.task_id, task.failure_reason]);

  const clearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 错误/成功提示 1 秒后自动消失，避免残留
  const clearActionMessages = useCallback(() => {
    if (clearTimerRef.current) {
      clearTimeout(clearTimerRef.current);
      clearTimerRef.current = null;
    }
    setActionError(null);
    setActionSuccess(null);
  }, []);

  const showActionMessage = useCallback((kind: 'error' | 'success', message: string) => {
    if (clearTimerRef.current) {
      clearTimeout(clearTimerRef.current);
      clearTimerRef.current = null;
    }
    if (kind === 'error') {
      setActionError(message);
    } else {
      setActionSuccess(message);
    }
    clearTimerRef.current = setTimeout(() => {
      clearTimerRef.current = null;
      setActionError(null);
      setActionSuccess(null);
    }, 1000);
  }, []);

  // 卸载时清理定时器
  useEffect(() => {
    return () => {
      if (clearTimerRef.current) {
        clearTimeout(clearTimerRef.current);
        clearTimerRef.current = null;
      }
    };
  }, []);

  const runAction = useCallback(
    async (action: RsiActionKind) => {
      if (action === 'config') {
        onOpenConfig();
        return;
      }
      setBusy(true);
      clearActionMessages();
      try {
        if (action === 'pause') {
          const res = await rsiTrainingPause(task.task_id);
          patchTaskStatus(task.task_id, res.status);
        } else if (action === 'resume') {
          const res = await rsiTrainingResume(task.task_id);
          patchTaskStatus(task.task_id, res.status);
        } else if (action === 'stop') {
          const res = await rsiTrainingTerminate(task.task_id);
          patchTaskStatus(task.task_id, res.status);
        } else if (action === 'delete') {
          const result = await rsiTaskDelete(task.task_id);
          if (!result.ok) throw new Error('task delete failed');
          removeListItem(task.task_id);
        } else if (action === 'download') {
          const artifactId =
            report?.metrics.best_artifact_id
            ?? report?.best_artifact?.artifact_id
            ?? task.best_artifact?.artifact_id
            ?? undefined;
          const artifact = await rsiArtifactDownload(task.task_id, artifactId);
          const downloadUrl = rsiArtifactDownloadUrl(artifact);
          if (artifact.is_directory || !downloadUrl) {
            onOpenArtifact(artifact.path, `${task.name} · ${artifact.filename}`);
            return;
          }
          const pywebviewApi = (window as DownloadCapableWindow).pywebview?.api;
          if (pywebviewApi?.download_file) {
            const outcome = await executeDesktopSave(() => pywebviewApi.download_file!(downloadUrl, artifact.filename));
            if (outcome === 'failed') window.alert(t('artifacts.downloadFailed', { name: artifact.filename }));
          } else {
            // 同源时用隐藏 <a download> 触发下载：桌面 WebView 中 window.open 到后端
            // HTTP URL 会直接导航覆盖应用页面，<a download> 则保持下载不导航；
            // 跨源时 download 属性会被浏览器忽略，仍保留 window.open 的网页版行为。
            const sameOrigin =
              new URL(downloadUrl, window.location.href).origin === window.location.origin;
            if (sameOrigin) {
              const link = document.createElement('a');
              link.href = downloadUrl;
              link.download = artifact.filename;
              document.body.appendChild(link);
              link.click();
              link.remove();
            } else {
              window.open(downloadUrl, '_blank', 'noopener,noreferrer');
            }
          }
        } else if (action === 'install') {
          // Harness artifacts are refs, not archives; the RSI installer resolves and registers the package.
          await rsiHarnessInstall(task.task_id);
          markTaskInstalled(task.task_id);
          await usePluginPackageStore.getState().loadList('mine', { silent: true });
          const snapshotName =
            report?.best_artifact?.name || task.best_artifact?.name;
          showActionMessage(
            'success',
            t('rsi.detail.actionInstallSuccess', {
              taskName: task.name,
              snapshotName,
            }),
          );
        }
      } catch (e) {
        const message = e instanceof Error && e.message ? e.message : t('rsi.detail.actionUnknownError');
        showActionMessage('error', t('rsi.detail.actionFailed', { message }));
        console.error('[rsi] action failed', action, e);
      } finally {
        setBusy(false);
      }
    },
    [
      task.task_id,
      task.best_artifact,
      report,
      patchTaskStatus,
      removeListItem,
      markTaskInstalled,
      onOpenConfig,
      onOpenArtifact,
      clearActionMessages,
      showActionMessage,
    ],
  );

  const handleAction = useCallback(
    (action: RsiActionKind) => {
      if (action === 'delete' || action === 'pause' || action === 'stop') {
        setConfirmAction(action);
        return;
      }
      void runAction(action);
    },
    [runAction],
  );

  const handleConfirm = useCallback(async () => {
    if (!confirmAction) return;
    const action = confirmAction;
    setConfirmAction(null);
    await runAction(action);
  }, [confirmAction, runAction]);

  const actions = actionsForStatus(task.status, task.scenario, installed, tree, task.artifact_type);
  const orderedActions = [...actions];
  const deleteIndex = orderedActions.indexOf('delete');
  if (deleteIndex > 0) {
    orderedActions.splice(deleteIndex, 1);
    orderedActions.unshift('delete');
  }

  const actionLabel: Record<RsiActionKind, string> = {
    config: t('rsi.detail.actionConfig'),
    delete: t('rsi.detail.actionDelete'),
    pause: t('rsi.detail.actionPause'),
    resume: t('rsi.detail.actionResume'),
    stop: t('rsi.detail.actionStop'),
    install: t('rsi.detail.actionInstall'),
    download: t('rsi.detail.actionDownload'),
  };
  // 类型标签 + 状态徽章 + 数值标签
  const typeLabel = typeDisplayLabel(task.scenario, task.artifact_type);
  const badge = statusBadgeInfo(task.status, installed);
  const maxIter = t('rsi.detail.tagMaxIterations') + '：' + task.config.max_iterations;
  const createdLabel = t('rsi.detail.tagCreatedAt', { defaultValue: '创建时间' }) + '：' + formatDateTime(createdAt);
  const failureReason = badge.kind === 'failed' ? task.failure_reason : null;

  return (
    <div className="rsi-detail__header">
      <div className="rsi-detail__title-block">
        <div className="rsi-detail__name">{task.name}</div>
        <div className="rsi-detail__tags">
          {badge.kind ? (
            <span className="rsi-detail__tag rsi-detail__tag--status">
              {failureReason ? (
                <span className="rsi-detail__failure-anchor" ref={failureReasonAnchorRef}>
                  <button
                    type="button"
                    className="rsi-detail__failure-trigger"
                    onClick={() => setFailureReasonOpen((open) => !open)}
                    aria-label={t('rsi.detail.failureReason', { defaultValue: '失败原因' })}
                    aria-expanded={failureReasonOpen}
                    aria-controls="rsi-failure-reason-popover"
                    data-testid="rsi-failure-reason-trigger"
                  >
                    <StatusIcon kind={badge.kind} title={failureReason} />
                  </button>
                  {failureReasonOpen && (
                    <div
                      id="rsi-failure-reason-popover"
                      className="rsi-detail__failure-popover"
                      role="dialog"
                      aria-label={t('rsi.detail.failureReason', { defaultValue: '失败原因' })}
                      data-testid="rsi-failure-reason-popover"
                    >
                      {failureReason}
                    </div>
                  )}
                </span>
              ) : (
                <StatusIcon kind={badge.kind} />
              )}
              {t('rsi.detail.' + badge.labelKey)}
            </span>
          ) : (
            <span className="rsi-detail__tag rsi-detail__tag--status">{t('rsi.detail.' + badge.labelKey)}</span>
          )}
          <span className="rsi-detail__divider" />
          <span className="rsi-detail__tag rsi-detail__tag--meta">
            {t('rsi.detail.tagType')}：{typeLabel}
          </span>
          <span className="rsi-detail__divider" />
          <span className="rsi-detail__tag rsi-detail__tag--meta">{maxIter}</span>
          <span className="rsi-detail__divider" />
          <span className="rsi-detail__tag rsi-detail__tag--meta">{createdLabel}</span>
        </div>
      </div>
      <div className="rsi-detail__header-actions">
        {actionSuccess && (
          <div className="rsi-detail__action-success" role="status">
            <img className="rsi-detail__action-success-icon" src={completeIcon} alt="" aria-hidden />
            <span className="rsi-detail__action-success-text">{actionSuccess}</span>
            <button
              type="button"
              className="rsi-detail__action-success-close"
              onClick={() => setActionSuccess(null)}
              aria-label={t('rsi.detail.actionClose')}
            >
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden>
                <path d="M2.5 2.5L9.5 9.5M9.5 2.5L2.5 9.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        )}
        <div className="rsi-detail__actions">
          {orderedActions.map((action) => {
            const className =
              action === 'delete'
                ? 'rsi-btn rsi-detail__action--delete'
                : `rsi-btn ${action === 'config' ? 'rsi-btn--ghost' : 'rsi-btn--primary'}`;
            return (
              <button
                key={action}
                type="button"
                className={className}
                onClick={() => handleAction(action)}
                disabled={busy}
                aria-label={action === 'delete' ? actionLabel.delete : undefined}
                data-testid={`rsi-action-${action}`}
              >
                {action === 'delete' && (
                  <img className="rsi-detail__action-icon" src={deleteIcon} alt={actionLabel.delete} />
                )}
                {action !== 'delete' && actionLabel[action]}
              </button>
            );
          })}
        </div>
        {actionError && (
          <div className="rsi-detail__action-error" role="alert">
            {actionError}
          </div>
        )}
      </div>
      {confirmAction && (
        <div className="rsi-confirm-overlay" role="presentation">
          <div className="rsi-confirm" role="dialog" aria-modal="true" aria-labelledby="rsi-confirm-title">
            <div className="rsi-confirm__title" id="rsi-confirm-title">
              {t(
                confirmAction === 'delete'
                  ? 'rsi.detail.deleteTitle'
                  : confirmAction === 'stop'
                    ? 'rsi.detail.stopTitle'
                    : 'rsi.detail.pauseTitle',
              )}
            </div>
            <div className="rsi-confirm__body">
              {t(
                confirmAction === 'delete'
                  ? 'rsi.detail.deleteBody'
                  : confirmAction === 'stop'
                    ? 'rsi.detail.stopBody'
                    : 'rsi.detail.pauseBody',
              )}
            </div>
            <div className="rsi-confirm__actions">
              <button
                type="button"
                className="rsi-btn rsi-btn--ghost rsi-confirm__btn"
                onClick={() => setConfirmAction(null)}
              >
                {t('rsi.detail.actionCancel')}
              </button>
              <button
                type="button"
                className="rsi-btn rsi-btn--primary rsi-confirm__btn"
                onClick={() => void handleConfirm()}
                disabled={busy}
              >
                {t('rsi.detail.actionConfirm')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const STATUS_ICON_SRCS: Partial<Record<StatusBadgeKind, string>> = {
  queued: waitingIcon,
  running: runningIcon,
  paused: pauseIcon,
  completed: completeIcon,
  installed: completeIcon,
};

function StatusIcon({ kind, title }: { kind: StatusBadgeKind; title?: string }) {
  const iconSrc = STATUS_ICON_SRCS[kind];
  if (iconSrc) {
    return <img className="rsi-detail__status-icon" src={iconSrc} alt="" title={title} aria-hidden />;
  }
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      fill="none"
      role={title ? 'img' : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      {title ? <title>{title}</title> : null}
      <circle cx="8" cy="8" r="7" fill="rgb(239,68,68)" />
      <path
        d="M8 4.5C7.6 4.5 7.25 4.85 7.25 5.25L7.25 8.5C7.25 8.9 7.6 9.25 8 9.25C8.4 9.25 8.75 8.9 8.75 8.5L8.75 5.25C8.75 4.85 8.4 4.5 8 4.5ZM8 11.5C8.41 11.5 8.75 11.16 8.75 10.75C8.75 10.34 8.41 10 8 10C7.59 10 7.25 10.34 7.25 10.75C7.25 11.16 7.59 11.5 8 11.5Z"
        fill="white"
        fillRule="nonzero"
      />
    </svg>
  );
}

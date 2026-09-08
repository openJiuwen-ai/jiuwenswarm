/**
 * RSI 详情 Header：实验名称 + Tag 信息区 + 右侧操作按钮（状态切换）。
 */
import { useCallback, useState } from 'react';
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
  formatCost,
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
  liveCost: number | null;
  createdAt: string | null;
  onOpenConfig: () => void;
  onOpenArtifact: (path: string, title: string) => void;
}

export function RsiDetailHeader({
  task,
  report,
  tree,
  liveCost,
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

  const runAction = useCallback(
    async (action: RsiActionKind) => {
      if (action === 'config') {
        onOpenConfig();
        return;
      }
      setBusy(true);
      setActionError(null);
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
            window.open(downloadUrl, '_blank', 'noopener,noreferrer');
          }
        } else if (action === 'install') {
          await rsiHarnessInstall(task.task_id);
          markTaskInstalled(task.task_id);
          await usePluginPackageStore.getState().loadList('local', { silent: true });
        }
      } catch (e) {
        const message = e instanceof Error && e.message ? e.message : t('rsi.detail.actionUnknownError');
        setActionError(t('rsi.detail.actionFailed', { message }));
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

  const actions = actionsForStatus(task.status, task.scenario, installed, tree);
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
              <StatusIcon kind={badge.kind} title={failureReason ?? undefined} />
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
        <div className="rsi-detail__actions">
          {liveCost != null && (
            <span className="rsi-canvas-area__cost" style={{ marginRight: 4 }}>
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.5}
                aria-hidden
              >
                <text x="12" y="18" textAnchor="middle" fontSize="16" fill="currentColor" stroke="none">
                  ¥
                </text>
              </svg>
              {t('rsi.detail.estimatedCost', { cost: formatCost(liveCost) })}
            </span>
          )}
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

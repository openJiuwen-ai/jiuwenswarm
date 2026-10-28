// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useEffect } from 'react';
import { AlertCircle, CheckCircle2, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ExternalCliAgentKind, ExternalCliDependencyInstallStatus } from './ExternalCliAgentsSection';
import './ExternalCliInstallDialog.css';

type StatusCallback = (agent: ExternalCliAgentKind, status: ExternalCliDependencyInstallStatus) => void;

/**
 * Polls each running install once per second and reports fresh statuses
 * upward. Accepts an empty list to idle; `agents` may be a new array on
 * every render (keyed by the joined agent names).
 */
export function useInstallStatusPolling(
  agents: ExternalCliAgentKind[],
  onGetStatus: (agent: ExternalCliAgentKind) => Promise<ExternalCliDependencyInstallStatus>,
  onStatusChange: StatusCallback,
) {
  const agentKey = agents.join(',');
  useEffect(() => {
    const runningAgents = agentKey.split(',').filter(Boolean) as ExternalCliAgentKind[];
    if (!runningAgents.length) return undefined;
    let cancelled = false;
    const poll = async () => {
      await Promise.all(
        runningAgents.map(async (agent) => {
          try {
            const next = await onGetStatus(agent);
            if (!cancelled) onStatusChange(agent, next);
          } catch (error) {
            if (cancelled) return;
            onStatusChange(agent, {
              cli_agent: agent,
              status: 'failed',
              phase: 'failed',
              error: error instanceof Error ? error.message : String(error),
            });
          }
        }),
      );
    };
    const timer = window.setInterval(() => void poll(), 1000);
    void poll();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [agentKey, onGetStatus, onStatusChange]);
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  const digits = amount >= 100 || unitIndex === 0 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[unitIndex]}`;
}

function formatEta(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  const rounded = Math.ceil(seconds);
  if (rounded < 60) return `${rounded}s`;
  const minutes = Math.ceil(rounded / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/** One agent's install task card (progress bar, metrics, retries, activity log). */
export function ExternalCliInstallTaskCard({
  agent,
  status,
}: {
  agent: ExternalCliAgentKind;
  status: ExternalCliDependencyInstallStatus;
}) {
  const { t } = useTranslation();
  const downloaded = Number(status.downloaded_bytes || 0);
  const total = Number(status.total_bytes || 0);
  const percent = total > 0 ? Math.min(100, (downloaded / total) * 100) : 0;
  const percentLabel = downloaded > 0 && percent < 1 ? '<1%' : `${Math.floor(percent)}%`;
  const installerActivity = status.progress_kind === 'installer_activity';
  const showProgress = status.status === 'running' && (installerActivity || status.phase === 'downloading');
  const determinateProgress = status.phase === 'downloading' && total > 0;
  const latestLog = String(status.last_log || status.log_tail?.at(-1) || '').trim();
  const agentName = agent === 'claude' ? 'Claude' : 'Codex';
  const downloadAttempt = Number(status.download_attempt || 0);
  const downloadMaxAttempts = Number(status.download_max_attempts || 0);
  let retryMessage = '';
  if (status.switching_source) {
    retryMessage = t('config.externalCli.installSwitchingSource');
  } else if (status.phase === 'downloading' && downloadAttempt > 1 && downloadMaxAttempts > 0) {
    retryMessage = t('config.externalCli.installRetry', {
      current: downloadAttempt,
      total: downloadMaxAttempts,
    });
  }
  const artifactLabel =
    status.phase === 'downloading' && status.artifact_count && status.artifact_index
      ? t('config.externalCli.installArtifact', {
          current: status.artifact_index,
          total: status.artifact_count,
          package: status.current_package || agentName,
          version: status.current_version || '',
        })
      : '';
  return (
    <section className="external-cli-install-dialog__task">
      <div className="external-cli-install-dialog__task-heading">
        <span className="external-cli-install-dialog__agent">{agentName}</span>
        <span className={`external-cli-install-dialog__state external-cli-install-dialog__state--${status.status}`}>
          {status.status === 'succeeded' ? <CheckCircle2 aria-hidden="true" /> : null}
          {status.status === 'failed' ? <AlertCircle aria-hidden="true" /> : null}
          {status.status === 'running' ? (
            <Loader2 className="external-cli-install-dialog__spinner" aria-hidden="true" />
          ) : null}
          {t(`config.externalCli.installPhase.${status.phase || status.status || 'preparing'}`)}
        </span>
      </div>
      {artifactLabel ? <div className="external-cli-install-dialog__artifact">{artifactLabel}</div> : null}
      {showProgress ? (
        <>
          <div
            className={`external-cli-install-dialog__progress${determinateProgress ? '' : ' external-cli-install-dialog__progress--indeterminate'}`}
            aria-label={t('config.externalCli.installProgress')}
          >
            <span style={determinateProgress ? { width: `${percent}%` } : undefined} />
          </div>
          {status.phase === 'downloading' ? (
            <div className="external-cli-install-dialog__metrics">
              <span>
                {total > 0 ? `${formatBytes(downloaded)} / ${formatBytes(total)}` : formatBytes(downloaded)}
              </span>
              {total > 0 ? <strong>{percentLabel}</strong> : null}
              {Number(status.bytes_per_second) > 0 ? (
                <span>{formatBytes(Number(status.bytes_per_second))}/s</span>
              ) : null}
              {Number(status.eta_seconds) > 0 ? (
                <span>{t('config.externalCli.installEta', { eta: formatEta(Number(status.eta_seconds)) })}</span>
              ) : null}
            </div>
          ) : null}
          {retryMessage ? <div className="external-cli-install-dialog__retry">{retryMessage}</div> : null}
        </>
      ) : null}
      {status.status === 'running' && installerActivity && latestLog ? (
        <div className="external-cli-install-dialog__activity" aria-live="polite">
          <span>{t('config.externalCli.installActivity')}</span>
          <code>{latestLog}</code>
        </div>
      ) : null}
      {status.error ? <div className="external-cli-install-dialog__error">{status.error}</div> : null}
    </section>
  );
}

/**
 * Single-agent install progress block, rendered inline inside the agent
 * card (replacing the "not installed" hint while an install runs): just
 * the bare progress bar, plus the error line when the install fails.
 */
export function ExternalCliInstallProgress({
  agent,
  status,
  onGetStatus,
  onStatusChange,
  inline = false,
}: {
  agent: ExternalCliAgentKind;
  status: ExternalCliDependencyInstallStatus;
  onGetStatus: (agent: ExternalCliAgentKind) => Promise<ExternalCliDependencyInstallStatus>;
  onStatusChange: StatusCallback;
  inline?: boolean;
}) {
  const { t } = useTranslation();
  useInstallStatusPolling(status.status === 'running' ? [agent] : [], onGetStatus, onStatusChange);
  const downloaded = Number(status.downloaded_bytes || 0);
  const total = Number(status.total_bytes || 0);
  const percent = total > 0 ? Math.min(100, (downloaded / total) * 100) : 0;
  // After success the bar stays pinned at 100% while the card waits for its
  // detect result to settle, then the card swaps to the installed layout.
  const succeeded = status.status === 'succeeded';
  const determinateProgress = succeeded || (status.phase === 'downloading' && total > 0);
  return (
    <div
      className={
        inline
          ? 'external-cli-install-dialog__body external-cli-install-dialog__body--inline'
          : 'external-cli-install-dialog__body'
      }
      data-testid="settings-panel-external-cli-agent-install-progress"
      data-variant={agent}
    >
      {status.status === 'running' || succeeded ? (
        <div
          className={`external-cli-install-dialog__progress${determinateProgress ? '' : ' external-cli-install-dialog__progress--indeterminate'}`}
          aria-label={t('config.externalCli.installProgress')}
        >
          <span style={determinateProgress ? { width: `${succeeded ? 100 : percent}%` } : undefined} />
        </div>
      ) : null}
      {status.error ? <div className="external-cli-install-dialog__error">{status.error}</div> : null}
    </div>
  );
}

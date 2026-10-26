/**
 * HarnessProgressBar Component
 *
 * Displays stage progress during auto_harness execution.
 * Shows stages with Chinese labels and a visual progress bar.
 * Stages: 评估当前状态 -> 制定优化计划 -> 执行代码修改 -> CI 门禁检查 -> 提交变更 -> 发布 PR -> 总结经验
 */

import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useHarnessStore, useSessionStore, useChatStore } from '../../stores';
import type { ExtensionProgressInfo, ExtensionProgressStatus } from '../../stores/harnessStore';
import clsx from 'clsx';

interface StageStatusIconProps {
  status: 'pending' | 'running' | 'success' | 'failed' | 'timeout';
}

function StageStatusIcon({ status }: StageStatusIconProps) {
  const { t } = useTranslation();

  // Don't show icon for pending status
  if (status === 'pending') {
    return <span className="harness-stage-icon harness-stage-pending" />;
  }

  if (status === 'success') {
    return (
      <span className="harness-stage-icon harness-stage-success" title={t('autoHarness.stageSuccess')}>
        ✓
      </span>
    );
  }

  if (status === 'failed') {
    return (
      <span className="harness-stage-icon harness-stage-failed" title={t('autoHarness.stageFailed')}>
        ✗
      </span>
    );
  }

  if (status === 'running') {
    return (
      <span className="harness-stage-icon harness-stage-running" title={t('autoHarness.stageRunning')}>
        <span className="harness-spinner" />
      </span>
    );
  }

  if (status === 'timeout') {
    return (
      <span className="harness-stage-icon harness-stage-timeout" title={t('autoHarness.stageFailed')}>
        ◐
      </span>
    );
  }

  return null;
}

interface StageItemProps {
  stageLabel: string;
  status: 'pending' | 'running' | 'success' | 'failed' | 'timeout';
  isCurrent: boolean;
  metrics?: Record<string, unknown>;
  messages?: string[];
  error?: string;
  children?: React.ReactNode;
  defaultExpanded?: boolean;
}

function StageItem({ stageLabel, status, isCurrent, metrics, messages, error, children, defaultExpanded }: StageItemProps) {
  const [expanded, setExpanded] = React.useState(Boolean(defaultExpanded));
  const { t } = useTranslation();
  const visibleMessages = (messages || []).filter((message) => {
    const normalized = message.trim();
    return (
      !normalized.startsWith('Gaps:')
      && !normalized.startsWith('Designs:')
      && !normalized.startsWith('Gap analysis complete:')
      && !normalized.startsWith('Extension design complete:')
      && !normalized.startsWith('扩展设计已保存:')
    );
  });

  useEffect(() => {
    if (defaultExpanded) {
      setExpanded(true);
    }
  }, [defaultExpanded]);

  const hasDetails = Boolean(children) || visibleMessages.length > 0 || (metrics && Object.keys(metrics).length > 0) || error;

  return (
    <div className={clsx('harness-stage-item', {
      'harness-stage-current': isCurrent,
      'harness-stage-success': status === 'success',
      'harness-stage-failed': status === 'failed' || status === 'timeout',
      'harness-stage-running': status === 'running',
    })}>
      <button
        className="harness-stage-header"
        onClick={() => hasDetails && setExpanded(!expanded)}
        disabled={!hasDetails}
      >
        <StageStatusIcon status={status} />
        <span className="harness-stage-name">{stageLabel}</span>
        {hasDetails && (
          <span className={clsx('harness-stage-expand-icon', { expanded })}>
            ▾
          </span>
        )}
      </button>

      {expanded && hasDetails && (
        <div className="harness-stage-details">
          {children && (
            <div className="harness-stage-inline-details">
              {children}
            </div>
          )}
          {error && (
            <div className="harness-stage-error">
              <strong>Error:</strong> {error}
            </div>
          )}
          {visibleMessages.length > 0 && (
            <div className="harness-stage-messages">
              <strong>{t('autoHarness.stageMessage')}:</strong>
              <ul>
                {visibleMessages.map((msg, idx) => (
                  <li key={idx}>{msg}</li>
                ))}
              </ul>
            </div>
          )}
          {metrics && Object.keys(metrics).length > 0 && (
            <div className="harness-stage-metrics">
              <strong>Metrics:</strong>
              <div className="harness-metrics-grid">
                {Object.entries(metrics).map(([key, value]) => (
                  <div key={key} className="harness-metric-item">
                    <span className="harness-metric-key">{key}:</span>
                    <span className="harness-metric-value">
                      {typeof value === 'number' ? (Number.isFinite(value) ? value.toFixed(2) : value) : String(value)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ExtensionStatusIcon({ status }: { status: ExtensionProgressStatus }) {
  if (status === 'success') return <span className="harness-extension-status success">✓</span>;
  if (status === 'failed' || status === 'timeout') return <span className="harness-extension-status failed">×</span>;
  if (status === 'running') return <span className="harness-extension-status running">◉</span>;
  if (status === 'skipped') return <span className="harness-extension-status skipped">↷</span>;
  if (status === 'rejected') return <span className="harness-extension-status failed">×</span>;
  if (status === 'waiting') return <span className="harness-extension-status pending">○</span>;
  return <span className="harness-extension-status pending">○</span>;
}

function statusLabel(status: ExtensionProgressStatus): string {
  if (status === 'success') return '已完成';
  if (status === 'running') return '进行中';
  if (status === 'failed') return '失败';
  if (status === 'timeout') return '超时';
  if (status === 'skipped') return '已跳过';
  if (status === 'rejected') return '已拒绝';
  if (status === 'waiting') return '等待确认';
  return '未开始';
}

function resolveActivateDisplayStatus(
  rows: ExtensionProgressInfo[],
  stageStatus: 'pending' | 'running' | 'success' | 'failed' | 'timeout',
): ExtensionProgressStatus {
  const merged = rows.find((row) => row.extensionName === 'merged_extensions');
  if (merged) return merged.activateStatus;
  if (rows.length === 1) return rows[0].activateStatus;
  if (rows.length === 0) {
    if (stageStatus === 'running') return 'running';
    if (stageStatus === 'success') return 'success';
    if (stageStatus === 'failed') return 'failed';
    if (stageStatus === 'timeout') return 'timeout';
    return 'pending';
  }

  // Multi-row fallback: pick the worst status.
  const priority: ExtensionProgressStatus[] = [
    'failed',
    'timeout',
    'rejected',
    'running',
    'waiting',
    'pending',
    'skipped',
    'success',
  ];
  for (const item of priority) {
    if (rows.some((row) => row.activateStatus === item)) return item;
  }
  return 'pending';
}

function parseNamedList(messages: string[], prefix: string): string[] {
  const values: string[] = [];
  for (const message of messages) {
    const normalized = message.trim();
    if (!normalized.startsWith(prefix)) continue;
    const raw = normalized.slice(prefix.length).trim();
    const parts = prefix === 'Designs:' ? raw.split(',') : raw.split(';');
    for (const part of parts) {
      const value = part.trim();
      if (value && !values.includes(value)) {
        values.push(value);
      }
    }
  }
  return values;
}

function StageSummaryList({
  items,
  stageStatus,
  itemLabel,
  emptyLabel,
}: {
  items: string[];
  stageStatus: 'pending' | 'running' | 'success' | 'failed' | 'timeout';
  itemLabel: string;
  emptyLabel: string;
}) {
  if (items.length === 0) {
    return <div className="harness-stage-empty">{emptyLabel}</div>;
  }
  return (
    <div className="harness-extension-list">
      {items.map((item) => (
        <div key={item} className="harness-extension-row">
          <div className="harness-extension-name" title={item}>{item}</div>
          <div className="harness-extension-flow">
            <span>
              <ExtensionStatusIcon status={stageStatus === 'running' ? 'running' : 'success'} />
              {itemLabel}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

export function HarnessProgressBar() {
    const { t } = useTranslation();
    const activeSessionId = useChatStore((s) => s.activeSessionId);
    const stageResults = useHarnessStore((s) => s.runtimes[activeSessionId ?? '']?.stageResults ?? []);
    const stageDefinitions = useHarnessStore((s) => s.runtimes[activeSessionId ?? '']?.stageDefinitions ?? []);
    const currentStage = useHarnessStore((s) => s.runtimes[activeSessionId ?? '']?.currentStage);
    const progressPercent = useHarnessStore((s) => s.runtimes[activeSessionId ?? '']?.progressPercent ?? 0);
    const extensionOrder = useHarnessStore((s) => s.runtimes[activeSessionId ?? '']?.extensionOrder ?? []);
    const extensionsByName = useHarnessStore((s) => s.runtimes[activeSessionId ?? '']?.extensionsByName ?? {});
    const mode = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.mode ?? 'agent');
    const isProcessing = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.isProcessing ?? false);
    const [expanded, setExpanded] = useState(true);

    // Show when in auto_harness mode and:
    // 1. Currently processing/running, OR
    // 2. Has stages defined (even after cancel, to show final state)
    const shouldShow = mode === 'auto_harness' && (isProcessing || stageResults.length > 0);

    // Don't render if not in auto_harness mode
    if (!shouldShow || stageResults.length === 0) {
      return null;
    }

    // Check if any stage has started (not all pending)
    const hasStarted = stageResults.some(s => s.status !== 'pending');

    // Check if task is completed (all stages are success/failed/timeout)
    const isCompleted = !isProcessing && stageResults.every(s => s.status !== 'pending' && s.status !== 'running');

    return (
      <div className="harness-progress-bar">
        <button
          className="harness-progress-header"
          onClick={() => setExpanded(!expanded)}
          type="button"
        >
          <span className="harness-progress-title">
            {isCompleted ? t('autoHarness.completed') : t('autoHarness.running')}
          </span>
          <span className="harness-progress-percent">{progressPercent}%</span>
          <span className={clsx('harness-progress-expand-icon', { expanded })}>
            ▾
          </span>
        </button>

        {/* Visual progress bar - only show markers when started */}
        <div className="harness-progress-track">
          <div
            className="harness-progress-fill"
            style={{ width: `${progressPercent}%` }}
          />
          {/* Stage markers on the progress bar - only visible after started */}
          {hasStarted && stageResults.map((stageInfo, index) => {
            // Calculate marker position: distribute evenly, or single stage at center
            const markerPosition = stageResults.length > 1
              ? (index / (stageResults.length - 1)) * 100
              : 50;
            const markerStatus = stageInfo.status;
            // Only show marker if stage is not pending
            if (markerStatus === 'pending') return null;
            // Get label: from stage definition, harness.message content, or i18n fallback, or stage key
            const definitionLabel = stageDefinitions.find(d => d.slot === stageInfo.stage)?.display_name;
            const label = stageInfo.stageLabel || definitionLabel || t(`autoHarness.stages.${stageInfo.stage}`) || stageInfo.stage;
            return (
              <div
                key={stageInfo.stage}
                className={clsx('harness-progress-marker', {
                  'harness-marker-success': markerStatus === 'success',
                  'harness-marker-failed': markerStatus === 'failed' || markerStatus === 'timeout',
                  'harness-marker-running': markerStatus === 'running',
                  'harness-marker-current': currentStage === stageInfo.stage,
                })}
                style={{ left: `${markerPosition}%` }}
                title={label}
              />
            );
          })}
        </div>

        {/* Stage details list - collapsible */}
        {expanded && (
          <div className="harness-progress-stages">
            {stageResults.map((stageInfo) => {
              // Get label: from stage definition, harness.message content, or i18n fallback, or stage key
              const definitionLabel = stageDefinitions.find(d => d.slot === stageInfo.stage)?.display_name;
              const label = stageInfo.stageLabel || definitionLabel || t(`autoHarness.stages.${stageInfo.stage}`) || stageInfo.stage;
              const extensionRows = extensionOrder.map((name) => extensionsByName[name]).filter(Boolean);
              const gapItems = stageInfo.stage === 'assess'
                ? parseNamedList(stageInfo.messages, 'Gaps:')
                : [];
              const designItems = stageInfo.stage === 'plan'
                ? parseNamedList(stageInfo.messages, 'Designs:')
                : [];
              const buildVerifyRows = stageInfo.stage === 'build_verify' ? (
                <div className="harness-extension-list">
                  {extensionRows.map((ext) => (
                    <div key={ext.extensionName} className="harness-extension-row">
                      <div className="harness-extension-name" title={ext.extensionName}>{ext.extensionName}</div>
                      <div className="harness-extension-flow">
                        <span><ExtensionStatusIcon status={ext.implementStatus} />实现扩展</span>
                        <span className="harness-extension-arrow">→</span>
                        <span><ExtensionStatusIcon status={ext.verifyStatus} />验证扩展</span>
                      </div>
                    </div>
                  ))}
                </div>
              ) : null;
              const activateDisplayStatus = resolveActivateDisplayStatus(
                extensionRows,
                stageInfo.status,
              );
              const activateRows = stageInfo.stage === 'activate' ? (
                <div className="harness-extension-list">
                  <div className="harness-extension-row">
                    <div className="harness-extension-flow">
                      <span>
                        <ExtensionStatusIcon status={activateDisplayStatus} />
                        {statusLabel(activateDisplayStatus)}
                      </span>
                    </div>
                  </div>
                </div>
              ) : null;
              return (
                <StageItem
                  key={stageInfo.stage}
                  stageLabel={label}
                  status={stageInfo.status}
                  isCurrent={currentStage === stageInfo.stage}
                  metrics={stageInfo.metrics}
                  messages={stageInfo.messages}
                  error={stageInfo.error}
                  defaultExpanded={currentStage === stageInfo.stage || stageInfo.stage === 'assess' || stageInfo.stage === 'plan'}
                >
                  {stageInfo.stage === 'assess' && (
                    <StageSummaryList
                      items={gapItems}
                      stageStatus={stageInfo.status}
                      itemLabel="关键 gap"
                      emptyLabel="暂无 gap 摘要"
                    />
                  )}
                  {stageInfo.stage === 'plan' && (
                    <StageSummaryList
                      items={designItems}
                      stageStatus={stageInfo.status}
                      itemLabel="设计方案"
                      emptyLabel="暂无设计摘要"
                    />
                  )}
                  {buildVerifyRows}
                  {activateRows}
                </StageItem>
              );
            })}
          </div>
        )}
      </div>
    );
  }

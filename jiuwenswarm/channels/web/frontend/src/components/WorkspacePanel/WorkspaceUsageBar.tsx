import { useTranslation } from 'react-i18next';
import { isEnterprise } from '../../edition';
import {
  formatBytes,
  formatPercent,
} from '../../features/workspace/workspaceApi';
import type { WorkspaceUsageData } from '../../features/workspace/workspaceTypes';

interface WorkspaceUsageBarProps {
  usage: WorkspaceUsageData | null;
  loading?: boolean;
  onRequestExpand?: () => void;
  onRefresh?: () => void;
}

function isUnlimited(usage: WorkspaceUsageData | null | undefined): boolean {
  if (!usage) return false;
  if (usage.unlimited === true) return true;
  return usage.limit_bytes === -1;
}

function barColor(status: string | undefined): string {
  if (status === 'block') return 'bg-danger';
  if (status === 'warn') return 'bg-warn';
  return 'bg-accent';
}

function statusLabel(
  status: string | undefined,
  t: (key: string) => string,
): string {
  if (status === 'block') return t('agent.quota.block');
  if (status === 'warn') return t('agent.quota.warn');
  return t('agent.quota.ok');
}

export function WorkspaceUsageBar({
  usage,
  loading,
  onRequestExpand,
  onRefresh,
}: WorkspaceUsageBarProps) {
  const { t } = useTranslation();
  const unlimited = isUnlimited(usage);
  const percent = usage && !unlimited ? Math.min(100, Math.max(0, usage.percent)) : 0;
  // 扩容仅企业版；个人版不展示入口
  const showExpand = Boolean(onRequestExpand) && isEnterprise();
  // 有限额即可申请扩容（含用量正常）；仅无用量数据或无限制配额时禁用
  const expandDisabled = !usage || unlimited;

  let summary: string;
  if (loading && !usage) {
    summary = t('common.loading');
  } else if (!usage) {
    summary = t('agent.quota.unavailable');
  } else if (unlimited) {
    summary = `${formatBytes(usage.used_bytes)} / ${t('agent.quota.unlimited')} · ${statusLabel(usage.status, t)}`;
  } else {
    summary = `${formatBytes(usage.used_bytes)} / ${formatBytes(usage.limit_bytes)} · ${formatPercent(usage.percent)} · ${statusLabel(usage.status, t)}`;
  }

  return (
    <div className="rounded-xl border border-border bg-card/70 px-4 py-3 flex flex-col gap-2">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <div className="text-sm font-medium text-text">{t('agent.quota.title')}</div>
          <div className="text-xs text-text-muted mt-0.5 mono">{summary}</div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {onRefresh ? (
            <button
              type="button"
              className="btn !px-3 !py-1.5"
              onClick={onRefresh}
              disabled={loading}
            >
              {loading ? t('common.refreshing') : t('common.refresh')}
            </button>
          ) : null}
          {showExpand ? (
            <button
              type="button"
              className="btn primary !px-3 !py-1.5"
              onClick={onRequestExpand}
              disabled={expandDisabled}
              title={
                unlimited
                  ? t('agent.quota.expandUnlimitedHint')
                  : t('agent.quota.expandHint')
              }
            >
              {t('agent.quota.requestExpand')}
            </button>
          ) : null}
        </div>
      </div>
      <div className="h-2 rounded-full bg-secondary overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${barColor(unlimited ? 'ok' : usage?.status)}`}
          style={{ width: unlimited ? '0%' : `${percent}%` }}
        />
      </div>
    </div>
  );
}

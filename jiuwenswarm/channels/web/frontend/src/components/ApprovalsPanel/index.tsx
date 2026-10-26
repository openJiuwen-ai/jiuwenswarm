/**
 * 用户面「我的申请」面板（通用审批单列表）。
 * 布局与视觉对齐定时任务列表：页头、统计 pill、搜索筛选、表格操作链。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Search } from 'lucide-react';
import {
  cancelMyApproval,
  listApproverCandidates,
  listMyApprovals,
  type ApprovalApprover,
  type ApprovalOrderItem,
  type ApprovalStatus,
} from '../../features/approvals/approvalsApi';
import {
  createWorkspaceExpandRequest,
  fetchWorkspaceUsage,
  formatBytes,
} from '../../features/workspace/workspaceApi';
import { isEnterprise } from '../../edition';
import { hasManagerSessionCredentials } from '../../auth/manager/authSession';
import { BoldRingIcon, RunningIcon } from '../CronPanel/StatusBadge';
import { Pagination } from '../common/Pagination';
import { TableColumnFilter } from '../common/TableColumnFilter';
import { ApproverPicker } from '../common/ApproverPicker';
import { copyHistoryText } from '../A2AIngressPanel/copyHistoryText';

const WORKSPACE_QUOTA_EXPAND = 'workspace_quota_expand';
const GIB = 1024 ** 3;
const PAGE_SIZE_OPTIONS = [10, 20, 50];
const DEFAULT_PAGE_SIZE = 20;
const STATUS_FILTERS: ApprovalStatus[] = ['pending', 'approved', 'rejected', 'cancelled'];

function businessLabel(businessType: string, t: (key: string) => string): string {
  const key = `approvals.business.${businessType}`;
  const translated = t(key);
  return translated === key ? businessType : translated;
}

function requestedQuotaText(item: ApprovalOrderItem): string {
  if (item.business_type !== WORKSPACE_QUOTA_EXPAND) return '—';
  const requested = Number(item.apply_data?.requested_limit_bytes);
  if (Number.isFinite(requested) && requested > 0) return formatBytes(requested);
  return '—';
}

function currentApprovers(item: ApprovalOrderItem): ApprovalApprover[] {
  if (item.status === 'pending' && item.current_approvers?.length) {
    return item.current_approvers.filter((approver) => (approver.user_id || '').trim());
  }
  const fallbackId = (item.approver_id || '').trim();
  if (!fallbackId) return [];
  return [{ user_id: fallbackId, display_name: fallbackId }];
}

function CurrentApproversCell({ item }: { item: ApprovalOrderItem }) {
  const approvers = currentApprovers(item);
  if (!approvers.length) return <span className="text-text-muted">—</span>;
  return (
    <div className="flex w-[8.5rem] max-w-[8.5rem] flex-col gap-1">
      {approvers.map((approver) => {
        const id = (approver.user_id || '').trim();
        const name = (approver.display_name || '').trim() || id;
        const label = `${name}(${id})`;
        return (
          <div
            key={id}
            className="break-all text-sm text-text"
            title={label}
          >
            {label}
          </div>
        );
      })}
    </div>
  );
}

function buildApprovalDetailLink(orderNum: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  return `${origin}/manager/approvals/${encodeURIComponent(orderNum)}`;
}

function canResubmit(item: ApprovalOrderItem): boolean {
  return (
    item.business_type === WORKSPACE_QUOTA_EXPAND &&
    (item.status === 'rejected' || item.status === 'cancelled')
  );
}

function bytesToGiBInput(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  const gib = bytes / GIB;
  return Number.isInteger(gib) ? String(gib) : gib.toFixed(1);
}

function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const label = t(`approvals.status.${status}`, { defaultValue: status });
  if (status === 'pending') {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm text-cron-running">
        <RunningIcon />
        {label}
      </span>
    );
  }
  if (status === 'approved') {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm text-ok">
        <BoldRingIcon />
        {label}
      </span>
    );
  }
  if (status === 'rejected') {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm text-danger">
        <BoldRingIcon />
        {label}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm text-text-muted">
      <BoldRingIcon />
      {label}
    </span>
  );
}

function StatPill({
  icon,
  label,
  count,
  active = false,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  count: number;
  active?: boolean;
  onClick?: () => void;
}) {
  const className = [
    'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors',
    active
      ? 'border-accent bg-accent/10 text-text-strong'
      : 'border-border bg-card text-text',
    onClick ? 'cursor-pointer hover:border-accent/60 hover:bg-secondary/50' : '',
  ]
    .filter(Boolean)
    .join(' ');

  if (!onClick) {
    return (
      <span className={className}>
        {icon}
        {label} {count}
      </span>
    );
  }

  return (
    <button
      type="button"
      className={className}
      aria-pressed={active}
      onClick={onClick}
    >
      {icon}
      {label} {count}
    </button>
  );
}

function Th({ children, first }: { children: React.ReactNode; first?: boolean }) {
  return (
    <th className="py-3 font-medium">
      <span className={`inline-block ${first ? 'px-4' : 'border-l border-border pl-4 pr-4'}`}>{children}</span>
    </th>
  );
}

export function ApprovalsPanel() {
  const { t } = useTranslation();
  const enabled = isEnterprise() && hasManagerSessionCredentials();
  const [items, setItems] = useState<ApprovalOrderItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyOrder, setBusyOrder] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const searchDebounceRef = useRef<number | null>(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [businessTypeFilter, setBusinessTypeFilter] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [copyHint, setCopyHint] = useState<string | null>(null);

  const [resubmitItem, setResubmitItem] = useState<ApprovalOrderItem | null>(null);
  const [resubmitLimitGiB, setResubmitLimitGiB] = useState('');
  const [resubmitReason, setResubmitReason] = useState('');
  const [resubmitApproverId, setResubmitApproverId] = useState('');
  const [resubmitApprovers, setResubmitApprovers] = useState<ApprovalApprover[]>([]);
  const [resubmitApproversLoading, setResubmitApproversLoading] = useState(false);
  const [resubmitBusy, setResubmitBusy] = useState(false);
  const [resubmitError, setResubmitError] = useState<string | null>(null);
  const [currentLimitBytes, setCurrentLimitBytes] = useState(0);
  const [usedBytes, setUsedBytes] = useState<number | undefined>(undefined);

  const statusFilterOptions = useMemo(
    () => [
      { value: '', label: t('common.all') },
      ...STATUS_FILTERS.map((status) => ({
        value: status,
        label: t(`approvals.status.${status}`),
      })),
    ],
    [t],
  );

  const businessTypeFilterOptions = useMemo(() => {
    const types = [...new Set(items.map((item) => item.business_type).filter(Boolean))].sort();
    return [
      { value: '', label: t('common.all') },
      ...types.map((value) => ({ value, label: businessLabel(value, t) })),
    ];
  }, [items, t]);

  const load = useCallback(async () => {
    if (!enabled) {
      setItems([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const next = await listMyApprovals({
        search: debouncedSearch.trim() || undefined,
      });
      setItems(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('approvals.loadFailed'));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [debouncedSearch, enabled, t]);

  useEffect(() => {
    if (searchDebounceRef.current != null) {
      window.clearTimeout(searchDebounceRef.current);
    }
    searchDebounceRef.current = window.setTimeout(() => {
      setDebouncedSearch(search);
      searchDebounceRef.current = null;
    }, 300);
    return () => {
      if (searchDebounceRef.current != null) {
        window.clearTimeout(searchDebounceRef.current);
        searchDebounceRef.current = null;
      }
    };
  }, [search]);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = useMemo(() => {
    const next = { pending: 0, approved: 0, rejected: 0, cancelled: 0 };
    for (const item of items) {
      if (item.status in next) next[item.status as ApprovalStatus] += 1;
    }
    return next;
  }, [items]);

  const filteredItems = useMemo(() => {
    return items.filter((item) => {
      if (statusFilter && item.status !== statusFilter) return false;
      if (businessTypeFilter && item.business_type !== businessTypeFilter) return false;
      return true;
    });
  }, [businessTypeFilter, items, statusFilter]);

  const totalPages = useMemo(
    () => Math.max(1, Math.ceil(filteredItems.length / pageSize)),
    [filteredItems.length, pageSize],
  );

  useEffect(() => {
    setPage(1);
  }, [search, statusFilter, businessTypeFilter, pageSize]);

  useEffect(() => {
    if (!copyHint) return;
    const timer = window.setTimeout(() => setCopyHint(null), 2500);
    return () => window.clearTimeout(timer);
  }, [copyHint]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const pagedItems = useMemo(() => {
    const start = (page - 1) * pageSize;
    return filteredItems.slice(start, start + pageSize);
  }, [filteredItems, page, pageSize]);

  const onCancel = useCallback(
    async (orderNum: string) => {
      setBusyOrder(orderNum);
      setError(null);
      try {
        await cancelMyApproval(orderNum);
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : t('approvals.cancelFailed'));
      } finally {
        setBusyOrder(null);
      }
    },
    [load, t],
  );

  const copyApprovalLink = useCallback(
    async (orderNum: string) => {
      const link = buildApprovalDetailLink(orderNum);
      try {
        await copyHistoryText(link);
        setError(null);
        setCopyHint(t('approvals.copyLinkSuccess'));
      } catch {
        setError(t('approvals.copyLinkFailed'));
      }
    },
    [t],
  );

  const copyOrderNum = useCallback(
    async (orderNum: string) => {
      try {
        await copyHistoryText(orderNum);
        setError(null);
        setCopyHint(t('approvals.copyOrderNumSuccess'));
      } catch {
        setError(t('approvals.copyOrderNumFailed'));
      }
    },
    [t],
  );

  const openResubmit = useCallback(async (item: ApprovalOrderItem) => {
    const requested = Number(item.apply_data?.requested_limit_bytes);
    const snapshotCurrent = Number(item.apply_data?.current_limit_bytes);
    setResubmitItem(item);
    setResubmitLimitGiB(bytesToGiBInput(requested));
    setResubmitReason(item.reason || '');
    setResubmitApproverId('');
    setResubmitError(null);
    setCurrentLimitBytes(Number.isFinite(snapshotCurrent) ? snapshotCurrent : 0);
    setUsedBytes(
      Number.isFinite(Number(item.apply_data?.used_bytes))
        ? Number(item.apply_data?.used_bytes)
        : undefined,
    );
    setResubmitApproversLoading(true);
    try {
      const [usageSettled, candidates] = await Promise.all([
        fetchWorkspaceUsage().catch(() => null),
        listApproverCandidates(),
      ]);
      if (usageSettled) {
        if (usageSettled.limit_bytes > 0) setCurrentLimitBytes(usageSettled.limit_bytes);
        setUsedBytes(usageSettled.used_bytes);
      }
      setResubmitApprovers(candidates);
      const preferred = String(item.approver_id || '').trim();
      if (preferred && candidates.some((c) => c.user_id === preferred)) {
        setResubmitApproverId(preferred);
      } else if (candidates.length === 1) {
        setResubmitApproverId(candidates[0]?.user_id || '');
      }
    } catch {
      setResubmitApprovers([]);
      setResubmitError(t('agent.quota.expandApproverLoadFailed'));
    } finally {
      setResubmitApproversLoading(false);
    }
  }, [t]);

  const closeResubmit = useCallback(() => {
    if (resubmitBusy) return;
    setResubmitItem(null);
    setResubmitError(null);
  }, [resubmitBusy]);

  const submitResubmit = useCallback(async () => {
    if (!resubmitItem) return;
    const gib = Number(resubmitLimitGiB);
    if (!Number.isFinite(gib) || gib <= 0) {
      setResubmitError(t('agent.quota.expandInvalidLimit'));
      return;
    }
    const reason = resubmitReason.trim();
    if (!reason) {
      setResubmitError(t('agent.quota.expandReasonRequired'));
      return;
    }
    const approverId = resubmitApproverId.trim();
    if (!approverId) {
      setResubmitError(t('agent.quota.expandApproverRequired'));
      return;
    }
    const requested = Math.round(gib * GIB);
    if (currentLimitBytes > 0 && requested <= currentLimitBytes) {
      setResubmitError(t('agent.quota.expandMustIncrease'));
      return;
    }
    const jiuwenclawId = String(resubmitItem.cluster_id || '').trim();
    const groupId = String(resubmitItem.group_id || '').trim();
    const botId = String(resubmitItem.bot_id || '').trim();
    if (!jiuwenclawId || !botId) {
      setResubmitError(t('agent.quota.expandContextRequired'));
      return;
    }

    setResubmitBusy(true);
    setResubmitError(null);
    try {
      await createWorkspaceExpandRequest({
        jiuwenclawId,
        groupId,
        botId,
        requestedLimitBytes: requested,
        reason,
        usedBytes,
        approverId,
      });
      setResubmitItem(null);
      await load();
    } catch (err) {
      setResubmitError(err instanceof Error ? err.message : t('approvals.resubmitFailed'));
    } finally {
      setResubmitBusy(false);
    }
  }, [
    currentLimitBytes,
    load,
    resubmitApproverId,
    resubmitItem,
    resubmitLimitGiB,
    resubmitReason,
    t,
    usedBytes,
  ]);
  return (
    <div className="flex-1 min-h-0 relative overflow-y-auto w-full" data-testid="approvals-panel">
      {error ? (
        <div className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 z-20">
          <div className="bg-danger px-4 py-2 text-sm text-text-inverse rounded-lg shadow-lg animate-rise">{error}</div>
        </div>
      ) : null}
      {copyHint ? (
        <div className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 z-20">
          <div className="rounded-lg bg-ok px-4 py-2 text-sm text-text-inverse shadow-lg animate-rise">
            {copyHint}
          </div>
        </div>
      ) : null}

      <div className="mx-auto w-[90%] max-w-[1600px] py-8">
        <div className="mb-5 flex items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-text-strong">{t('approvals.myTitle')}</h1>
            <p className="mt-1 text-sm text-text-muted">{t('approvals.mySubtitle')}</p>
          </div>
          <button
            type="button"
            className="flex items-center gap-2 rounded-full bg-cron-action px-6 py-1.5 text-sm font-bold text-cron-action-foreground hover:bg-cron-action-hover disabled:opacity-60"
            onClick={() => {
              void load();
            }}
            disabled={!enabled || loading}
          >
            {t('common.refresh')}
          </button>
        </div>

        {!enabled ? (
          <div className="rounded-lg border border-border bg-secondary/30 px-4 py-8 text-center text-sm text-text-muted">
            {t('approvals.unavailable')}
          </div>
        ) : null}

        {enabled && items.length > 0 ? (
          <div className="mb-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className={`text-sm font-bold transition-colors ${
                statusFilter === ''
                  ? 'text-accent'
                  : 'text-text-strong hover:text-accent'
              }`}
              aria-pressed={statusFilter === ''}
              onClick={() => setStatusFilter('')}
            >
              {t('approvals.stats.total', { count: items.length })}
            </button>
            <StatPill
              icon={
                <span className="text-cron-running">
                  <RunningIcon size={15} />
                </span>
              }
              label={t('approvals.status.pending')}
              count={counts.pending}
              active={statusFilter === 'pending'}
              onClick={() =>
                setStatusFilter((current) => (current === 'pending' ? '' : 'pending'))
              }
            />
            <StatPill
              icon={
                <span className="text-ok">
                  <BoldRingIcon />
                </span>
              }
              label={t('approvals.status.approved')}
              count={counts.approved}
              active={statusFilter === 'approved'}
              onClick={() =>
                setStatusFilter((current) => (current === 'approved' ? '' : 'approved'))
              }
            />
            <StatPill
              icon={
                <span className="text-danger">
                  <BoldRingIcon />
                </span>
              }
              label={t('approvals.status.rejected')}
              count={counts.rejected}
              active={statusFilter === 'rejected'}
              onClick={() =>
                setStatusFilter((current) => (current === 'rejected' ? '' : 'rejected'))
              }
            />
            <StatPill
              icon={
                <span className="text-text-muted">
                  <BoldRingIcon />
                </span>
              }
              label={t('approvals.status.cancelled')}
              count={counts.cancelled}
              active={statusFilter === 'cancelled'}
              onClick={() =>
                setStatusFilter((current) => (current === 'cancelled' ? '' : 'cancelled'))
              }
            />
          </div>
        ) : null}

        {enabled && !(loading && items.length === 0) ? (
          <div className="mb-4">
            <div className="relative">
              <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t('approvals.search.placeholder') ?? undefined}
                className="w-full rounded-md border border-border bg-card py-1.5 pl-9 pr-3 text-sm text-text outline-none focus:border-accent"
              />
            </div>
          </div>
        ) : null}

        {enabled && loading && items.length === 0 ? (
          <div className="rounded-lg border border-border bg-secondary/30 px-3 py-4 flex items-center justify-center text-sm text-text-muted">
            {t('common.loading')}
          </div>
        ) : null}

        {enabled && !loading && items.length === 0 ? (
          <div className="flex min-h-[50vh] flex-col items-center justify-center gap-2 text-text-muted">
            <p className="text-sm">{t('approvals.empty')}</p>
          </div>
        ) : null}

        {enabled && items.length > 0 && filteredItems.length === 0 ? (
          <div className="flex min-h-[30vh] flex-col items-center justify-center gap-2 text-text-muted">
            <p className="text-sm">{t('approvals.search.noResults')}</p>
          </div>
        ) : null}

        {enabled && filteredItems.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full min-w-[1100px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border bg-bg-muted text-left text-text">
                  <Th first>
                    <TableColumnFilter
                      label={t('approvals.table.type')}
                      value={businessTypeFilter}
                      options={businessTypeFilterOptions}
                      onChange={setBusinessTypeFilter}
                    />
                  </Th>
                  <Th>{t('approvals.table.orderNum')}</Th>
                  <Th>{t('approvals.table.title')}</Th>
                  <Th>{t('approvals.table.requestedQuota')}</Th>
                  <Th>{t('approvals.table.approver')}</Th>
                  <Th>
                    <TableColumnFilter
                      label={t('approvals.table.status')}
                      value={statusFilter}
                      options={statusFilterOptions}
                      onChange={setStatusFilter}
                    />
                  </Th>
                  <Th>{t('approvals.table.createdAt')}</Th>
                  <Th>{t('approvals.table.reason')}</Th>
                  <Th>{t('approvals.table.actions')}</Th>
                </tr>
              </thead>
              <tbody>
                {pagedItems.map((item) => {
                  const typeLabel = businessLabel(item.business_type, t);
                  const title = item.title || typeLabel;
                  const quota = requestedQuotaText(item);
                  const reason = item.reason?.trim() || '—';
                  return (
                    <tr key={item.order_num} className="border-b border-border last:border-0 align-top">
                      <td className="px-4 py-3 text-text">
                        <span
                          className="inline-flex max-w-[160px] truncate rounded-full border border-border bg-card px-2 py-0.5 text-xs text-text-muted"
                          title={typeLabel}
                        >
                          {typeLabel}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-text">
                        <button
                          type="button"
                          className="block w-[12rem] max-w-[12rem] whitespace-nowrap text-left font-mono text-xs text-text-muted hover:text-text-strong"
                          title={t('approvals.copyOrderNum')}
                          onClick={() => {
                            void copyOrderNum(item.order_num);
                          }}
                        >
                          {item.order_num}
                        </button>
                      </td>
                      <td className="px-4 py-3 text-text">
                        <div className="max-w-[220px] truncate font-medium" title={title}>
                          {title}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-text whitespace-nowrap" title={quota}>
                        {quota}
                      </td>
                      <td className="px-4 py-3 text-text">
                        <CurrentApproversCell item={item} />
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={item.status} />
                      </td>
                      <td className="px-4 py-3 text-text whitespace-nowrap">
                        {item.created_at ? new Date(item.created_at).toLocaleString() : '—'}
                      </td>
                      <td className="px-4 py-3 text-text">
                        <div className="min-w-[12rem] max-w-[24rem] space-y-1">
                          <div className="line-clamp-3" title={reason}>
                            {reason}
                          </div>
                          {item.status === 'rejected' && item.latest_reject_comment ? (
                            <div className="line-clamp-2 text-xs text-danger" title={item.latest_reject_comment}>
                              {t('approvals.rejectComment')}: {item.latest_reject_comment}
                            </div>
                          ) : null}
                        </div>
                      </td>
                      <td className="relative px-4 py-3">
                        <div className="flex flex-wrap items-center gap-3">
                          {item.status === 'pending' ? (
                            <>
                              <button
                                type="button"
                                className="text-sm text-cron-action-link hover:opacity-80"
                                onClick={() => {
                                  void copyApprovalLink(item.order_num);
                                }}
                              >
                                {t('approvals.copyLink')}
                              </button>
                              <button
                                type="button"
                                className="text-sm text-cron-action-link hover:opacity-80 disabled:opacity-50"
                                disabled={busyOrder === item.order_num}
                                onClick={() => {
                                  void onCancel(item.order_num);
                                }}
                              >
                                {busyOrder === item.order_num ? t('common.loading') : t('approvals.cancel')}
                              </button>
                            </>
                          ) : null}
                          {canResubmit(item) ? (
                            <button
                              type="button"
                              className="text-sm text-cron-action-link hover:opacity-80"
                              onClick={() => {
                                void openResubmit(item);
                              }}
                            >
                              {t('approvals.resubmit')}
                            </button>
                          ) : null}
                          {item.status !== 'pending' && !canResubmit(item) ? (
                            <span className="text-sm text-text-muted/50">—</span>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}

        {enabled && filteredItems.length > 0 ? (
          <Pagination
            page={page}
            totalPages={totalPages}
            total={filteredItems.length}
            pageSize={pageSize}
            pageSizeOptions={PAGE_SIZE_OPTIONS}
            onPageChange={setPage}
            onPageSizeChange={setPageSize}
            className="mt-3"
          />
        ) : null}
      </div>

      {resubmitItem ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
          onClick={closeResubmit}
        >
          <div
            className="relative w-[440px] max-w-[92vw] rounded-lg bg-card p-6 shadow-xl animate-rise"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold text-text-strong mb-2">{t('approvals.resubmitTitle')}</h3>
            <p className="text-sm text-text-muted mb-3">{t('approvals.resubmitHint')}</p>
            {resubmitItem.latest_reject_comment ? (
              <div className="mb-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
                {t('approvals.rejectComment')}: {resubmitItem.latest_reject_comment}
              </div>
            ) : null}
            {currentLimitBytes > 0 ? (
              <p className="text-xs text-text-muted mb-3">
                {t('approvals.currentQuota')}: {formatBytes(currentLimitBytes)}
              </p>
            ) : null}
            <label className="block text-sm text-text mb-1">{t('agent.quota.expandLimitLabel')}</label>
            <input
              type="number"
              min="0.1"
              step="0.1"
              className="w-full mb-3 rounded-md border border-border bg-background px-3 py-2 text-sm"
              value={resubmitLimitGiB}
              onChange={(e) => setResubmitLimitGiB(e.target.value)}
              disabled={resubmitBusy}
            />
            <label className="block text-sm text-text mb-1">{t('agent.quota.expandReasonLabel')}</label>
            <textarea
              className="w-full mb-3 rounded-md border border-border bg-background px-3 py-2 text-sm min-h-[88px]"
              value={resubmitReason}
              onChange={(e) => setResubmitReason(e.target.value)}
              disabled={resubmitBusy}
              maxLength={1024}
            />
            <label className="block text-sm text-text mb-1">{t('agent.quota.expandApproverLabel')}</label>
            <ApproverPicker
              value={resubmitApproverId}
              options={resubmitApprovers}
              onChange={setResubmitApproverId}
              disabled={resubmitBusy}
              loading={resubmitApproversLoading}
            />
            {resubmitError ? <div className="mb-3 text-sm text-danger">{resubmitError}</div> : null}
            <div className="flex justify-end gap-3">
              <button
                type="button"
                className="rounded-full border border-border bg-card px-10 py-1.5 text-sm font-bold text-text hover:bg-bg-hover"
                onClick={closeResubmit}
                disabled={resubmitBusy}
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="rounded-full bg-cron-action px-10 py-1.5 text-sm font-bold text-cron-action-foreground hover:bg-cron-action-hover disabled:opacity-60"
                disabled={resubmitBusy}
                onClick={() => {
                  void submitResubmit();
                }}
              >
                {resubmitBusy ? t('common.loading') : t('approvals.resubmitSubmit')}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

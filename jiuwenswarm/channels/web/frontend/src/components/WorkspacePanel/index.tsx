import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation, Trans } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import {
  createWorkspaceExpandRequest,
  deleteWorkspaceEntries,
  downloadWorkspaceEntry,
  fetchWorkspaceTree,
  fetchWorkspaceUsage,
  formatBytes,
  previewWorkspaceFile,
} from '../../features/workspace/workspaceApi';
import {
  listApproverCandidates,
  type ApprovalApprover,
} from '../../features/approvals/approvalsApi';
import type {
  WorkspaceEntry,
  WorkspaceUsageData,
} from '../../features/workspace/workspaceTypes';
import { useEnterpriseContext } from '../../services/enterpriseContext';
import { getRuntimeScope } from '../../services/runtimeScope';
import { isEnterprise } from '../../edition';
import { WorkspaceUsageBar } from './WorkspaceUsageBar';
import { ConfirmDialog } from '../common/ConfirmDialog';
import { ApproverPicker } from '../common/ApproverPicker';

interface WorkspacePanelProps {
  sessionId: string;
}

const ROOT_KEY = '';

/** 可清理 zone 的根路径（相对租户根）；筛选时保留其祖先目录以便导航。 */
const DELETABLE_ZONE_ROOTS = [
  'agent/jiuwenclaw_workspace/projects',
  'agent/jiuwenclaw_workspace/todo',
  'agent/jiuwenclaw_workspace/prompt_attachment',
  'agent/jiuwenclaw_workspace/received_files',
] as const;

/** 打开「仅可删除」时预展开并拉取的路径（含中间祖先）。 */
const DELETABLE_FILTER_EXPAND_PATHS = [
  'agent',
  'agent/jiuwenclaw_workspace',
  ...DELETABLE_ZONE_ROOTS,
] as const;

type ChildrenCache = Record<string, WorkspaceEntry[]>;

function parentPath(relativePath: string): string {
  const idx = relativePath.lastIndexOf('/');
  return idx === -1 ? ROOT_KEY : relativePath.slice(0, idx);
}

function formatTime(ms: number | undefined): string {
  if (!ms || !Number.isFinite(ms)) return '—';
  try {
    return new Date(ms).toLocaleString();
  } catch {
    return '—';
  }
}

function zoneLabel(zone: string, t: (key: string) => string): string {
  const key = `agent.zones.${zone}`;
  const translated = t(key);
  return translated === key ? zone : translated;
}

/** 条目本身可删，或目录是通往可删 zone 的祖先 / 落在可删 zone 内。 */
function keepUnderDeletableFilter(entry: WorkspaceEntry): boolean {
  if (entry.deletable) return true;
  if (!entry.is_dir) return false;
  const path = entry.relative_path.replace(/\/+$/, '');
  if (!path) return false;
  return DELETABLE_ZONE_ROOTS.some(
    (zoneRoot) => zoneRoot === path || zoneRoot.startsWith(`${path}/`),
  );
}

export function WorkspacePanel({ sessionId: _sessionId }: WorkspacePanelProps) {
  const { t } = useTranslation();
  const enterprise = useEnterpriseContext();
  const [usage, setUsage] = useState<WorkspaceUsageData | null>(null);
  const [usageLoading, setUsageLoading] = useState(true);
  const [childrenCache, setChildrenCache] = useState<ChildrenCache>({});
  const childrenCacheRef = useRef<ChildrenCache>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set([ROOT_KEY]));
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(() => new Set());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<WorkspaceEntry | null>(null);
  const [deletableOnly, setDeletableOnly] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [previewContent, setPreviewContent] = useState<string | null>(null);
  const [previewTruncated, setPreviewTruncated] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewBinary, setPreviewBinary] = useState(false);
  const [expandSuccessOpen, setExpandSuccessOpen] = useState(false);
  const [expandOpen, setExpandOpen] = useState(false);
  const [expandLimitGiB, setExpandLimitGiB] = useState('');
  const [expandReason, setExpandReason] = useState('');
  const [expandApproverId, setExpandApproverId] = useState('');
  const [expandApprovers, setExpandApprovers] = useState<ApprovalApprover[]>([]);
  const [expandApproversLoading, setExpandApproversLoading] = useState(false);
  const [expandBusy, setExpandBusy] = useState(false);
  const [expandError, setExpandError] = useState<string | null>(null);

  useEffect(() => {
    childrenCacheRef.current = childrenCache;
  }, [childrenCache]);

  const loadUsage = useCallback(async () => {
    setUsageLoading(true);
    try {
      const next = await fetchWorkspaceUsage();
      setUsage(next);
    } catch (err) {
      console.error('[WorkspacePanel] usage failed', err);
      setUsage(null);
    } finally {
      setUsageLoading(false);
    }
  }, []);

  const loadDir = useCallback(async (relativePath: string, force = false) => {
    if (!force && childrenCacheRef.current[relativePath] !== undefined) {
      return;
    }
    setLoadingDirs((prev) => new Set(prev).add(relativePath));
    try {
      const data = await fetchWorkspaceTree(relativePath);
      setChildrenCache((prev) => ({ ...prev, [relativePath]: data.entries }));
      setLoadError(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : t('agent.errors.loadFailed');
      // 「仅可删除」会预拉 projects/todo/prompt_attachment/received_files 等 zone；
      // 尚未创建的目录后端返回 path not found，应按空目录处理，避免整页报错。
      if (/path not found/i.test(message)) {
        setChildrenCache((prev) => ({ ...prev, [relativePath]: [] }));
        return;
      }
      console.error('[WorkspacePanel] tree failed', err);
      setLoadError(message);
    } finally {
      setLoadingDirs((prev) => {
        const next = new Set(prev);
        next.delete(relativePath);
        return next;
      });
    }
  }, [t]);

  useEffect(() => {
    void loadUsage();
    void loadDir(ROOT_KEY, true);
  }, [loadDir, loadUsage]);

  const refreshAll = useCallback(async () => {
    setActionError(null);
    const openPaths = Array.from(expanded);
    await loadUsage();
    await Promise.all(openPaths.map((path) => loadDir(path, true)));
    if (selected) {
      const parent = selected.is_dir ? selected.relative_path : parentPath(selected.relative_path);
      try {
        const entries = (await fetchWorkspaceTree(parent)).entries;
        setChildrenCache((prev) => ({ ...prev, [parent]: entries }));
        setSelected(entries.find((e) => e.relative_path === selected.relative_path) || null);
      } catch {
        setSelected(null);
      }
    }
  }, [expanded, loadDir, loadUsage, selected]);
  const visibleEntries = useCallback(
    (path: string): WorkspaceEntry[] => {
      let entries = childrenCache[path] || [];
      if (deletableOnly) {
        entries = entries.filter((e) => keepUnderDeletableFilter(e));
      }
      return [...entries].sort((a, b) => {
        if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
        return a.name.localeCompare(b.name, 'zh-Hans-CN');
      });
    },
    [childrenCache, deletableOnly],
  );

  useEffect(() => {
    if (!deletableOnly) return;
    setExpanded((prev) => {
      const next = new Set(prev);
      for (const path of DELETABLE_FILTER_EXPAND_PATHS) {
        next.add(path);
      }
      return next;
    });
    for (const path of DELETABLE_FILTER_EXPAND_PATHS) {
      void loadDir(path);
    }
  }, [deletableOnly, loadDir]);

  const toggleDir = (entry: WorkspaceEntry) => {
    const key = entry.relative_path;
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
        void loadDir(key);
      }
      return next;
    });
  };

  const selectEntry = (entry: WorkspaceEntry) => {
    setSelected(entry);
    setActionError(null);
    if (entry.is_dir) {
      setExpanded((prev) => {
        const next = new Set(prev);
        next.add(entry.relative_path);
        return next;
      });
      void loadDir(entry.relative_path);
    }
  };

  useEffect(() => {
    if (!selected || selected.is_dir) {
      setPreviewContent(null);
      setPreviewTruncated(false);
      setPreviewBinary(false);
      setPreviewLoading(false);
      return;
    }
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewContent(null);
    setPreviewBinary(false);
    void previewWorkspaceFile(selected.relative_path)
      .then((data) => {
        if (cancelled) return;
        setPreviewTruncated(data.truncated);
        if (data.content == null) {
          setPreviewBinary(true);
          setPreviewContent(null);
        } else {
          setPreviewBinary(false);
          setPreviewContent(data.content);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setPreviewBinary(false);
        setPreviewContent(null);
        setActionError(err instanceof Error ? err.message : t('fileViewer.unknownError'));
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, t]);

  const requestDelete = () => {
    if (!selected?.deletable || actionBusy) return;
    setDeleteConfirmOpen(true);
  };

  const confirmDelete = async () => {
    if (!selected?.deletable) {
      setDeleteConfirmOpen(false);
      return;
    }
    setActionBusy(true);
    setActionError(null);
    try {
      const result = await deleteWorkspaceEntries([selected.relative_path]);
      const item = result.results[0];
      if (item && !item.ok) {
        setActionError(item.error || t('agent.errors.deleteFailed'));
        return;
      }
      const parent = parentPath(selected.relative_path);
      setSelected(null);
      setDeleteConfirmOpen(false);
      await loadDir(parent, true);
      await loadUsage();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : t('agent.errors.deleteFailed'));
    } finally {
      setActionBusy(false);
    }
  };

  const handleDownload = async () => {
    if (!selected) return;
    setActionBusy(true);
    setActionError(null);
    try {
      await downloadWorkspaceEntry(selected.relative_path);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : t('agent.errors.downloadFailed'));
    } finally {
      setActionBusy(false);
    }
  };

  const isMarkdown =
    selected &&
    !selected.is_dir &&
    (selected.name.toLowerCase().endsWith('.md') ||
      selected.name.toLowerCase().endsWith('.mdx'));

  const rootEntries = useMemo(() => visibleEntries(ROOT_KEY), [visibleEntries]);

  const renderTree = (path: string, depth: number): JSX.Element[] => {
    const entries = visibleEntries(path);
    const loading = loadingDirs.has(path);
    if (loading && entries.length === 0) {
      return [
        <div
          key={`loading-${path}`}
          className="px-2 py-2 text-xs text-text-muted"
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
        >
          {t('common.loading')}
        </div>,
      ];
    }
    return entries.map((entry) => {
      const isExpanded = expanded.has(entry.relative_path);
      const selectedRow = selected?.relative_path === entry.relative_path;
      return (
        <div key={entry.relative_path}>
          <div
            className={`w-full min-h-10 flex items-center gap-1 rounded-lg px-1 py-1 text-left text-[14px] border ${
              selectedRow
                ? 'bg-accent-subtle text-text border-[var(--color-border-accent)]'
                : 'text-text-muted hover:bg-secondary/40 hover:text-text border-transparent'
            }`}
            style={{ paddingLeft: `${depth * 14 + 4}px` }}
          >
            {entry.is_dir ? (
              <button
                type="button"
                className="w-5 h-5 flex items-center justify-center flex-shrink-0"
                onClick={() => toggleDir(entry)}
                aria-label={isExpanded ? 'collapse' : 'expand'}
              >
                <svg
                  className={`w-3 h-3 ${isExpanded ? 'rotate-90' : ''}`}
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 6l6 6-6 6" />
                </svg>
              </button>
            ) : (
              <span className="w-5 h-5 flex-shrink-0" />
            )}
            <button
              type="button"
              className="flex-1 min-w-0 flex items-center gap-2 py-1.5 pr-2 text-left"
              onClick={() => selectEntry(entry)}
              title={entry.relative_path}
            >
              <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                {entry.is_dir ? (
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h4.5l1.5 2.25h10.5v8.25A2.25 2.25 0 0118 19.5H6A2.25 2.25 0 013.75 17.25V6.75z" />
                ) : (
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM14.25 3.75v4.5h4.5" />
                )}
              </svg>
              <span className="flex-1 min-w-0 truncate">{entry.name}</span>
              {entry.deletable ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded border border-border bg-secondary/40">
                  {t('agent.deletable')}
                </span>
              ) : null}
            </button>
          </div>
          {entry.is_dir && isExpanded ? renderTree(entry.relative_path, depth + 1) : null}
        </div>
      );
    });
  };

  const openExpandDialog = useCallback(() => {
    const currentGiB =
      usage && usage.limit_bytes > 0 ? (usage.limit_bytes / (1024 ** 3)).toFixed(1) : '';
    setExpandLimitGiB(currentGiB);
    setExpandReason('');
    setExpandApproverId('');
    setExpandError(null);
    setExpandOpen(true);
    setExpandApproversLoading(true);
    void listApproverCandidates()
      .then((items) => {
        setExpandApprovers(items);
        if (items.length === 1) {
          setExpandApproverId(items[0]?.user_id || '');
        }
      })
      .catch(() => {
        setExpandApprovers([]);
        setExpandError(t('agent.quota.expandApproverLoadFailed'));
      })
      .finally(() => setExpandApproversLoading(false));
  }, [t, usage]);

  const submitExpand = useCallback(async () => {
    const gib = Number(expandLimitGiB);
    if (!Number.isFinite(gib) || gib <= 0) {
      setExpandError(t('agent.quota.expandInvalidLimit'));
      return;
    }
    const reason = expandReason.trim();
    if (!reason) {
      setExpandError(t('agent.quota.expandReasonRequired'));
      return;
    }
    const approverId = expandApproverId.trim();
    if (!approverId) {
      setExpandError(t('agent.quota.expandApproverRequired'));
      return;
    }
    const requested = Math.round(gib * 1024 ** 3);
    if (usage && usage.limit_bytes > 0 && requested <= usage.limit_bytes) {
      setExpandError(t('agent.quota.expandMustIncrease'));
      return;
    }
    const scope = getRuntimeScope();
    const jiuwenclawId = enterprise?.selected.jiuwenclaw_id?.trim() || '';
    const groupId = enterprise?.selected.group_id ?? scope.groupId ?? '';
    const botId = enterprise?.selected.bot_id ?? scope.botId ?? '';
    if (!jiuwenclawId || !botId) {
      setExpandError(t('agent.quota.expandContextRequired'));
      return;
    }
    setExpandBusy(true);
    setExpandError(null);
    try {
      await createWorkspaceExpandRequest({
        jiuwenclawId,
        groupId,
        botId,
        requestedLimitBytes: requested,
        reason,
        usedBytes: usage?.used_bytes,
        approverId,
      });
      setExpandOpen(false);
      setExpandSuccessOpen(true);
    } catch (err) {
      setExpandError(err instanceof Error ? err.message : t('agent.quota.expandFailed'));
    } finally {
      setExpandBusy(false);
    }
  }, [enterprise, expandApproverId, expandLimitGiB, expandReason, t, usage]);

  const goToApprovals = useCallback(() => {
    setExpandSuccessOpen(false);
    window.dispatchEvent(new CustomEvent<string>('jiuwen:nav', { detail: 'approvals' }));
  }, []);

  return (
    <>
      {expandOpen ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
          onClick={() => {
            if (!expandBusy) setExpandOpen(false);
          }}
        >
          <div
            className="relative w-[420px] rounded-lg bg-card p-6 shadow-xl animate-rise"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold text-text-strong mb-4">
              {t('agent.quota.expandTitle')}
            </h3>
            <p className="text-sm text-text-muted mb-4">{t('agent.quota.expandDialogHint')}</p>
            <label className="block text-sm text-text mb-1">{t('agent.quota.expandLimitLabel')}</label>
            <input
              type="number"
              min="0.1"
              step="0.1"
              className="w-full mb-3 rounded-md border border-border bg-background px-3 py-2 text-sm"
              value={expandLimitGiB}
              onChange={(e) => setExpandLimitGiB(e.target.value)}
              disabled={expandBusy}
            />
            <label className="block text-sm text-text mb-1">{t('agent.quota.expandReasonLabel')}</label>
            <textarea
              className="w-full mb-3 rounded-md border border-border bg-background px-3 py-2 text-sm min-h-[88px]"
              value={expandReason}
              onChange={(e) => setExpandReason(e.target.value)}
              disabled={expandBusy}
              maxLength={1024}
            />
            <label className="block text-sm text-text mb-1">{t('agent.quota.expandApproverLabel')}</label>
            <ApproverPicker
              value={expandApproverId}
              options={expandApprovers}
              onChange={setExpandApproverId}
              disabled={expandBusy}
              loading={expandApproversLoading}
            />
            {expandError ? (
              <div className="mb-3 text-sm text-danger">{expandError}</div>
            ) : null}
            <div className="flex justify-end gap-3">
              <button
                type="button"
                className="px-6 py-1.5 rounded-full border border-border text-sm font-medium text-text hover:bg-secondary/50"
                disabled={expandBusy}
                onClick={() => setExpandOpen(false)}
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="rounded-full bg-accent px-6 py-1.5 text-sm font-medium text-accent-foreground hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
                disabled={expandBusy}
                onClick={() => {
                  void submitExpand();
                }}
              >
                {expandBusy ? t('common.loading') : t('agent.quota.expandSubmit')}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {expandSuccessOpen ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
          onClick={() => setExpandSuccessOpen(false)}
        >
          <div
            className="relative w-[420px] rounded-lg bg-card p-6 shadow-xl animate-rise"
            role="dialog"
            aria-modal="true"
            aria-labelledby="expand-success-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="expand-success-title" className="text-base font-semibold text-text-strong mb-3">
              {t('agent.quota.expandSubmittedTitle')}
            </h3>
            <p className="mb-6 text-sm text-text leading-relaxed">
              <Trans
                i18nKey="agent.quota.expandSubmittedBody"
                components={{
                  approvals: (
                    <button
                      type="button"
                      className="font-medium text-accent underline underline-offset-2 hover:text-accent-hover"
                      onClick={goToApprovals}
                    />
                  ),
                }}
              />
            </p>
            <div className="flex justify-end gap-3">
              <button
                type="button"
                className="px-6 py-1.5 rounded-full border border-border text-sm font-medium text-text hover:bg-secondary/50"
                onClick={() => setExpandSuccessOpen(false)}
              >
                {t('common.ok')}
              </button>
              <button
                type="button"
                className="rounded-full bg-accent px-6 py-1.5 text-sm font-medium text-accent-foreground hover:bg-accent-hover"
                onClick={goToApprovals}
              >
                {t('agent.quota.expandViewApprovals')}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {deleteConfirmOpen && selected ? (
        <ConfirmDialog
          title={t('agent.deleteConfirmTitle')}
          message={t('agent.confirmDelete', { name: selected.name })}
          confirmLabel={t('agent.delete')}
          onConfirm={() => {
            void confirmDelete();
          }}
          onCancel={() => {
            if (!actionBusy) setDeleteConfirmOpen(false);
          }}
          loading={actionBusy}
        />
      ) : null}
      <div className="flex-1 min-h-0">
      <div className="card w-full h-full flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h2 className="text-lg font-semibold">{t('agent.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t('agent.subtitle')}</p>
          </div>
        </div>

        <WorkspaceUsageBar
          usage={usage}
          loading={usageLoading}
          onRefresh={() => {
            void refreshAll();
          }}
          onRequestExpand={isEnterprise() ? openExpandDialog : undefined}
        />

        {loadError ? (
          <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {loadError}
          </div>
        ) : null}

        <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,3fr)_minmax(0,7fr)] gap-4">
          <div className="rounded-xl border border-border bg-card/70 overflow-hidden flex flex-col min-h-0">
            <div className="px-4 py-3 bg-secondary/30 border-b border-border space-y-2">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-sm font-medium text-text">{t('agent.workspace')}</h3>
                <button
                  type="button"
                  className="btn !px-3 !py-1.5"
                  onClick={() => {
                    void refreshAll();
                  }}
                  disabled={usageLoading}
                >
                  {t('common.refresh')}
                </button>
              </div>
              <div className="flex items-center gap-3 text-xs text-text-muted flex-wrap">
                <label className="inline-flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={deletableOnly}
                    onChange={(e) => setDeletableOnly(e.target.checked)}
                  />
                  {t('agent.filterDeletable')}
                </label>
              </div>
            </div>
            <div className="flex-1 overflow-auto p-2">
              {rootEntries.length === 0 && !loadingDirs.has(ROOT_KEY) ? (
                <div className="h-full flex items-center justify-center text-sm text-text-muted">
                  {deletableOnly ? t('agent.emptyDeletable') : t('agent.empty')}
                </div>
              ) : (
                <div className="space-y-0.5">{renderTree(ROOT_KEY, 0)}</div>
              )}
            </div>
          </div>

          <div className="rounded-xl border border-border bg-card/70 overflow-hidden flex flex-col min-h-0">
            {selected ? (
              <>
                <div className="px-4 py-3 bg-secondary/30 border-b border-border space-y-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h4 className="text-sm font-medium text-text truncate">{selected.name}</h4>
                      <p className="text-xs text-text-muted mono truncate mt-1" title={selected.relative_path}>
                        {selected.relative_path}
                      </p>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <button
                        type="button"
                        className="btn !px-3 !py-1.5 disabled:opacity-50"
                        disabled={actionBusy}
                        onClick={() => {
                          void handleDownload();
                        }}
                      >
                        {t('agent.download')}
                      </button>
                      <button
                        type="button"
                        className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                        disabled={actionBusy || !selected.deletable}
                        title={
                          selected.deletable
                            ? t('agent.delete')
                            : t('agent.notDeletableReason')
                        }
                        onClick={requestDelete}
                      >
                        {t('agent.delete')}
                      </button>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-xs text-text-muted">
                    <div>{t('agent.meta.zone')}: <span className="text-text">{zoneLabel(String(selected.zone), t)}</span></div>
                    <div>{t('agent.meta.size')}: <span className="text-text mono">{formatBytes(selected.size_bytes)}</span></div>
                    <div>{t('agent.meta.mtime')}: <span className="text-text">{formatTime(selected.mtime_ms)}</span></div>
                    <div>
                      {t('agent.meta.deletable')}:{' '}
                      <span className="text-text">
                        {selected.deletable ? t('common.yes') : t('common.no')}
                      </span>
                    </div>
                  </div>
                  {actionError ? (
                    <div className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
                      {actionError}
                    </div>
                  ) : null}
                </div>
                <div className="flex-1 min-h-0 overflow-auto p-4">
                  {selected.is_dir ? (
                    <div className="h-full flex items-center justify-center text-sm text-text-muted">
                      {t('agent.dirSelectedHint')}
                    </div>
                  ) : previewLoading ? (
                    <div className="h-full flex items-center justify-center">
                      <div className="w-7 h-7 rounded-full border-4 border-border border-t-accent animate-spin" />
                    </div>
                  ) : previewBinary ? (
                    <div className="h-full flex items-center justify-center text-sm text-text-muted">
                      {t('agent.binaryPreview')}
                    </div>
                  ) : previewContent != null ? (
                    <div className="space-y-2">
                      {previewTruncated ? (
                        <div className="text-xs text-warn">{t('agent.previewTruncated')}</div>
                      ) : null}
                      {isMarkdown ? (
                        <article className="chat-text max-w-none">
                          <ReactMarkdown>{previewContent || ' '}</ReactMarkdown>
                        </article>
                      ) : (
                        <pre className="text-sm text-text mono whitespace-pre-wrap break-words rounded-lg border border-border bg-card p-3">
                          {previewContent}
                        </pre>
                      )}
                    </div>
                  ) : (
                    <div className="h-full flex items-center justify-center text-sm text-text-muted">
                      {t('agent.previewEmpty')}
                    </div>
                  )}
                </div>
              </>
            ) : (
              <>
                <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                  <h4 className="text-sm font-medium text-text">{t('agent.contentPreview')}</h4>
                  <p className="text-xs text-text-muted mt-1">{t('agent.selectEntry')}</p>
                </div>
                <div className="flex-1 min-h-0 flex items-center justify-center text-sm text-text-muted">
                  {t('agent.selectEntryHint')}
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
    </>
  );
}

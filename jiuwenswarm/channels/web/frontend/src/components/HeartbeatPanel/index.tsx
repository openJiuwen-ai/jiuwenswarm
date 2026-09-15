// src/components/HeartbeatPanel/index.tsx
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Activity,
  ArrowLeft,
  ChevronDown,
  CircleStop,
  List,
  MessageSquarePlus,
  Pause,
  Pencil,
  PencilLine,
  Play,
  Plus,
  RotateCcw,
  Trash2,
  X,
} from 'lucide-react';
import { webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores';
import type { WebError } from '../../types';
import type {
  HeartbeatCancelResult,
  HeartbeatJobDTO,
  HeartbeatJobStatus,
  HeartbeatMeta,
  HeartbeatRunNowResult,
  HeartbeatTaskUI,
} from '../../types/heartbeat';
import { summarizeHeartbeatSchedule } from './heartbeatScheduleConvert';
import {
  heartbeatRunNowMessageKey,
  heartbeatCancelMessageKey,
  heartbeatLastRunStatusLabelKey,
  canHeartbeatRunNow,
  canHeartbeatToggleEnable,
} from './heartbeatStatusText';
import HeartbeatStatusBadge from './HeartbeatStatusBadge';
import HeartbeatPagination, { HEARTBEAT_PAGE_SIZE_DEFAULT } from './HeartbeatPagination';
import HeartbeatTaskDrawer, {
  emptyHeartbeatTaskForm,
  jobToHeartbeatTaskForm,
  type HeartbeatTaskFormValue,
} from './HeartbeatTaskDrawer';
import { scheduleFormToDto } from './heartbeatScheduleConvert';
import ConfirmDialog from '../CronPanel/ConfirmDialog';
import SimpleSelect from '../CronPanel/SimpleSelect';
import { useClickOutside } from '../CronPanel/useClickOutside';

interface HeartbeatPanelProps {
  sessionId: string;
  onClose: () => void;
}

function heartbeatJobToUI(job: HeartbeatJobDTO): HeartbeatTaskUI {
  return {
    id: job.id,
    name: job.name,
    prompt: job.prompt,
    enabled: job.enabled,
    status: job.status,
    schedule: job.schedule,
    timezone: job.timezone,
    concurrencyPolicy: job.concurrency_policy,
    sessionDeletedPolicy: job.session_deleted_policy,
    maxRuns: job.max_runs,
    createdAt: job.created_at,
    updatedAt: job.updated_at,
    nextRunAt: job.next_run_at,
    lastRunAt: job.last_run_at,
    runCount: job.run_count,
    runState: job.run_state,
  };
}

/** Unix 秒 -> 本地可读时间；接口时间戳单位统一是秒，展示前需要 *1000，见接口规格说明 §3 */
function formatHeartbeatTimestamp(seconds: number | null): string | null {
  if (!seconds) return null;
  const d = new Date(seconds * 1000);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString();
}

export default function HeartbeatPanel({ sessionId, onClose }: HeartbeatPanelProps) {
  const { t } = useTranslation();
  const [meta, setMeta] = useState<HeartbeatMeta | null>(null);
  const [jobs, setJobs] = useState<HeartbeatTaskUI[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // 会话切换/组件卸载时中止未完成请求，避免旧会话的响应覆盖新会话状态，见接口规格说明 §16.3
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;

  const loadAll = useCallback(async (signal: AbortSignal, options?: { silent?: boolean }) => {
    const silent = options?.silent ?? false;
    if (!silent) setLoading(true);
    setLoadError(null);
    try {
      const [metaPayload, listPayload] = await Promise.all([
        webRequest<HeartbeatMeta>('heartbeat.job.meta', { session_id: sessionId }, { signal }),
        webRequest<{ jobs: HeartbeatJobDTO[] }>('heartbeat.job.list', { session_id: sessionId }, { signal }),
      ]);
      if (sessionIdRef.current !== sessionId) return; // 会话已切换，丢弃过期响应
      setMeta(metaPayload);
      setJobs((listPayload.jobs ?? []).map(heartbeatJobToUI));
    } catch (err) {
      if (typeof err === 'object' && err !== null && 'code' in err && (err as WebError).code === 'REQUEST_ABORTED') return;
      if (sessionIdRef.current !== sessionId) return;
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      if (sessionIdRef.current === sessionId && !silent) setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadAll(controller.signal);
    return () => controller.abort();
  }, [loadAll]);

  // 自动触发结束时（useWebSocket 在 Heartbeat 轮 processing_status(false) 派发此事件），
  // 静默刷新 list/get 以同步本轮 run_state（last_run_status/skipped_count 等），
  // 见接口规格说明2 §8 步骤5。只在当前会话面板可见时刷新，避免无关会话的刷新请求。
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<{ sessionId?: string }>).detail;
      if (!detail || detail.sessionId !== sessionId) return;
      const controller = new AbortController();
      void loadAll(controller.signal, { silent: true });
    };
    window.addEventListener('heartbeat-list-refresh', handler);
    return () => window.removeEventListener('heartbeat-list-refresh', handler);
  }, [sessionId, loadAll]);

  // 后端 list 按 created_at ASC 返回，前端本地按"最近更新/创建优先"重新排序 + 按状态/关键词筛选，
  // 见接口规格说明 §16.8；只复制展示用副本，不改 jobs 本身
  const [statusFilter, setStatusFilter] = useState<HeartbeatJobStatus | 'all' | 'active'>('active');
  const [searchQuery, setSearchQuery] = useState('');
  const [pageSize, setPageSize] = useState(HEARTBEAT_PAGE_SIZE_DEFAULT);
  const [currentPage, setCurrentPage] = useState(1);

  // "活跃" = scheduled + running 合并计数；其余终态各自计数，用于筛选下拉里展示每种状态的数量
  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = { all: jobs.length, active: 0, completed: 0, expired: 0, disabled: 0 };
    for (const job of jobs) {
      if (job.status === 'scheduled' || job.status === 'running') counts.active += 1;
      else counts[job.status] = (counts[job.status] ?? 0) + 1;
    }
    return counts;
  }, [jobs]);

  const filteredJobs = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return [...jobs]
      .filter((job) => {
        if (statusFilter === 'all') return true;
        if (statusFilter === 'active') return job.status === 'scheduled' || job.status === 'running';
        return job.status === statusFilter;
      })
      .filter((job) => {
        if (!q) return true;
        return job.name.toLowerCase().includes(q) || job.prompt.toLowerCase().includes(q);
      })
      .sort((a, b) => (b.updatedAt ?? b.createdAt ?? 0) - (a.updatedAt ?? a.createdAt ?? 0));
  }, [jobs, statusFilter, searchQuery]);

  const totalPages = Math.max(1, Math.ceil(filteredJobs.length / pageSize));
  // 筛选/搜索/每页条数变化时回到第 1 页；任务被删/筛结果变少导致页数缩水时把当前页钳回范围内
  useEffect(() => {
    setCurrentPage(1);
  }, [statusFilter, searchQuery, pageSize]);
  useEffect(() => {
    setCurrentPage((p) => Math.min(p, totalPages));
  }, [totalPages]);

  const displayedJobs = useMemo(
    () => filteredJobs.slice((currentPage - 1) * pageSize, currentPage * pageSize),
    [filteredJobs, currentPage, pageSize],
  );

  const [drawer, setDrawer] = useState<
    | { mode: 'create'; form: HeartbeatTaskFormValue; submitting: boolean; error: string | null }
    | { mode: 'edit'; jobId: string; form: HeartbeatTaskFormValue; submitting: boolean; error: string | null }
    | null
  >(null);

  const openCreateDrawer = useCallback(() => {
    if (!meta) return;
    setDrawer({ mode: 'create', form: emptyHeartbeatTaskForm(meta), submitting: false, error: null });
  }, [meta]);

  // "通过聊天创建"：心跳任务只能绑定当前会话（见方案设计 §1），因此不像 Cron 那样导航去新会话，
  // 而是直接把预制提示词写回当前会话的聊天输入框，让用户在原会话里用 Agent Tool 创建。
  // 只调 setInputValue 只更新 store，不会让已经挂载的 contenteditable 输入框跟着刷新——
  // InputArea 只在"切会话"时才会用 store 的 inputValue 回填 DOM；同一会话内要让输入框
  // 立即显示新内容，必须再派发 chat-input-sync 事件，跟"编辑排队任务"用的是同一套机制
  // （见 ChatPanel/index.tsx 的 handleEditTask）。
  const createViaChat = useCallback(() => {
    const prompt = t('heartbeat.panel.createMenu.viaChatPrompt');
    useChatStore.getState().setInputValue(sessionId, prompt);
    window.dispatchEvent(new CustomEvent('chat-input-sync', { detail: { sessionId, value: prompt } }));
    onClose();
  }, [sessionId, onClose, t]);

  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  const createMenuRef = useRef<HTMLDivElement>(null);
  useClickOutside(createMenuRef, createMenuOpen, () => setCreateMenuOpen(false));

  const openEditDrawer = useCallback((job: HeartbeatTaskUI) => {
    setDrawer({ mode: 'edit', jobId: job.id, form: jobToHeartbeatTaskForm(job), submitting: false, error: null });
  }, []);

  const [toast, setToast] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<HeartbeatTaskUI | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [actingJobId, setActingJobId] = useState<string | null>(null);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 4000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // 有任务处于 running 时，每 3 秒静默刷新一次列表；全部离开 running 后自动停止，
  // 页面隐藏/组件卸载时也停止，见接口规格说明 §7 建议刷新策略
  useEffect(() => {
    const hasRunning = jobs.some((job) => job.status === 'running');
    if (!hasRunning) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void loadAll(controller.signal, { silent: true });
    }, 3000);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [jobs, loadAll]);

  const handleToggle = useCallback(
    async (job: HeartbeatTaskUI) => {
      setActingJobId(job.id);
      try {
        await webRequest<{ job: HeartbeatJobDTO }>('heartbeat.job.toggle', {
          session_id: sessionId,
          id: job.id,
          enabled: !job.enabled,
        });
        setToast(t(job.enabled ? 'heartbeat.toast.paused' : 'heartbeat.toast.resumed'));
        const controller = new AbortController();
        await loadAll(controller.signal, { silent: true });
      } catch (err) {
        setToast(err instanceof Error ? err.message : String(err));
      } finally {
        setActingJobId(null);
      }
    },
    [sessionId, loadAll, t],
  );

  const handleRunNow = useCallback(
    async (job: HeartbeatTaskUI) => {
      setActingJobId(job.id);
      try {
        const result = await webRequest<HeartbeatRunNowResult>('heartbeat.job.run_now', {
          session_id: sessionId,
          id: job.id,
          reschedule: false,
        });
        setToast(t(heartbeatRunNowMessageKey(result.accepted, result.reason, result.queued)));
        const controller = new AbortController();
        await loadAll(controller.signal, { silent: true });
      } catch (err) {
        setToast(err instanceof Error ? err.message : String(err));
      } finally {
        setActingJobId(null);
      }
    },
    [sessionId, loadAll, t],
  );

  const handleCancel = useCallback(
    async (job: HeartbeatTaskUI, pauseSchedule: boolean) => {
      setActingJobId(job.id);
      try {
        const result = await webRequest<HeartbeatCancelResult>('heartbeat.job.cancel', {
          session_id: sessionId,
          id: job.id,
          pause_schedule: pauseSchedule,
        });
        setToast(t(heartbeatCancelMessageKey(result.cancel_status)));
        const controller = new AbortController();
        await loadAll(controller.signal, { silent: true });
      } catch (err) {
        setToast(err instanceof Error ? err.message : String(err));
      } finally {
        setActingJobId(null);
      }
    },
    [sessionId, loadAll, t],
  );

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    setActingJobId(pendingDelete.id);
    setDeleting(true);
    setDeleteError(null);
    try {
      const result = await webRequest<{ deleted: boolean }>('heartbeat.job.delete', {
        session_id: sessionId,
        id: pendingDelete.id,
      });
      if (!result.deleted) {
        setDeleteError(t('heartbeat.toast.deleteConflict') ?? undefined);
        return;
      }
      setPendingDelete(null);
      const controller = new AbortController();
      await loadAll(controller.signal, { silent: true });
    } catch (err) {
      const webErr = err as WebError;
      if (webErr.code === 'CONFLICT') {
        setDeleteError(t('heartbeat.toast.deleteConflict'));
      } else {
        setDeleteError(webErr.message ?? String(err));
      }
    } finally {
      setDeleting(false);
      setActingJobId(null);
    }
  }, [pendingDelete, sessionId, loadAll, t]);

  const submitDrawer = useCallback(
    async (value: HeartbeatTaskFormValue) => {
      if (!drawer) return;
      setDrawer({ ...drawer, form: value, submitting: true, error: null });
      const payload = {
        name: value.name.trim(),
        prompt: value.prompt.trim(),
        schedule: scheduleFormToDto(value.schedule),
        timezone: value.schedule.timezone,
        concurrency_policy: value.concurrencyPolicy,
        max_runs: value.maxRuns,
        ...(drawer.mode === 'edit' ? { session_deleted_policy: value.sessionDeletedPolicy } : {}),
      };
      try {
        if (drawer.mode === 'create') {
          await webRequest<{ job: HeartbeatJobDTO }>('heartbeat.job.create', {
            session_id: sessionId,
            ...payload,
            enabled: value.enabled,
          });
        } else {
          // enabled 由 heartbeat.job.toggle 独占管理，编辑表单不应携带并覆盖它，
          // 否则会把用户在抽屉打开期间通过 Pause/Resume 按钮做的改动静默覆盖回去
          await webRequest<{ job: HeartbeatJobDTO }>('heartbeat.job.update', {
            session_id: sessionId,
            id: drawer.jobId,
            patch: payload,
          });
        }
        setDrawer(null);
        const controller = new AbortController();
        await loadAll(controller.signal, { silent: true });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setDrawer((prev) => (prev ? { ...prev, submitting: false, error: message } : prev));
      }
    },
    [drawer, sessionId, loadAll],
  );

  const drawerBusy = Boolean(drawer?.submitting);

  // "活跃" 已覆盖 scheduled + running，下拉里不再单列这两项
  const statusFilterOptions = [
    {
      value: 'all',
      label: (
        <span className="inline-flex items-center gap-1.5 text-sm text-text-muted">
          <List size={13} />
          {t('heartbeat.panel.filterAll')}
          <span className="text-text-muted">({statusCounts.all})</span>
        </span>
      ),
    },
    {
      value: 'active',
      label: (
        <span className="inline-flex items-center gap-1.5 text-sm text-cron-running">
          <Activity size={13} />
          {t('heartbeat.panel.filterActive')}
          <span className="text-text-muted">({statusCounts.active})</span>
        </span>
      ),
    },
    ...(meta?.statuses ?? [])
      .filter((status) => status !== 'scheduled' && status !== 'running')
      .map((status) => ({
        value: status,
        label: (
          <span className="inline-flex items-center gap-1.5">
            <HeartbeatStatusBadge status={status} />
            <span className="text-text-muted">({statusCounts[status] ?? 0})</span>
          </span>
        ),
      })),
  ];

  return (
    <div className="flex h-full w-[420px] max-w-full flex-shrink-0 flex-col border-l border-border bg-card" data-testid="heartbeat-panel-root">
      {drawer && meta ? (
        <div className="flex items-center gap-2 border-b border-border p-4" data-testid="heartbeat-panel-drawer-header">
          <button
            type="button"
            onClick={() => setDrawer(null)}
            title={t('heartbeat.drawer.back')}
            className="text-text-muted hover:text-text"
            data-testid="heartbeat-panel-drawer-back-btn"
          >
            <ArrowLeft size={18} />
          </button>
          <h2 className="text-lg font-bold text-text-strong" data-testid="heartbeat-panel-drawer-title" data-variant={drawer.mode}>
            {t(drawer.mode === 'create' ? 'heartbeat.drawer.titleCreate' : 'heartbeat.drawer.titleEdit')}
          </h2>
          <button onClick={onClose} className="ml-auto text-text-muted hover:text-text" data-testid="heartbeat-panel-close-btn">
            <X size={20} />
          </button>
        </div>
      ) : (
        <div className="flex items-center justify-between border-b border-border p-4" data-testid="heartbeat-panel-header">
          <h2 className="text-lg font-bold text-text-strong" data-testid="heartbeat-panel-title">{t('heartbeat.panel.title')}</h2>
          <div className="flex items-center gap-3">
            <div className="relative" ref={createMenuRef} data-testid="heartbeat-panel-create-menu-wrap">
              <button
                type="button"
                disabled={!meta || drawerBusy}
                onClick={() => setCreateMenuOpen((v) => !v)}
                className="flex items-center gap-1.5 rounded-full bg-cron-action px-4 py-1.5 text-sm font-bold text-cron-action-foreground hover:bg-cron-action-hover disabled:cursor-not-allowed disabled:opacity-60"
                data-testid="heartbeat-panel-create-btn"
              >
                <Plus size={14} />
                {t('heartbeat.panel.create')}
                <ChevronDown size={14} />
              </button>
              {createMenuOpen && (
                <div className="absolute right-0 top-[calc(100%+6px)] z-20 w-44 rounded-lg border border-border bg-card py-1.5 shadow-lg" data-testid="heartbeat-panel-create-menu">
                  <button
                    type="button"
                    onClick={() => {
                      setCreateMenuOpen(false);
                      openCreateDrawer();
                    }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-semibold text-text hover:bg-bg-hover"
                    data-testid="heartbeat-panel-create-menu-item"
                    data-variant="manual"
                  >
                    <PencilLine size={14} />
                    {t('heartbeat.panel.createMenu.manual')}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setCreateMenuOpen(false);
                      createViaChat();
                    }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-semibold text-text hover:bg-bg-hover"
                    data-testid="heartbeat-panel-create-menu-item"
                    data-variant="viaChat"
                  >
                    <MessageSquarePlus size={14} />
                    {t('heartbeat.panel.createMenu.viaChat')}
                  </button>
                </div>
              )}
            </div>
            <button onClick={onClose} className="text-text-muted hover:text-text" data-testid="heartbeat-panel-close-btn">
              <X size={20} />
            </button>
          </div>
        </div>
      )}

      {drawer && meta ? (
        <div className="flex-1 overflow-y-auto" data-testid="heartbeat-panel-drawer-body">
          <HeartbeatTaskDrawer
            key={drawer.mode === 'edit' ? drawer.jobId : 'create'}
            mode={drawer.mode}
            initial={drawer.form}
            meta={meta}
            submitting={drawer.submitting}
            error={drawer.error}
            onSubmit={submitDrawer}
            onCancel={() => setDrawer(null)}
          />
        </div>
      ) : (
        <>
          {jobs.length > 0 && meta && (
            <div className="flex items-center gap-2 px-4 py-2" data-testid="heartbeat-panel-toolbar">
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder={t('heartbeat.panel.searchPlaceholder') ?? ''}
                className="flex-1 rounded-md border border-border bg-card px-2 py-1.5 text-sm"
                data-testid="heartbeat-panel-search-input"
              />
              <SimpleSelect
                value={statusFilter}
                onChange={(v) => setStatusFilter(v as HeartbeatJobStatus | 'all' | 'active')}
                options={statusFilterOptions}
                className="w-36"
              />
            </div>
          )}
          <div className="flex-1 overflow-y-auto p-4" data-testid="heartbeat-panel-list-scroll">
            {loading && <p className="text-sm text-text-muted" data-testid="heartbeat-panel-loading-text">{t('heartbeat.panel.loading')}</p>}
            {!loading && loadError && <p className="text-sm text-red-500" data-testid="heartbeat-panel-error-text">{loadError}</p>}
            {!loading && !loadError && jobs.length === 0 && (
              <p className="text-sm text-text-muted" data-testid="heartbeat-panel-empty-text">{t('heartbeat.panel.empty')}</p>
            )}
            {!loading && !loadError && jobs.length > 0 && filteredJobs.length === 0 && (
              <p className="text-sm text-text-muted" data-testid="heartbeat-panel-empty-filtered-text">{t('heartbeat.panel.emptyFiltered')}</p>
            )}
            {!loading && !loadError && displayedJobs.length > 0 && meta && (
              <ul className="space-y-3" data-testid="heartbeat-panel-task-list">
                {displayedJobs.map((job) => (
                  <li key={job.id} className="rounded-lg border border-border bg-card p-3 shadow-sm" data-testid="heartbeat-panel-task-item" data-variant={job.id}>
                    <div className="flex items-start justify-between gap-2">
                      <span className="min-w-0 truncate font-medium text-text-strong" title={job.name} data-testid="heartbeat-panel-task-name">
                        {job.name}
                      </span>
                      <HeartbeatStatusBadge status={job.status} />
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-sm text-text-muted" data-testid="heartbeat-panel-task-prompt">{job.prompt}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted" data-testid="heartbeat-panel-task-meta">
                      <span data-testid="heartbeat-panel-task-schedule">{summarizeHeartbeatSchedule(job.schedule, t)}</span>
                      {formatHeartbeatTimestamp(job.nextRunAt) && (
                        <span data-testid="heartbeat-panel-task-next-run">{t('heartbeat.panel.nextRunAt', { time: formatHeartbeatTimestamp(job.nextRunAt) })}</span>
                      )}
                      {job.runCount > 0 && <span data-testid="heartbeat-panel-task-run-count">{t('heartbeat.panel.runCount', { count: job.runCount })}</span>}
                      {/* §5.1：展示最近一次运行状态与错误；忙等待超时后显示 skipped + session_busy_timeout */}
                      {(() => {
                        const lastStatusKey = heartbeatLastRunStatusLabelKey(job.runState.last_run_status);
                        if (!lastStatusKey) return null;
                        const lastError = job.runState.last_error;
                        const errorText = lastError ? t('heartbeat.panel.lastError', { error: lastError }) : '';
                        const sep = lastStatusKey && errorText ? ' · ' : '';
                        return <span data-testid="heartbeat-panel-task-last-run">{t(lastStatusKey)}{sep}{errorText}</span>;
                      })()}
                    </div>
                    <div className="mt-3 flex flex-wrap justify-end gap-2 border-t border-border pt-2" data-testid="heartbeat-panel-task-actions">
                      {job.status === 'running' && (
                        <button
                          type="button"
                          disabled={actingJobId === job.id}
                          onClick={() => void handleCancel(job, false)}
                          className="inline-flex items-center gap-1 rounded-full border border-border px-3 py-1 text-xs text-text hover:bg-bg-hover disabled:opacity-60"
                          data-testid="heartbeat-panel-task-cancel-run-btn"
                        >
                          <CircleStop size={13} />
                          {t('heartbeat.panel.cancelRun')}
                        </button>
                      )}
                      <button
                        type="button"
                        disabled={actingJobId === job.id || !canHeartbeatRunNow(job.enabled, job.status)}
                        onClick={() => void handleRunNow(job)}
                        className="inline-flex items-center gap-1 rounded-full border border-border px-3 py-1 text-xs text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40"
                        data-testid="heartbeat-panel-task-run-now-btn"
                      >
                        <Play size={13} />
                        {t('heartbeat.panel.runNow')}
                      </button>
                      {(() => {
                        const isTerminal = job.status === 'completed' || job.status === 'expired';
                        const canEnable = canHeartbeatToggleEnable(
                          job.status,
                          job.maxRuns,
                          job.runCount,
                          job.schedule,
                        );
                        const toggleBtn = (
                          <button
                            type="button"
                            disabled={actingJobId === job.id || (!job.enabled && !canEnable)}
                            onClick={() => void handleToggle(job)}
                            className="inline-flex items-center gap-1 rounded-full border border-border px-3 py-1 text-xs text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40"
                            aria-pressed={job.enabled}
                            data-testid="heartbeat-panel-task-toggle-btn"
                          >
                            {job.enabled ? <Pause size={13} /> : <RotateCcw size={13} />}
                            {t(job.enabled ? 'heartbeat.panel.pause' : 'heartbeat.panel.resume')}
                          </button>
                        );
                        // disabled 按钮本身不触发 hover tooltip，终态时用外层 span 承载"如何重新激活"的提示
                        return isTerminal && !canEnable ? (
                          <span className="inline-flex" title={t('heartbeat.panel.resumeFromCompletedHint')}>
                            {toggleBtn}
                          </span>
                        ) : (
                          toggleBtn
                        );
                      })()}
                      <button
                        type="button"
                        disabled={drawerBusy}
                        onClick={() => openEditDrawer(job)}
                        className="inline-flex items-center gap-1 rounded-full border border-border px-3 py-1 text-xs text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-60"
                        data-testid="heartbeat-panel-task-edit-btn"
                      >
                        <Pencil size={13} />
                        {t('heartbeat.panel.edit')}
                      </button>
                      <button
                        type="button"
                        disabled={actingJobId === job.id}
                        onClick={() => {
                          setDeleteError(null);
                          setPendingDelete(job);
                        }}
                        className="inline-flex items-center gap-1 rounded-full border border-red-300 px-3 py-1 text-xs text-red-500 hover:bg-red-50 disabled:opacity-60"
                        data-testid="heartbeat-panel-task-delete-btn"
                      >
                        <Trash2 size={13} />
                        {t('heartbeat.panel.delete')}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {!loading && !loadError && filteredJobs.length > 0 && (
            <HeartbeatPagination
              currentPage={currentPage}
              totalPages={totalPages}
              pageSize={pageSize}
              totalCount={filteredJobs.length}
              onPageChange={setCurrentPage}
              onPageSizeChange={setPageSize}
            />
          )}
        </>
      )}
      {toast && (
        <div className="pointer-events-none fixed bottom-6 right-6 z-50 rounded-md bg-text-strong px-4 py-2 text-sm text-card shadow-lg" data-testid="heartbeat-panel-toast">
          {toast}
        </div>
      )}
      {pendingDelete && (
        <ConfirmDialog
          title={t('heartbeat.panel.delete')}
          message={deleteError ?? t('heartbeat.panel.deleteConfirm', { name: pendingDelete.name })}
          loading={deleting}
          onConfirm={() => void confirmDelete()}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}

/**
 * CronPanel 组件
 *
 * 定时任务面板，使用 cron 表达式管理定时任务
 */

import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { useSessionStore } from '../../stores/sessionStore';

const DEFAULT_CRON_TIMEZONE = 'Asia/Shanghai';
const DEFAULT_CRON_TARGET = 'web';

interface CronJob {
  id: string;
  name: string;
  enabled: boolean;
  expired?: boolean;
  cron_expr: string;
  timezone: string;
  wake_offset_seconds: number;
  description?: string;
  targets: string;
  schedule?: {
    kind: string;
    expr?: string;
    tz?: string;
    at?: string;
    everyMs?: number;
  };
  payload?: {
    kind: string;
    text?: string;
    message?: string;
  };
  delivery?: {
    mode: string;
    channel?: string;
  };
  session_target?: string;
  wake_mode?: string;
  compat_mode?: string;
  created_at: number | string | null;
  updated_at: number | string | null;
}

interface CronPreviewItem {
  wake_at: string;
  push_at: string;
}

interface CronJobInput {
  name: string;
  enabled: boolean;
  cron_expr: string;
  timezone: string;
  wake_offset_seconds: number;
  description: string;
  targets: string;
}

interface UpdateCronJob extends CronJobInput {
  id: string;
  created_at?: number | string | null;
  updated_at?: number | string | null;
}

interface CronPanelProps {
  sessionId: string;
}

function createEmptyJobInput(): CronJobInput {
  return {
    name: '',
    enabled: true,
    cron_expr: '',
    timezone: DEFAULT_CRON_TIMEZONE,
    wake_offset_seconds: 60,
    description: '',
    targets: DEFAULT_CRON_TARGET,
  };
}

function renderScheduleSummary(job: CronJob): string {
  if (!job.schedule) {
    return job.cron_expr || '';
  }
  if (job.schedule.kind === 'cron') {
    return job.schedule.expr || job.cron_expr || '';
  }
  if (job.schedule.kind === 'every') {
    return `every ${job.schedule.everyMs || 0} ms`;
  }
  if (job.schedule.kind === 'at') {
    return job.schedule.at || job.cron_expr || '';
  }
  return job.cron_expr || '';
}

function resolveCronExpr(job: CronJob): string {
  if (job.schedule?.kind === 'cron') {
    return (job.schedule.expr || job.cron_expr || '').trim();
  }
  return (job.cron_expr || '').trim();
}

function resolveTimezone(job: CronJob): string {
  if (job.schedule?.kind === 'cron') {
    return (job.schedule.tz || job.timezone || DEFAULT_CRON_TIMEZONE).trim() || DEFAULT_CRON_TIMEZONE;
  }
  return (job.timezone || DEFAULT_CRON_TIMEZONE).trim() || DEFAULT_CRON_TIMEZONE;
}

function resolveDescription(job: CronJob): string {
  if (job.payload?.kind === 'agentTurn') {
    return (job.payload.message || job.description || '').trim();
  }
  if (job.payload?.kind === 'systemEvent') {
    return (job.payload.text || job.description || '').trim();
  }
  return (job.description || '').trim();
}

function resolveTargets(job: CronJob): string {
  const deliveryChannel = (job.delivery?.channel || '').trim();
  if (deliveryChannel && deliveryChannel !== 'last') {
    return deliveryChannel;
  }
  return (job.targets || DEFAULT_CRON_TARGET).trim() || DEFAULT_CRON_TARGET;
}

function normalizeJobForEdit(job: CronJob): UpdateCronJob {
  return {
    id: job.id,
    name: (job.name || '').trim(),
    enabled: Boolean(job.enabled),
    cron_expr: resolveCronExpr(job),
    timezone: resolveTimezone(job),
    wake_offset_seconds: Number.isFinite(job.wake_offset_seconds) ? job.wake_offset_seconds : 0,
    description: resolveDescription(job),
    targets: resolveTargets(job),
    created_at: job.created_at,
    updated_at: job.updated_at,
  };
}

function buildLegacyJobInput(job: CronJobInput | UpdateCronJob, mode?: string): Record<string, unknown> {
  const result: Record<string, unknown> = {
    name: job.name.trim(),
    enabled: job.enabled,
    cron_expr: job.cron_expr.trim(),
    timezone: job.timezone.trim() || DEFAULT_CRON_TIMEZONE,
    wake_offset_seconds: Math.max(0, job.wake_offset_seconds || 0),
    description: job.description.trim(),
    targets: job.targets.trim() || DEFAULT_CRON_TARGET,
  };
  if (mode) {
    result['mode'] = mode;
  }
  return result;
}

export default function CronPanel({ sessionId }: CronPanelProps) {
  const { t } = useTranslation();
  const { mode } = useSessionStore();
  const [cronJobs, setCronJobs] = useState<CronJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [newJob, setNewJob] = useState<CronJobInput>(createEmptyJobInput());
  const [isCreating, setIsCreating] = useState(false);
  const [editingJobs, setEditingJobs] = useState<Record<string, UpdateCronJob>>({});
  const [previewJobId, setPreviewJobId] = useState<string | null>(null);
  const [previewRuns, setPreviewRuns] = useState<CronPreviewItem[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);

  // 时区选项
  const timezoneOptions = [
    { value: 'Asia/Shanghai', label: 'Asia/Shanghai' },
    { value: 'Asia/Bangkok', label: 'Asia/Bangkok' },
    { value: 'Asia/Tokyo', label: 'Asia/Tokyo' },
    { value: 'Asia/Seoul', label: 'Asia/Seoul' },
    { value: 'Asia/Singapore', label: 'Asia/Singapore' },
    { value: 'Europe/London', label: 'Europe/London' },
    { value: 'Europe/Paris', label: 'Europe/Paris' },
    { value: 'America/New_York', label: 'America/New_York' },
    { value: 'America/Los_Angeles', label: 'America/Los_Angeles' },
    { value: 'America/Chicago', label: 'America/Chicago' },
  ];

  // 目标选项
  const targetOptions = [
    { value: 'web', label: t('cron.targets.web') },
    { value: 'feishu', label: t('cron.targets.feishu') },
    { value: 'wecom', label: t('cron.targets.wecom') },
    { value: 'wechat', label: t('cron.targets.wechat') },
    { value: 'xiaoyi', label: t('cron.targets.xiaoyi'), disabled: true, style: { color: '#8c8c96ff' } },
    { value: 'dingtalk', label: t('cron.targets.dingtalk'), disabled: true, style: { color: '#8c8c96ff' } },
  ];

  // 加载任务列表
  const loadJobs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const payload = await webRequest<{ jobs: CronJob[] }>('cron.job.list');
      setCronJobs(payload.jobs || []);
    } catch (loadError) {
      const message = loadError instanceof Error ? loadError.message : t('cron.errors.loadJobs');
      setError(message);
      setCronJobs([]);
    } finally {
      setLoading(false);
    }
  }, [t]);

  // 初始化加载
  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  // 成功消息自动消失
  useEffect(() => {
    if (!success) return;
    const timer = window.setTimeout(() => {
      setSuccess(null);
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [success]);

  // 创建任务
  const handleCreateJob = async () => {
    if (!newJob.name) {
      setError(t('cron.errors.nameRequired'));
      return;
    }
    if (!newJob.cron_expr) {
      setError(t('cron.errors.cronRequired'));
      return;
    }
    if (!newJob.timezone) {
      setError(t('cron.errors.timezoneRequired'));
      return;
    }
    if (!newJob.targets) {
      setError(t('cron.errors.targetRequired'));
      return;
    }
    if (!newJob.description) {
      setError(t('cron.errors.descriptionRequired'));
      return;
    }

    try {
      await webRequest<{ job: CronJob }>('cron.job.create', {
        ...buildLegacyJobInput(newJob, mode),
        session_id: sessionId,
      });
      setSuccess(t('cron.success.created'));
      setIsCreating(false);
      setNewJob(createEmptyJobInput());
      await loadJobs();
    } catch (createError) {
      const message = createError instanceof Error ? createError.message : t('cron.errors.createFailed');
      setError(message);
    }
  };

  // 切换任务状态
  const handleToggleJob = async (id: string, enabled: boolean) => {
    try {
      await webRequest<{ job: CronJob }>('cron.job.toggle', {
        id,
        enabled: !enabled,
      });
      setSuccess(t('cron.success.statusUpdated'));
      await loadJobs();
    } catch (toggleError) {
      const message = toggleError instanceof Error ? toggleError.message : t('cron.errors.toggleFailed');
      setError(message);
    }
  };

  // 删除任务
  const handleDeleteJob = async (id: string) => {
    if (!window.confirm(t('cron.deleteConfirm'))) return;

    try {
      await webRequest<{ deleted: boolean }>('cron.job.delete', { id });
      setSuccess(t('cron.success.deleted'));
      await loadJobs();
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : t('cron.errors.deleteFailed');
      setError(message);
    }
  };

  const handleRunNow = async (id: string) => {
    try {
      await webRequest<{ run_id: string }>('cron.job.run_now', { id, session_id: sessionId });
      setSuccess(t('cron.success.runNow'));
    } catch (runError) {
      const message = runError instanceof Error ? runError.message : t('cron.errors.runNowFailed');
      setError(message);
    }
  };

  const handlePreviewRuns = async (id: string) => {
    setPreviewJobId(id);
    setPreviewLoading(true);
    try {
      const payload = await webRequest<{ next: CronPreviewItem[] }>('cron.job.preview', {
        id,
        count: 3,
        session_id: sessionId,
      });
      setPreviewRuns(payload.next || []);
    } catch (previewError) {
      const message = previewError instanceof Error ? previewError.message : t('cron.errors.previewFailed');
      setError(message);
      setPreviewRuns([]);
    } finally {
      setPreviewLoading(false);
    }
  };

  const formatPreviewTime = (value: string) => {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return value;
    }
    return parsed.toLocaleString();
  };

  // 准备更新任务
  const handleUpdateJob = async (id: string) => {
    try {
      const payload = await webRequest<{ job: CronJob }>('cron.job.get', { id, session_id: sessionId });
      setEditingJobs((prev) => ({
        ...prev,
        [id]: normalizeJobForEdit(payload.job),
      }));
    } catch (viewError) {
      const message = viewError instanceof Error ? viewError.message : t('cron.errors.loadDetailFailed');
      setError(message);
    }
  };

  // 执行更新任务
  const handleSubmitUpdate = async (jobId: string) => {
    const editJob = editingJobs[jobId];
    if (!editJob) return;

    if (!editJob.name) {
      setError(t('cron.errors.nameRequired'));
      return;
    }
    if (!editJob.cron_expr) {
      setError(t('cron.errors.cronRequired'));
      return;
    }
    if (!editJob.timezone) {
      setError(t('cron.errors.timezoneRequired'));
      return;
    }
    if (!editJob.targets) {
      setError(t('cron.errors.targetRequired'));
      return;
    }
    if (!editJob.description) {
      setError(t('cron.errors.descriptionRequired'));
      return;
    }

    try {
      const updateData: Record<string, unknown> = {
        id: editJob.id,
        patch: buildLegacyJobInput(editJob, mode),
      };

      await webRequest<{ job: CronJob }>('cron.job.update', {
        ...updateData,
        session_id: sessionId,
      });
      setSuccess(t('cron.success.updated'));
      setEditingJobs((prev) => {
        const next = { ...prev };
        delete next[jobId];
        return next;
      });
      await loadJobs();
    } catch (updateError) {
      const message = updateError instanceof Error ? updateError.message : t('cron.errors.updateFailed');
      setError(message);
    }
  };

  return (
    <div className="flex-1 min-h-0 relative" data-testid="cron-panel" data-session-id={sessionId}>
      {success && (
        <div className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 z-20" data-testid="cron-success">
          <div className="bg-ok text-white px-4 py-2 rounded-lg shadow-lg animate-rise text-sm">
            {success}
          </div>
        </div>
      )}

      <div className="card w-full h-full flex flex-col">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">{t('cron.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t('cron.subtitle')}</p>
          </div>
          <button
            onClick={() => setIsCreating(!isCreating)}
            className="btn primary !px-4 !py-2"
            data-testid="cron-create-toggle"
          >
            {isCreating ? t('cron.cancelCreate') : t('cron.createJob')}
          </button>
        </div>

        <div className="flex-1 min-h-0">
          {error && (
            <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-danger mb-4" data-testid="cron-error">
              {error}
            </div>
          )}

          {loading ? (
            <div className="rounded-lg border border-border bg-secondary/30 px-3 py-4 flex items-center justify-center">
              {t('cron.loading')}
            </div>
          ) : (
            <div className="overflow-auto rounded-lg border border-border max-h-[750px]">
              <table className="w-full border-collapse">
                <thead>
                  <tr className="border-b border-border sticky top-0 bg-bg">
                    <th className="px-4 py-3 text-left text-sm font-medium text-text-muted w-[160px]">{t('cron.columns.name')}</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-text-muted w-[200px]">{t('cron.columns.cron')}</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-text-muted">{t('cron.columns.status')}</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-text-muted w-[300px]">{t('cron.columns.description')}</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-text-muted w-[120px]">{t('cron.columns.wakeOffset')}</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-text-muted">{t('cron.columns.timezone')}</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-text-muted">{t('cron.columns.target')}</th>
                    <th className="px-4 py-3 text-left text-sm font-medium text-text-muted w-[160px]">{t('cron.columns.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {/* 创建任务行 */}
                  {isCreating && (
                    <tr className="border-b border-border bg-secondary/10 sticky top-[41px] z-5">
                      <td className="px-4 py-3">
                        <input
                          type="text"
                          value={newJob.name}
                          onChange={(e) => setNewJob({ ...newJob, name: e.target.value })}
                          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                          placeholder={t('cron.placeholders.name')}
                          data-testid="cron-create-name"
                        />
                      </td>
                      <td className="px-4 py-3">
                        <div className="relative">
                          <input
                            type="text"
                            value={newJob.cron_expr}
                            onChange={(e) => setNewJob({ ...newJob, cron_expr: e.target.value })}
                            className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent pr-8"
                            placeholder={t('cron.placeholders.cronShort')}
                            data-testid="cron-create-expr"
                          />
                          <span
                            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text cursor-help"
                            title={t('cron.placeholders.cron')}
                          >
                            <svg width="16" height="16" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
                              <circle cx="20" cy="20" r="18" fill="transparent" stroke="currentColor" strokeWidth="2" />
                              <text x="20" y="22" fontFamily="Arial, sans-serif" fontSize="24" fill="currentColor" textAnchor="middle" dominantBaseline="middle">?</text>
                            </svg>
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center">
                          <span className="text-sm mr-2">{newJob.enabled ? t('cron.status.enabled') : t('cron.status.disabled')}</span>
                          <div
                            className="relative inline-block w-10 h-6 align-middle select-none rounded-full cursor-pointer"
                            onClick={() => setNewJob({ ...newJob, enabled: !newJob.enabled })}
                            style={{ backgroundColor: newJob.enabled ? '#10b981' : '#d1d5db' }}
                          >
                            <div
                              className="absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform"
                              style={{ transform: newJob.enabled ? 'translateX(16px)' : 'none' }}
                            />
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <input
                          type="text"
                          value={newJob.description}
                          onChange={(e) => setNewJob({ ...newJob, description: e.target.value })}
                          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                          placeholder={t('cron.placeholders.description')}
                          data-testid="cron-create-description"
                        />
                      </td>
                      <td className="px-4 py-3">
                        <input
                          type="number"
                          value={newJob.wake_offset_seconds}
                          onChange={(e) => setNewJob({ ...newJob, wake_offset_seconds: parseInt(e.target.value, 10) || 0 })}
                          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                          placeholder={t('cron.placeholders.wakeOffset')}
                          data-testid="cron-create-wake-offset"
                        />
                      </td>
                      <td className="px-4 py-3">
                        <select
                          value={newJob.timezone}
                          onChange={(e) => setNewJob({ ...newJob, timezone: e.target.value })}
                          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                          data-testid="cron-create-timezone"
                        >
                          {timezoneOptions.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-4 py-3">
                        <select
                          value={newJob.targets}
                          onChange={(e) => setNewJob({ ...newJob, targets: e.target.value })}
                          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                          data-testid="cron-create-target"
                        >
                          {targetOptions.map((option) => (
                            <option key={option.value} value={option.value} disabled={option.disabled} style={option.style}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-enter gap-2">
                          <button
                            onClick={() => {
                              setIsCreating(false);
                              setNewJob({
                                name: '',
                                enabled: true,
                                cron_expr: '',
                                timezone: DEFAULT_CRON_TIMEZONE,
                                wake_offset_seconds: 0,
                                description: '',
                                targets: DEFAULT_CRON_TARGET,
                              });
                            }}
                            className="btn !px-3 !py-1.5"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            onClick={handleCreateJob}
                            className="btn primary !px-3 !py-1.5"
                            data-testid="cron-create-submit"
                          >
                            {t('cron.create')}
                          </button>
                        </div>
                      </td>
                    </tr>
                  )}

                  {/* 任务列表 */}
                  {cronJobs.length === 0 ? (
                    <tr>
                      <td colSpan={8} className="px-4 py-8 text-center text-text-muted">
                        {t('cron.empty')}
                      </td>
                    </tr>
                  ) : (
                    cronJobs.map((job) => {
                      const isEditing = editingJobs[job.id] !== undefined;
                      const editJob = editingJobs[job.id];
                      const displayCron = renderScheduleSummary(job);
                      const displayDescription = resolveDescription(job);
                      const displayTimezone = resolveTimezone(job);
                      const displayTarget = resolveTargets(job);

                      return isEditing && editJob ? (
                        <tr key={job.id} className="border-b border-border bg-secondary/10">
                          <td className="px-4 py-3">
                            <input
                              type="text"
                              value={editJob.name}
                              onChange={(e) => setEditingJobs((prev) => ({
                                ...prev,
                                [job.id]: { ...prev[job.id], name: e.target.value },
                              }))}
                              className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                              placeholder={t('cron.placeholders.name')}
                            />
                          </td>
                          <td className="px-4 py-3">
                            <div className="relative">
                              <input
                                type="text"
                                value={editJob.cron_expr}
                                onChange={(e) => setEditingJobs((prev) => ({
                                  ...prev,
                                  [job.id]: { ...prev[job.id], cron_expr: e.target.value },
                                }))}
                                className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent pr-8"
                                placeholder={t('cron.placeholders.cronShort')}
                              />
                              <span
                                className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text cursor-help"
                                title={t('cron.placeholders.cron')}
                              >
                                <svg width="16" height="16" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
                                  <circle cx="20" cy="20" r="18" fill="transparent" stroke="currentColor" strokeWidth="2" />
                                  <text x="20" y="22" fontFamily="Arial, sans-serif" fontSize="24" fill="currentColor" textAnchor="middle" dominantBaseline="middle">?</text>
                                </svg>
                              </span>
                            </div>
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex items-center">
                              <span className="text-sm mr-2">{editJob.enabled ? t('cron.status.enabled') : t('cron.status.disabled')}</span>
                              <div
                                className="relative inline-block w-10 h-6 align-middle select-none rounded-full cursor-pointer"
                                onClick={() => setEditingJobs((prev) => ({
                                  ...prev,
                                  [job.id]: { ...prev[job.id], enabled: !prev[job.id].enabled },
                                }))}
                                style={{ backgroundColor: editJob.enabled ? '#10b981' : '#d1d5db' }}
                              >
                                <div
                                  className="absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition-transform"
                                  style={{ transform: editJob.enabled ? 'translateX(16px)' : 'none' }}
                                />
                              </div>
                            </div>
                          </td>
                          <td className="px-4 py-3">
                            <input
                              type="text"
                              value={editJob.description || ''}
                              onChange={(e) => setEditingJobs((prev) => ({
                                ...prev,
                                [job.id]: { ...prev[job.id], description: e.target.value },
                              }))}
                              className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                              placeholder={t('cron.placeholders.description')}
                            />
                          </td>
                          <td className="px-4 py-3">
                            <input
                              type="number"
                              value={editJob.wake_offset_seconds}
                              onChange={(e) => setEditingJobs((prev) => ({
                                ...prev,
                                [job.id]: { ...prev[job.id], wake_offset_seconds: parseInt(e.target.value, 10) || 0 },
                              }))}
                              className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                              placeholder={t('cron.placeholders.wakeOffset')}
                            />
                          </td>
                          <td className="px-4 py-3">
                            <select
                              value={editJob.timezone}
                              onChange={(e) => setEditingJobs((prev) => ({
                                ...prev,
                                [job.id]: { ...prev[job.id], timezone: e.target.value },
                              }))}
                              className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                            >
                              {timezoneOptions.map((option) => (
                                <option key={option.value} value={option.value}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                          </td>
                          <td className="px-4 py-3">
                            <select
                              value={editJob.targets}
                              onChange={(e) => setEditingJobs((prev) => ({
                                ...prev,
                                [job.id]: { ...prev[job.id], targets: e.target.value },
                              }))}
                              className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                            >
                              {targetOptions.map((option) => (
                                <option key={option.value} value={option.value} disabled={option.disabled} style={option.style}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                          </td>
                          <td className="px-4 py-3 text-left">
                            <div className="flex items-center gap-2">
                              <button
                                onClick={() => setEditingJobs((prev) => {
                                  const next = { ...prev };
                                  delete next[job.id];
                                  return next;
                                })}
                                className="btn !px-3 !py-1.5"
                              >
                                {t('common.cancel')}
                              </button>
                              <button
                                onClick={() => handleSubmitUpdate(job.id)}
                                className="btn primary !px-3 !py-1.5"
                              >
                                {t('cron.update')}
                              </button>
                            </div>
                          </td>
                        </tr>
                      ) : (
                        <tr
                          key={job.id}
                          className="border-b border-border hover:bg-secondary/10"
                          data-testid={`cron-row-${job.id}`}
                          data-cron-id={job.id}
                          data-cron-name={job.name}
                        >
                          <td className="px-4 py-3 text-sm">
                            <div className="max-w-[100px] overflow-hidden text-ellipsis whitespace-nowrap" title={job.name}>
                              {job.name}
                            </div>
                          </td>
                          <td className="px-4 py-3 text-sm mono" data-testid={`cron-schedule-${job.id}`}>
                            {displayCron}
                          </td>
                          <td className="px-4 py-3">
                            <span
                              className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${
                                job.expired ? 'bg-amber-100 text-amber-700' : job.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-700'
                              }`}
                            >
                              {job.expired ? t('cron.status.expired') : job.enabled ? t('cron.status.enabled') : t('cron.status.disabled')}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-sm text-text-muted">
                            <div className="max-w-[300px] overflow-hidden text-ellipsis whitespace-nowrap" title={displayDescription || '-'}>
                              {displayDescription || '-'}
                            </div>
                            {previewJobId === job.id && (
                              <div className="mt-2 space-y-1 text-xs text-text-muted" data-testid={`cron-preview-${job.id}`}>
                                {previewLoading ? (
                                  <div>{t('cron.preview.loading')}</div>
                                ) : previewRuns.length > 0 ? (
                                  previewRuns.map((item, index) => (
                                    <div key={`${job.id}-${index}`} data-testid={`cron-preview-${job.id}-${index}`}>
                                      {t('cron.preview.label', { index: index + 1 })}: {formatPreviewTime(item.push_at)}
                                    </div>
                                  ))
                                ) : (
                                  <div>{t('cron.preview.empty')}</div>
                                )}
                              </div>
                            )}
                          </td>
                          <td className="px-4 py-3 text-sm text-text-muted">
                            {job.wake_offset_seconds}
                          </td>
                          <td className="px-4 py-3 text-sm text-text-muted">
                            {displayTimezone}
                          </td>
                          <td className="px-4 py-3 text-sm text-text-muted">
                            {displayTarget || '-'}
                          </td>
                          <td className="px-4 py-3 text-left">
                            <div className="flex items-center gap-4">
                              <span
                                onClick={() => handleRunNow(job.id)}
                                className="cursor-pointer text-sm text-accent"
                                data-testid={`cron-run-${job.id}`}
                              >
                                {t('cron.runNow')}
                              </span>
                              <span
                                onClick={() => handlePreviewRuns(job.id)}
                                className="cursor-pointer text-sm text-accent"
                                data-testid={`cron-preview-action-${job.id}`}
                              >
                                {t('cron.previewAction')}
                              </span>
                              <span
                                onClick={() => handleToggleJob(job.id, job.enabled)}
                                className={`cursor-pointer text-sm ${job.enabled ? 'text-danger' : 'text-accent'}`}
                                data-testid={`cron-toggle-${job.id}`}
                              >
                                {job.enabled ? t('cron.disable') : t('cron.enable')}
                              </span>
                              <span
                                onClick={() => handleUpdateJob(job.id)}
                                className="cursor-pointer text-sm text-accent"
                              >
                                {t('cron.update')}
                              </span>
                              <span
                                onClick={() => handleDeleteJob(job.id)}
                                className="cursor-pointer text-sm text-accent"
                                data-testid={`cron-delete-${job.id}`}
                              >
                                {t('cron.delete')}
                              </span>
                            </div>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

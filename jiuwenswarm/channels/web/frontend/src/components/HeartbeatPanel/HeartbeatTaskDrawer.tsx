import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { HeartbeatConcurrencyPolicy, HeartbeatMeta, HeartbeatSessionDeletedPolicy, HeartbeatTaskUI } from '../../types/heartbeat';
import {
  emptyHeartbeatScheduleForm,
  scheduleDtoToForm,
  type HeartbeatScheduleFormValue,
} from './heartbeatScheduleConvert';
import { validateHeartbeatCronExpr } from './heartbeatCronValidation';
import HeartbeatScheduleEditor from './HeartbeatScheduleEditor';
import SimpleSelect from '../CronPanel/SimpleSelect';
import { nowWallClock } from '../CronPanel/scheduleConvert';
import { Switch } from '../Switch';

const NAME_MAX_LENGTH = 64;
const PROMPT_MAX_LENGTH = 2000;

export interface HeartbeatTaskFormValue {
  name: string;
  prompt: string;
  schedule: HeartbeatScheduleFormValue;
  concurrencyPolicy: HeartbeatConcurrencyPolicy;
  sessionDeletedPolicy: HeartbeatSessionDeletedPolicy;
  maxRuns: number | null;
  enabled: boolean;
}

export function emptyHeartbeatTaskForm(meta: HeartbeatMeta): HeartbeatTaskFormValue {
  return {
    name: '',
    prompt: '',
    schedule: emptyHeartbeatScheduleForm('Asia/Shanghai'),
    concurrencyPolicy: meta.limits.default_concurrency_policy,
    sessionDeletedPolicy: meta.limits.default_session_deleted_policy,
    maxRuns: meta.limits.default_max_runs,
    enabled: true,
  };
}

export function jobToHeartbeatTaskForm(job: HeartbeatTaskUI): HeartbeatTaskFormValue {
  return {
    name: job.name,
    prompt: job.prompt,
    schedule: scheduleDtoToForm(job.schedule, job.timezone),
    concurrencyPolicy: job.concurrencyPolicy,
    sessionDeletedPolicy: job.sessionDeletedPolicy,
    maxRuns: job.maxRuns,
    enabled: job.enabled,
  };
}

interface HeartbeatTaskDrawerProps {
  mode: 'create' | 'edit';
  initial: HeartbeatTaskFormValue;
  meta: HeartbeatMeta;
  submitting: boolean;
  error: string | null;
  onSubmit: (value: HeartbeatTaskFormValue) => void;
  onCancel: () => void;
}

export default function HeartbeatTaskDrawer({ mode, initial, meta, submitting, error, onSubmit, onCancel }: HeartbeatTaskDrawerProps) {
  const { t } = useTranslation();
  const [form, setForm] = useState<HeartbeatTaskFormValue>(initial);

  // 「只触发一次」时用户可能停在抽屉里直到所选时间自然过期。每 10s 触发一次重渲染，
  // 让下方日期/时间选择器的禁选范围、以及提交前的「已过期」拦截随真实时间推进即时刷新。
  const [, forceNowTick] = useState(0);
  useEffect(() => {
    if (form.schedule.kind !== 'once') return;
    const id = setInterval(() => forceNowTick((n) => n + 1), 10000);
    return () => clearInterval(id);
  }, [form.schedule.kind]);

  const concurrencyOptions = meta.concurrency_policies.map((p) => ({ value: p, label: t(`heartbeat.concurrencyPolicy.${p}`) }));
  const sessionDeletedOptions = meta.session_deleted_policies.map((p) => ({
    value: p,
    label: t(`heartbeat.sessionDeletedPolicy.${p}`),
  }));

  // once 模式只触发一次，「最大触发次数」没有意义，整块不显示，提交时也强制 null
  const isOnce = form.schedule.kind === 'once';
  const maxRunsUnlimited = form.maxRuns === null;

  const missingFieldLabels: string[] = [];
  if (!form.name.trim()) missingFieldLabels.push(t('heartbeat.drawer.fieldName'));
  if (form.name.length > NAME_MAX_LENGTH) missingFieldLabels.push(t('heartbeat.drawer.fieldNameTooLong'));
  if (!form.prompt.trim()) missingFieldLabels.push(t('heartbeat.drawer.fieldPrompt'));
  if (form.prompt.length > PROMPT_MAX_LENGTH) missingFieldLabels.push(t('heartbeat.drawer.fieldPromptTooLong'));
  if (form.schedule.kind === 'cron') {
    const cronCheck = validateHeartbeatCronExpr(form.schedule.cronExpr);
    if (!form.schedule.cronExpr.trim() || !cronCheck.valid) missingFieldLabels.push(t('heartbeat.drawer.fieldSchedule'));
  }
  if (form.schedule.kind === 'once' && (!form.schedule.onceDate || !form.schedule.onceTime)) {
    missingFieldLabels.push(t('heartbeat.drawer.fieldSchedule'));
  }
  // 兜底：选择器已禁选过期日期时间，但用户停留在抽屉期间时间会流逝，选好的时间可能变成已过期。
  // 提交前再按表单时区核一次（每 10s tick 会重算），过期则阻止提交，并用红色单独提示——
  // 区别于下方灰色的「必填项未填」提示（见 bug002 用户确认第 2 点、第 3 轮反馈）。
  const scheduleExpired =
    form.schedule.kind === 'once' &&
    !!form.schedule.onceDate &&
    !!form.schedule.onceTime &&
    `${form.schedule.onceDate}T${form.schedule.onceTime}` <= nowWallClock(form.schedule.timezone);
  const canSubmit = missingFieldLabels.length === 0 && !scheduleExpired && !submitting;

  return (
    <div className="space-y-4 p-4" data-testid="heartbeat-panel-task-drawer">
      <div data-testid="heartbeat-panel-name-field">
        <label className="mb-1 block text-sm text-text-muted" data-testid="heartbeat-panel-name-label">
          {t('heartbeat.drawer.fieldName')}
          <span className="text-red-500">*</span>
        </label>
        <input
          type="text"
          value={form.name}
          maxLength={NAME_MAX_LENGTH}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          className="w-full rounded-md border border-border bg-card px-2 py-1.5 text-sm"
          data-testid="heartbeat-panel-name-input"
        />
      </div>
      <div data-testid="heartbeat-panel-prompt-field">
        <label className="mb-1 block text-sm text-text-muted" title={t('heartbeat.drawer.fieldPromptHint') ?? undefined} data-testid="heartbeat-panel-prompt-label">
          {t('heartbeat.drawer.fieldPrompt')}
          <span className="text-red-500">*</span>
        </label>
        <textarea
          value={form.prompt}
          maxLength={PROMPT_MAX_LENGTH}
          rows={4}
          onChange={(e) => setForm({ ...form, prompt: e.target.value })}
          className="w-full rounded-md border border-border bg-card px-2 py-1.5 text-sm"
          data-testid="heartbeat-panel-prompt-textarea"
        />
      </div>
      <div data-testid="heartbeat-panel-schedule-field">
        <label className="mb-1 block text-sm text-text-muted" title={t('heartbeat.drawer.fieldScheduleHint') ?? undefined} data-testid="heartbeat-panel-schedule-label">
          {t('heartbeat.drawer.fieldSchedule')}
          <span className="text-red-500">*</span>
        </label>
        <HeartbeatScheduleEditor
          value={form.schedule}
          onChange={(schedule) => setForm({ ...form, schedule })}
          minIntervalSeconds={meta.limits.min_interval_seconds}
        />
      </div>
      <div className="flex gap-4" data-testid="heartbeat-panel-policy-row">
        <div className="flex-1" data-testid="heartbeat-panel-concurrency-policy-field">
          <label
            className="mb-1 block text-sm text-text-muted"
            title={t('heartbeat.drawer.fieldConcurrencyPolicyHint') ?? undefined}
            data-testid="heartbeat-panel-concurrency-policy-label"
          >
            {t('heartbeat.drawer.fieldConcurrencyPolicy')}
          </label>
          <SimpleSelect
            value={form.concurrencyPolicy}
            onChange={(v) => setForm({ ...form, concurrencyPolicy: v as HeartbeatConcurrencyPolicy })}
            options={concurrencyOptions}
          />
        </div>
        {mode === 'edit' && (
          <div className="flex-1" data-testid="heartbeat-panel-session-deleted-policy-field">
            <label
              className="mb-1 block text-sm text-text-muted"
              title={t('heartbeat.drawer.fieldSessionDeletedPolicyHint') ?? undefined}
              data-testid="heartbeat-panel-session-deleted-policy-label"
            >
              {t('heartbeat.drawer.fieldSessionDeletedPolicy')}
            </label>
            <SimpleSelect
              value={form.sessionDeletedPolicy}
              onChange={(v) => setForm({ ...form, sessionDeletedPolicy: v as HeartbeatSessionDeletedPolicy })}
              options={sessionDeletedOptions}
            />
          </div>
        )}
      </div>
      {!isOnce && (
        <div data-testid="heartbeat-panel-max-runs-field">
          <label className="mb-1 block text-sm text-text-muted" title={t('heartbeat.drawer.fieldMaxRunsHint') ?? undefined} data-testid="heartbeat-panel-max-runs-label">
            {t('heartbeat.drawer.fieldMaxRuns')}
          </label>
          {/* 单行：次数输入框在前，「不限次数」开关在后。开关打开 → maxRuns=null，输入框仍显示但禁用、
              清空、置灰（不再渲染在开关前会太挤，保留输入框让布局稳定）。关闭 → 恢复默认次数并可编辑。 */}
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              value={maxRunsUnlimited ? '' : (form.maxRuns ?? '')}
              disabled={maxRunsUnlimited}
              onChange={(e) => setForm({ ...form, maxRuns: e.target.value === '' ? null : Math.max(1, Number(e.target.value)) })}
              className="w-24 rounded-md border border-border bg-card px-2 py-1.5 text-sm disabled:cursor-not-allowed disabled:bg-bg-muted disabled:opacity-60"
              data-testid="heartbeat-panel-max-runs-input"
            />
            <Switch
              checked={maxRunsUnlimited}
              onChange={(unlimited) =>
                setForm({ ...form, maxRuns: unlimited ? null : (form.maxRuns ?? meta.limits.default_max_runs) })
              }
              title={t('heartbeat.drawer.fieldMaxRunsUnlimitedOption') ?? undefined}
            />
            <span className="text-sm text-text-muted" data-testid="heartbeat-panel-max-runs-unlimited-label">
              {t('heartbeat.drawer.fieldMaxRunsUnlimitedOption')}
            </span>
          </div>
        </div>
      )}

      {error && <p className="text-sm text-red-500" data-testid="heartbeat-panel-drawer-error">{error}</p>}
      {!error && scheduleExpired && (
        <p className="text-xs text-red-500" data-testid="heartbeat-panel-drawer-schedule-expired">{t('heartbeat.drawer.scheduleExpired')}</p>
      )}
      {!error && !scheduleExpired && missingFieldLabels.length > 0 && (
        <p className="text-xs text-text-muted" data-testid="heartbeat-panel-drawer-missing-fields">{t('heartbeat.drawer.missingFields', { fields: missingFieldLabels.join('、') })}</p>
      )}

      <div className="flex justify-end gap-2 pt-2" data-testid="heartbeat-panel-drawer-actions">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-full border border-border bg-card px-6 py-1.5 text-sm text-text hover:bg-bg-hover"
          data-testid="heartbeat-panel-drawer-cancel-btn"
        >
          {t('common.cancel')}
        </button>
        <button
          type="button"
          disabled={!canSubmit}
          onClick={() => onSubmit({
            ...form,
            // once 只触发一次，max_runs 无意义，一律传 null（不限触发）
            maxRuns: form.schedule.kind === 'once' ? null : form.maxRuns,
          })}
          className="rounded-full bg-cron-action px-6 py-1.5 text-sm font-bold text-cron-action-foreground hover:bg-cron-action-hover disabled:cursor-not-allowed disabled:opacity-60"
          data-testid="heartbeat-panel-drawer-submit-btn"
          data-variant={mode}
        >
          {mode === 'create' ? t('heartbeat.drawer.submitCreate') : t('heartbeat.drawer.submitUpdate')}
        </button>
      </div>
    </div>
  );
}

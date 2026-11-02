import { useState } from 'react';
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

  const concurrencyOptions = meta.concurrency_policies.map((p) => ({ value: p, label: t(`heartbeat.concurrencyPolicy.${p}`) }));
  const sessionDeletedOptions = meta.session_deleted_policies.map((p) => ({
    value: p,
    label: t(`heartbeat.sessionDeletedPolicy.${p}`),
  }));

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
  const canSubmit = missingFieldLabels.length === 0 && !submitting;

  return (
    <div className="space-y-4 p-4">
      <div>
        <label className="mb-1 block text-sm text-text-muted">
          {t('heartbeat.drawer.fieldName')}
          <span className="text-red-500">*</span>
        </label>
        <input
          type="text"
          value={form.name}
          maxLength={NAME_MAX_LENGTH}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          className="w-full rounded-md border border-border bg-card px-2 py-1.5 text-sm"
        />
      </div>
      <div>
        <label className="mb-1 block text-sm text-text-muted" title={t('heartbeat.drawer.fieldPromptHint') ?? undefined}>
          {t('heartbeat.drawer.fieldPrompt')}
          <span className="text-red-500">*</span>
        </label>
        <textarea
          value={form.prompt}
          maxLength={PROMPT_MAX_LENGTH}
          rows={4}
          onChange={(e) => setForm({ ...form, prompt: e.target.value })}
          className="w-full rounded-md border border-border bg-card px-2 py-1.5 text-sm"
        />
      </div>
      <div>
        <label className="mb-1 block text-sm text-text-muted" title={t('heartbeat.drawer.fieldScheduleHint') ?? undefined}>
          {t('heartbeat.drawer.fieldSchedule')}
          <span className="text-red-500">*</span>
        </label>
        <HeartbeatScheduleEditor
          value={form.schedule}
          onChange={(schedule) => setForm({ ...form, schedule })}
          minIntervalSeconds={meta.limits.min_interval_seconds}
        />
      </div>
      <div className="flex gap-4">
        <div className="flex-1">
          <label
            className="mb-1 block text-sm text-text-muted"
            title={t('heartbeat.drawer.fieldConcurrencyPolicyHint') ?? undefined}
          >
            {t('heartbeat.drawer.fieldConcurrencyPolicy')}
          </label>
          <SimpleSelect
            value={form.concurrencyPolicy}
            onChange={(v) => setForm({ ...form, concurrencyPolicy: v as HeartbeatConcurrencyPolicy })}
            options={concurrencyOptions}
          />
        </div>
        <div className="flex-1">
          <label
            className="mb-1 block text-sm text-text-muted"
            title={t('heartbeat.drawer.fieldSessionDeletedPolicyHint') ?? undefined}
          >
            {t('heartbeat.drawer.fieldSessionDeletedPolicy')}
          </label>
          <SimpleSelect
            value={form.sessionDeletedPolicy}
            onChange={(v) => setForm({ ...form, sessionDeletedPolicy: v as HeartbeatSessionDeletedPolicy })}
            options={sessionDeletedOptions}
          />
        </div>
      </div>
      <div>
        <label className="mb-1 block text-sm text-text-muted" title={t('heartbeat.drawer.fieldMaxRunsHint') ?? undefined}>
          {t('heartbeat.drawer.fieldMaxRuns')}
        </label>
        <input
          type="number"
          min={1}
          value={form.maxRuns ?? ''}
          placeholder={t('heartbeat.drawer.fieldMaxRunsUnlimited') ?? ''}
          onChange={(e) => setForm({ ...form, maxRuns: e.target.value === '' ? null : Math.max(1, Number(e.target.value)) })}
          className="w-32 rounded-md border border-border bg-card px-2 py-1.5 text-sm"
        />
      </div>

      {error && <p className="text-sm text-red-500">{error}</p>}
      {!error && missingFieldLabels.length > 0 && (
        <p className="text-xs text-text-muted">{t('heartbeat.drawer.missingFields', { fields: missingFieldLabels.join('、') })}</p>
      )}

      <div className="flex justify-end gap-2 pt-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-full border border-border bg-card px-6 py-1.5 text-sm text-text hover:bg-bg-hover"
        >
          {t('common.cancel')}
        </button>
        <button
          type="button"
          disabled={!canSubmit}
          onClick={() => onSubmit(form)}
          className="rounded-full bg-cron-action px-6 py-1.5 text-sm font-bold text-cron-action-foreground hover:bg-cron-action-hover disabled:cursor-not-allowed disabled:opacity-60"
        >
          {mode === 'create' ? t('heartbeat.drawer.submitCreate') : t('heartbeat.drawer.submitUpdate')}
        </button>
      </div>
    </div>
  );
}

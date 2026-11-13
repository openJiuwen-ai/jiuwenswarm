import { useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { buildCron, fromZonedInput, parseCron, toZonedInput, validateDraft, weekdayName } from './state';
import type { Schedule, ScheduleDraft } from './types';

type Mode = 'period' | 'interval' | 'once';
type Frequency = 'daily' | 'weekly' | 'monthly';

interface FormState {
  name: string;
  prompt: string;
  mode: Mode;
  frequency: Frequency;
  time: string;
  weekday: number;
  monthDay: number;
  intervalValue: string;
  intervalUnit: 'minutes' | 'hours';
  onceDate: string;
  onceTime: string;
  startsOn: string;
  endsOn: string;
}

const WEEKDAY_VALUES = [1, 2, 3, 4, 5, 6, 0];

function blankForm(): FormState {
  return {
    name: '',
    prompt: '',
    mode: 'period',
    frequency: 'daily',
    time: '09:00',
    weekday: 1,
    monthDay: 1,
    intervalValue: '1',
    intervalUnit: 'hours',
    onceDate: '',
    onceTime: '09:00',
    startsOn: '',
    endsOn: '',
  };
}

function formFromPlan(plan: Schedule): FormState {
  const form = { ...blankForm(), name: plan.name, prompt: plan.prompt };
  if (plan.startsAt) form.startsOn = toZonedInput(plan.startsAt, plan.timezone).slice(0, 10);
  if (plan.endsAt) form.endsOn = toZonedInput(plan.endsAt, plan.timezone).slice(0, 10);
  if (plan.scheduleKind === 'interval' && plan.intervalSeconds) {
    form.mode = 'interval';
    if (plan.intervalSeconds % 3600 === 0) {
      form.intervalUnit = 'hours';
      form.intervalValue = String(plan.intervalSeconds / 3600);
    } else {
      form.intervalUnit = 'minutes';
      form.intervalValue = String(Math.max(1, Math.round(plan.intervalSeconds / 60)));
    }
    return form;
  }
  if (plan.scheduleKind === 'once' && plan.runAt) {
    const local = toZonedInput(plan.runAt, plan.timezone);
    form.mode = 'once';
    form.onceDate = local.slice(0, 10);
    form.onceTime = local.slice(11, 16);
    return form;
  }
  const parsed = parseCron(plan.cronExpr);
  if (parsed.frequency === 'weekly' || parsed.frequency === 'monthly' || parsed.frequency === 'daily') {
    form.frequency = parsed.frequency;
    form.time = parsed.time;
    form.weekday = parsed.day;
    form.monthDay = parsed.day;
  }
  return form;
}

function toDraft(form: FormState, timezone: string): ScheduleDraft {
  const startsAt = form.startsOn ? fromZonedInput(`${form.startsOn}T00:00`, timezone) : null;
  const endsAt = form.endsOn ? fromZonedInput(`${form.endsOn}T23:59`, timezone) : null;
  const base = { name: form.name.trim(), prompt: form.prompt.trim(), timezone, startsAt, endsAt };
  if (form.mode === 'interval') {
    const amount = Number(form.intervalValue);
    const seconds = form.intervalUnit === 'minutes' ? amount * 60 : amount * 3600;
    return { ...base, scheduleKind: 'interval', intervalSeconds: Number.isFinite(seconds) ? seconds : 0, cronExpr: '', runAt: null };
  }
  if (form.mode === 'once') {
    return {
      ...base,
      scheduleKind: 'once',
      intervalSeconds: null,
      cronExpr: '',
      runAt: form.onceDate && form.onceTime ? fromZonedInput(`${form.onceDate}T${form.onceTime}`, timezone) : null,
    };
  }
  const day = form.frequency === 'weekly' ? form.weekday : form.monthDay;
  return { ...base, scheduleKind: 'cron', intervalSeconds: null, runAt: null, cronExpr: buildCron(form.frequency, form.time, day) };
}

export function ScheduleDialog({
  open,
  plan,
  executorId,
  executors,
  saving,
  timezone = 'Asia/Shanghai',
  onClose,
  onSubmit,
}: {
  open: boolean;
  plan?: Schedule | null;
  executorId: string;
  executors?: readonly { id: string; name: string }[];
  saving?: boolean;
  timezone?: string;
  onClose: () => void;
  onSubmit: (executorId: string, draft: ScheduleDraft) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<FormState>(() => (plan ? formFromPlan(plan) : blankForm()));
  const executorRef = useRef<HTMLSelectElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const endsOnRef = useRef<HTMLInputElement>(null);
  const [chosenExecutor, setChosenExecutor] = useState(executorId);
  const [submitError, setSubmitError] = useState('');
  const [validationAttempted, setValidationAttempted] = useState(false);
  const errorId = useId();
  const executorErrorId = `${errorId}-executor`;
  const nameErrorId = `${errorId}-name`;
  const promptErrorId = `${errorId}-prompt`;
  const rangeErrorId = `${errorId}-range`;
  if (!open) return null;
  const draft = toDraft(form, timezone);
  const validationReason = validationAttempted ? validateDraft(draft, chosenExecutor) : null;
  const executorInvalid = validationReason === 'executorRequired';
  const nameInvalid = validationReason === 'required' && !draft.name;
  const promptInvalid = validationReason === 'required' && !draft.prompt;
  const rangeInvalid = validationReason === 'rangeInvalid';
  const hasFieldError = (executorInvalid && executors !== undefined) || nameInvalid || promptInvalid || rangeInvalid;
  const validationMessage = validationReason && !hasFieldError ? t(`jiuwenmate.schedule.error.${validationReason}`) : '';
  const save = () => {
    const reason = validateDraft(draft, chosenExecutor);
    setValidationAttempted(true);
    setSubmitError('');
    if (reason) {
      if (reason === 'executorRequired') executorRef.current?.focus();
      else if (reason === 'required' && !draft.name) nameRef.current?.focus();
      else if (reason === 'required' && !draft.prompt) promptRef.current?.focus();
      else if (reason === 'rangeInvalid') endsOnRef.current?.focus();
      return;
    }
    void Promise.resolve(onSubmit(chosenExecutor, draft))
      .then(() => onClose())
      .catch((failure: unknown) => {
        const message = failure instanceof Error ? failure.message : '';
        setSubmitError(message || t('jiuwenmate.schedule.error.required'));
      });
  };
  const patch = (next: Partial<FormState>) => {
    setForm((current) => ({ ...current, ...next }));
    setSubmitError('');
  };
  return (
    <div className="jiuwenmate-dialog-backdrop" data-testid="jiuwenmate-schedule-dialog">
      <div className="jiuwenmate-dialog jiuwenmate-schedule-dialog" role="dialog" aria-modal="true" aria-labelledby="jiuwenmate-schedule-dialog-title">
        <button type="button" className="jiuwenmate-dialog__close" aria-label={t('jiuwenmate.schedule.cancel')} onClick={onClose}>
          <X aria-hidden />
        </button>
        <h2 id="jiuwenmate-schedule-dialog-title" className="jiuwenmate-dialog__title">
          {plan ? t('jiuwenmate.schedule.editTitle') : t('jiuwenmate.schedule.createTitle')}
        </h2>
        <div className="jiuwenmate-dialog__body">
          {executors ? (
            <label className="jiuwenmate-field">
              <span>{t('jiuwenmate.schedule.executor')}</span>
              <select
                ref={executorRef}
                className="jiuwenmate-schedule-dialog__executor"
                required
                value={chosenExecutor}
                aria-label={t('jiuwenmate.schedule.executor')}
                aria-invalid={executorInvalid || undefined}
                aria-describedby={executorInvalid ? executorErrorId : undefined}
                onChange={(event) => {
                  setChosenExecutor(event.target.value);
                  setSubmitError('');
                }}
                data-testid="jiuwenmate-schedule-executor"
              >
                <option value="">{t('jiuwenmate.schedule.chooseExecutor')}</option>
                {executors.map((item) => (
                  <option key={item.id} value={item.id}>{item.name}</option>
                ))}
              </select>
              {executorInvalid ? (
                <span id={executorErrorId} role="alert" data-testid="jiuwenmate-schedule-executor-error">
                  {t('jiuwenmate.schedule.error.executorRequired')}
                </span>
              ) : null}
            </label>
          ) : null}
          <label className="jiuwenmate-field">
            <span>{t('jiuwenmate.schedule.name')}</span>
            <input
              ref={nameRef}
              value={form.name}
              placeholder={t('jiuwenmate.schedule.placeholder')}
              aria-label={t('jiuwenmate.schedule.name')}
              aria-invalid={nameInvalid || undefined}
              aria-describedby={nameInvalid ? nameErrorId : undefined}
              onChange={(event) => patch({ name: event.target.value })}
              data-testid="jiuwenmate-schedule-name"
            />
            {nameInvalid ? (
              <span id={nameErrorId} role="alert" data-testid="jiuwenmate-schedule-name-error">
                {t('jiuwenmate.schedule.error.nameRequired')}
              </span>
            ) : null}
          </label>
          <label className="jiuwenmate-field">
            <span>{t('jiuwenmate.schedule.prompt')}</span>
            <textarea
              ref={promptRef}
              value={form.prompt}
              placeholder={t('jiuwenmate.schedule.placeholder')}
              aria-label={t('jiuwenmate.schedule.prompt')}
              aria-invalid={promptInvalid || undefined}
              aria-describedby={promptInvalid ? promptErrorId : undefined}
              onChange={(event) => patch({ prompt: event.target.value })}
              data-testid="jiuwenmate-schedule-prompt"
            />
            {promptInvalid ? (
              <span id={promptErrorId} role="alert" data-testid="jiuwenmate-schedule-prompt-error">
                {t('jiuwenmate.schedule.error.promptRequired')}
              </span>
            ) : null}
          </label>
          <div className="jiuwenmate-field">
            <span>{t('jiuwenmate.schedule.plan')}</span>
            <div className="jiuwenmate-schedule-modes" role="tablist">
              {(['period', 'interval', 'once'] as const).map((mode) => (
                <button key={mode} type="button" role="tab" aria-selected={form.mode === mode} onClick={() => patch({ mode })}>
                  {t(`jiuwenmate.schedule.mode.${mode}`)}
                </button>
              ))}
            </div>
            {form.mode === 'period' ? (
              <div className="jiuwenmate-schedule-plan">
                <select value={form.frequency} aria-label={t('jiuwenmate.schedule.frequency')} onChange={(event) => patch({ frequency: event.target.value as Frequency })}>
                  <option value="daily">{t('jiuwenmate.schedule.daily')}</option>
                  <option value="weekly">{t('jiuwenmate.schedule.weekly')}</option>
                  <option value="monthly">{t('jiuwenmate.schedule.monthly')}</option>
                </select>
                {form.frequency === 'weekly' ? (
                  <select value={form.weekday} aria-label={t('jiuwenmate.schedule.weekday')} onChange={(event) => patch({ weekday: Number(event.target.value) })}>
                    {WEEKDAY_VALUES.map((day) => (
                      <option key={day} value={day}>{weekdayName(day)}</option>
                    ))}
                  </select>
                ) : null}
                {form.frequency === 'monthly' ? (
                  <input type="number" min={1} max={31} value={form.monthDay} aria-label={t('jiuwenmate.schedule.monthDay')} onChange={(event) => patch({ monthDay: Number(event.target.value) })} />
                ) : null}
                <input type="time" value={form.time} aria-label={t('jiuwenmate.schedule.time')} onChange={(event) => patch({ time: event.target.value })} />
              </div>
            ) : null}
            {form.mode === 'interval' ? (
              <div className="jiuwenmate-schedule-plan">
                <input value={form.intervalValue} inputMode="numeric" aria-label={t('jiuwenmate.schedule.interval')} onChange={(event) => patch({ intervalValue: event.target.value.replace(/[^\d]/g, '') })} data-testid="jiuwenmate-schedule-interval" />
                <select value={form.intervalUnit} aria-label={t('jiuwenmate.schedule.intervalUnit')} onChange={(event) => patch({ intervalUnit: event.target.value as 'minutes' | 'hours' })}>
                  <option value="minutes">{t('jiuwenmate.schedule.minutes')}</option>
                  <option value="hours">{t('jiuwenmate.schedule.hours')}</option>
                </select>
              </div>
            ) : null}
            {form.mode === 'once' ? (
              <div className="jiuwenmate-schedule-plan">
                <input type="date" value={form.onceDate} aria-label={t('jiuwenmate.schedule.onceDate')} onChange={(event) => patch({ onceDate: event.target.value })} />
                <input type="time" value={form.onceTime} aria-label={t('jiuwenmate.schedule.time')} onChange={(event) => patch({ onceTime: event.target.value })} />
              </div>
            ) : null}
          </div>
          <div className="jiuwenmate-field">
            <span>{t('jiuwenmate.schedule.effective')}</span>
            <div className="jiuwenmate-schedule-plan">
              <input
                type="date"
                value={form.startsOn}
                aria-label={t('jiuwenmate.schedule.startsOn')}
                aria-invalid={rangeInvalid || undefined}
                aria-describedby={rangeInvalid ? rangeErrorId : undefined}
                onChange={(event) => patch({ startsOn: event.target.value })}
                data-testid="jiuwenmate-schedule-starts-on"
              />
              <input
                ref={endsOnRef}
                type="date"
                value={form.endsOn}
                aria-label={t('jiuwenmate.schedule.endsOn')}
                aria-invalid={rangeInvalid || undefined}
                aria-describedby={rangeInvalid ? rangeErrorId : undefined}
                onChange={(event) => patch({ endsOn: event.target.value })}
                data-testid="jiuwenmate-schedule-ends-on"
              />
            </div>
            {rangeInvalid ? (
              <span id={rangeErrorId} role="alert" data-testid="jiuwenmate-schedule-range-error">
                {t('jiuwenmate.schedule.error.rangeInvalid')}
              </span>
            ) : null}
          </div>
          {submitError || validationMessage ? <p role="alert">{submitError || validationMessage}</p> : null}
        </div>
        <div className="jiuwenmate-dialog__footer">
          <button type="button" className="jiuwenmate-outline-button" onClick={onClose}>{t('jiuwenmate.schedule.cancel')}</button>
          <button type="button" className="jiuwenmate-outline-button" onClick={save} disabled={saving} data-testid="jiuwenmate-schedule-save">
            {t('jiuwenmate.schedule.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}

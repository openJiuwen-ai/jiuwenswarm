import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import {
  dismissReminder,
  formatLongHorizonDue,
  getReminders,
  subscribeReminders,
} from '../../features/longHorizon/reminderState';
import { runLongHorizonStageAction } from '../../features/longHorizon/stageAction';
import type { LongHorizonReminderToast } from '../../types/longHorizon';

export default function LongHorizonReminderToasts() {
  const [toasts, setToasts] = useState(getReminders);
  useEffect(() => subscribeReminders(() => setToasts(getReminders())), []);
  if (toasts.length === 0) return null;
  return (
    <div
      className="app-toast-wrapper app-toast-wrapper--bottom-right"
      data-testid="long-horizon-reminder-toasts"
    >
      {toasts.map((toast) => (
        <LongHorizonReminderToastCard key={toast.id} toast={toast} />
      ))}
    </div>
  );
}

function LongHorizonReminderToastCard({ toast }: { toast: LongHorizonReminderToast }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (action: 'start' | 'snooze' | 'skip') => {
    setBusy(action);
    setError(null);
    try {
      await runLongHorizonStageAction({
        taskId: toast.taskId,
        stageId: toast.stageId,
        action,
        execSessionId: toast.execSessionId,
        title: toast.title,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : t('longHorizon.errors.actionFailed'));
      setBusy(null);
    }
  };

  return (
    <div
      className="w-[min(90vw,360px)] rounded-lg border border-border bg-card p-4 shadow-xl"
      role="alertdialog"
      aria-label={t('longHorizon.toastTitle')}
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 text-sm text-text-muted">
          <span className="font-semibold text-text">{t('longHorizon.toastTitle')}</span>
        </div>
        <button
          type="button"
          className="rounded p-0.5 text-text-muted hover:bg-bg-muted hover:text-text"
          aria-label={t('common.close')}
          onClick={() => dismissReminder(toast.id)}
        >
          <X size={14} />
        </button>
      </div>
      <div className="text-sm font-medium text-text-strong">
        {toast.title}
        {toast.stageTitle ? ` · ${toast.stageTitle}` : ''}
      </div>
      {toast.hint ? <p className="mt-1 text-xs text-text-muted">{toast.hint}</p> : null}
      {toast.dueAt ? (
        <p className="mt-1 text-xs text-text-muted">
          {t('longHorizon.dueLabel')} · {formatLongHorizonDue(toast.dueAt)}
        </p>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          className="btn primary !px-2.5 !py-1 !text-xs"
          disabled={busy !== null}
          onClick={() => void run('start')}
        >
          {t('longHorizon.actions.start')}
        </button>
        <button
          type="button"
          className="btn !px-2.5 !py-1 !text-xs"
          disabled={busy !== null}
          onClick={() => void run('snooze')}
        >
          {t('longHorizon.actions.snooze')}
        </button>
        <button
          type="button"
          className="btn !px-2.5 !py-1 !text-xs"
          disabled={busy !== null}
          onClick={() => void run('skip')}
        >
          {t('longHorizon.actions.skip')}
        </button>
      </div>
      {error ? <div className="mt-2 text-xs text-danger">{error}</div> : null}
    </div>
  );
}

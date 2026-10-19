import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Clock3, History, PencilLine, Play, Trash2 } from 'lucide-react';
import scheduleEmpty from '../../../assets/jiuwenmate/illustrations/schedule-empty.png';
import { ActionMenu } from '../components/ActionMenu';
import { ScheduleDialog } from './ScheduleDialog';
import { ScheduleHistory } from './ScheduleHistory';
import { scheduleSummary } from './state';
import type { Schedule, ScheduleDraft, ScheduleRun } from './types';

export function ScheduleSection({
  schedules,
  saving,
  onCreate,
  onUpdate,
  onSetEnabled,
  onDelete,
  onRun,
  onLoadHistory,
}: {
  schedules: readonly Schedule[];
  saving?: boolean;
  onCreate?: (draft: ScheduleDraft) => void | Promise<void>;
  onUpdate?: (planId: string, draft: ScheduleDraft) => void | Promise<void>;
  onSetEnabled?: (planId: string, enabled: boolean) => void | Promise<void>;
  onDelete?: (planId: string) => void | Promise<void>;
  onRun?: (planId: string) => void | Promise<void>;
  onLoadHistory?: (planId: string) => Promise<{ plan: Schedule; runs: ScheduleRun[] }>;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<Schedule | null | undefined>(undefined);
  const [history, setHistory] = useState<{ plan: Schedule; runs: ScheduleRun[] } | null>(null);
  const openHistory = (planId: string) => {
    if (!onLoadHistory) return;
    void onLoadHistory(planId).then(setHistory).catch(() => undefined);
  };
  if (history) {
    return <ScheduleHistory plan={history.plan} runs={history.runs} onBack={() => setHistory(null)} />;
  }
  return (
    <div className="jiuwenmate-schedule-section" data-testid="jiuwenmate-schedule-section">
      <div className="jiuwenmate-schedule-section__bar">
        <span />
        <button type="button" className="jiuwenmate-outline-button jiuwenmate-outline-button--small" onClick={() => setEditing(null)}>
          {t('jiuwenmate.schedule.add')}
        </button>
      </div>
      {schedules.length === 0 ? (
        <div className="jiuwenmate-empty-state">
          <img src={scheduleEmpty} alt="" aria-hidden />
          <p>{t('jiuwenmate.workspace.noSchedules')}</p>
          <button type="button" className="jiuwenmate-outline-button jiuwenmate-outline-button--small" onClick={() => setEditing(null)}>
            {t('jiuwenmate.schedule.createTitle')}
          </button>
        </div>
      ) : (
        <ul className="jiuwenmate-schedule-cards">
          {schedules.map((plan) => (
            <li key={plan.id} className="jiuwenmate-schedule-card" data-testid="jiuwenmate-schedule-card">
              <div>
                <strong>{plan.name}</strong>
                <small>
                  <Clock3 aria-hidden />
                  {scheduleSummary(plan)}
                </small>
              </div>
              <div className="jiuwenmate-schedule-card__actions">
                <ActionMenu
                  label={t('jiuwenmate.workspace.more')}
                  items={[
                    { key: 'run', label: t('jiuwenmate.schedule.runNow'), icon: Play, onSelect: () => void onRun?.(plan.id) },
                    { key: 'history', label: t('jiuwenmate.schedule.history'), icon: History, onSelect: () => openHistory(plan.id) },
                    { key: 'edit', label: t('jiuwenmate.schedule.edit'), icon: PencilLine, onSelect: () => setEditing(plan) },
                    { key: 'delete', label: t('jiuwenmate.schedule.delete'), icon: Trash2, onSelect: () => void onDelete?.(plan.id) },
                  ]}
                />
                <button
                  type="button"
                  className="jiuwenmate-switch"
                  role="switch"
                  aria-checked={plan.enabled}
                  aria-label={plan.enabled ? t('jiuwenmate.schedule.pause') : t('jiuwenmate.schedule.resume')}
                  onClick={() => void onSetEnabled?.(plan.id, !plan.enabled)}
                >
                  <span className="jiuwenmate-switch__thumb" aria-hidden />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {editing !== undefined && (onCreate || onUpdate) ? (
        <ScheduleDialog
          key={editing?.id ?? 'new'}
          open
          plan={editing}
          executorId="local"
          saving={saving}
          onClose={() => setEditing(undefined)}
          onSubmit={(_executor, draft) => (editing ? onUpdate?.(editing.id, draft) : onCreate?.(draft))}
        />
      ) : null}
    </div>
  );
}

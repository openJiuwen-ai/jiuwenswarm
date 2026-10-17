import { ChevronDown, Search } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ActionMenu } from '../components/ActionMenu';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import type { JiuwenMateCategory } from '../types';
import { filterSchedules, scheduleSummary } from './state';
import { ScheduleDialog } from './ScheduleDialog';
import { ScheduleHistory } from './ScheduleHistory';
import type { Schedule, ScheduleDraft, ScheduleRun } from './types';

const STATUSES = ['', 'running', 'paused', 'failed'] as const;

type BoardColleague = {
  id: string;
  name: string;
  avatarUrl?: string;
  category?: JiuwenMateCategory;
};

export function ScheduleBoard({
  schedules,
  colleagues,
  saving,
  onCreate,
  onUpdate,
  onSetEnabled,
  onDelete,
  onLoadHistory,
}: {
  schedules: readonly Schedule[];
  colleagues: readonly BoardColleague[];
  saving?: boolean;
  onCreate: (agentId: string, draft: ScheduleDraft) => void | Promise<void>;
  onUpdate: (planId: string, draft: ScheduleDraft) => void | Promise<void>;
  onSetEnabled: (planId: string, enabled: boolean) => void | Promise<void>;
  onDelete: (planId: string) => void | Promise<void>;
  onLoadHistory: (planId: string) => Promise<{ plan: Schedule; runs: ScheduleRun[] }>;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [executor, setExecutor] = useState('');
  const [status, setStatus] = useState('');
  const [editing, setEditing] = useState<Schedule | null | undefined>(undefined);
  const [history, setHistory] = useState<{ plan: Schedule; runs: ScheduleRun[] } | null>(null);
  const visible = filterSchedules(schedules, query, executor, status);
  const count = (value: string) => schedules.filter((plan) => plan.lastStatus === value).length;
  const colleagueOf = (agentId: string) => colleagues.find((item) => item.id === agentId);
  const nameOf = (agentId: string) => colleagueOf(agentId)?.name ?? agentId;
  if (history) {
    return (
      <ScheduleHistory
        plan={history.plan}
        runs={history.runs}
        executorName={nameOf(history.plan.agentId)}
        onBack={() => setHistory(null)}
      />
    );
  }
  return (
    <section className="jiuwenmate-schedule-board" data-testid="jiuwenmate-schedule-board">
      <header className="jiuwenmate-schedule-board__header">
        <h2>{t('jiuwenmate.workspace.tabs.schedules')}</h2>
        <p>{t('jiuwenmate.schedule.boardHint')}</p>
      </header>
      <div className="jiuwenmate-schedule-board__tools">
        <div className="jiuwenmate-schedule-board__summary">
          <span>{t('jiuwenmate.schedule.taskList')}</span>
          {(['running', 'paused', 'failed'] as const).map((tone) => (
            <em key={tone} data-tone={tone}>
              <i aria-hidden />
              {t(`jiuwenmate.schedule.status.${tone}`)} {count(tone)}
            </em>
          ))}
        </div>
        <div className="jiuwenmate-schedule-board__filters">
          <select value={executor} aria-label={t('jiuwenmate.schedule.executor')} onChange={(event) => setExecutor(event.target.value)}>
            <option value="">{t('jiuwenmate.schedule.executor')}</option>
            {colleagues.map((item) => (
              <option key={item.id} value={item.id}>{item.name}</option>
            ))}
          </select>
          <select value={status} aria-label={t('jiuwenmate.schedule.runStatus')} onChange={(event) => setStatus(event.target.value)}>
            {STATUSES.map((item) => (
              <option key={item || 'all'} value={item}>{item ? t(`jiuwenmate.schedule.status.${item}`) : t('jiuwenmate.schedule.runStatus')}</option>
            ))}
          </select>
          <label className="jiuwenmate-schedule-board__search">
            <Search aria-hidden />
            <input value={query} placeholder={t('jiuwenmate.schedule.search')} aria-label={t('jiuwenmate.schedule.search')} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <button type="button" className="jiuwenmate-dark-button" onClick={() => setEditing(null)}>
            {t('jiuwenmate.schedule.createTitle')}
          </button>
        </div>
      </div>
      <div className="jiuwenmate-schedule-board__table">
        <table>
          <thead>
            <tr>
              <th>{t('jiuwenmate.schedule.name')}</th>
              <th>{t('jiuwenmate.schedule.executor')}</th>
              <th>{t('jiuwenmate.schedule.planned')}</th>
              <th>{t('jiuwenmate.schedule.runStatus')}</th>
              <th>{t('jiuwenmate.schedule.runCount')}</th>
              <th>{t('jiuwenmate.schedule.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 ? (
              <tr>
                <td className="jiuwenmate-schedule-board__empty" colSpan={6}>{t('jiuwenmate.workspace.noSchedules')}</td>
              </tr>
            ) : visible.map((plan) => {
              const colleague = colleagueOf(plan.agentId);
              return (
                <tr key={plan.id}>
                  <td>{plan.name}</td>
                  <td>
                    <span className="jiuwenmate-schedule-board__executor">
                      <ColleagueAvatar
                        size="small"
                        colleague={{
                          id: plan.agentId,
                          name: colleague?.name ?? plan.agentId,
                          category: colleague?.category ?? 'custom',
                          avatarUrl: colleague?.avatarUrl,
                        }}
                      />
                      {nameOf(plan.agentId)}
                    </span>
                  </td>
                  <td>{scheduleSummary(plan)}</td>
                  <td>
                    <span className="jiuwenmate-schedule-board__status" data-tone={plan.lastStatus}>
                      <i aria-hidden />
                      {t(`jiuwenmate.schedule.status.${plan.lastStatus}`, { defaultValue: plan.lastStatus })}
                    </span>
                  </td>
                  <td>{plan.runCount}</td>
                  <td>
                    <span className="jiuwenmate-schedule-board__actions">
                      <button type="button" className="jiuwenmate-link-button jiuwenmate-link-button--blue" onClick={() => void onSetEnabled(plan.id, !plan.enabled)}>
                        {plan.enabled ? t('jiuwenmate.schedule.pause') : t('jiuwenmate.schedule.resume')}
                      </button>
                      <button type="button" className="jiuwenmate-link-button jiuwenmate-link-button--blue" onClick={() => setEditing(plan)}>{t('jiuwenmate.schedule.edit')}</button>
                      <ActionMenu
                        label={t('jiuwenmate.schedule.more')}
                        align="end"
                        className="jiuwenmate-schedule-board__more"
                        trigger={(
                          <>
                            <span>{t('jiuwenmate.schedule.more')}</span>
                            <ChevronDown aria-hidden />
                          </>
                        )}
                        items={[
                          { key: 'delete', label: t('jiuwenmate.schedule.delete'), onSelect: () => void onDelete(plan.id) },
                          {
                            key: 'history',
                            label: t('jiuwenmate.schedule.history'),
                            onSelect: () => void onLoadHistory(plan.id).then(setHistory).catch(() => undefined),
                          },
                        ]}
                      />
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {editing !== undefined ? (
        <ScheduleDialog
          key={editing?.id ?? 'new'}
          open
          plan={editing}
          executorId={editing?.agentId ?? ''}
          executors={colleagues}
          saving={saving}
          onClose={() => setEditing(undefined)}
          onSubmit={(agentId, draft) => (editing ? onUpdate(editing.id, draft) : onCreate(agentId, draft))}
        />
      ) : null}
    </section>
  );
}

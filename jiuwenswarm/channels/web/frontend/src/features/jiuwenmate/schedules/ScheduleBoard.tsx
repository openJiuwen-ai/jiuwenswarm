import { ChevronDown, CircleAlert, CirclePlay, History, ListChecks, Search, Timer } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ActionMenu } from '../components/ActionMenu';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import type { JiuwenMateActivity, JiuwenMateCategory } from '../types';
import { filterSchedules, scheduleSummary } from './state';
import { ScheduleDialog } from './ScheduleDialog';
import { ScheduleHistory } from './ScheduleHistory';
import type { Schedule, ScheduleDraft, ScheduleRun } from './types';

const STATUSES = ['', 'running', 'paused', 'failed', 'completed'] as const;
const TIME_RANGES = ['7d', '30d', ''] as const;

type BoardColleague = {
  id: string;
  name: string;
  avatarUrl?: string;
  avatarHue?: number;
  category?: JiuwenMateCategory;
  isDefault?: boolean;
};

export function ScheduleBoard({
  schedules,
  colleagues,
  activity,
  saving,
  onCreate,
  onUpdate,
  onSetEnabled,
  onDelete,
  onLoadHistory,
}: {
  schedules: readonly Schedule[];
  colleagues: readonly BoardColleague[];
  activity: JiuwenMateActivity | null;
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
  const [timeRange, setTimeRange] = useState('7d');
  const [editing, setEditing] = useState<Schedule | null | undefined>(undefined);
  const [history, setHistory] = useState<{ plan: Schedule; runs: ScheduleRun[] } | null>(null);
  const visible = filterSchedules(schedules, query, executor, status);
  const metrics = taskMetrics(schedules, activity, executor, timeRange);
  const colleagueOf = (agentId: string) => colleagues.find((item) => item.id === agentId);
  const nameOf = (agentId: string) => colleagueOf(agentId)?.name ?? agentId;
  if (history) {
    const executor = colleagueOf(history.plan.agentId);
    return (
      <ScheduleHistory
        plan={history.plan}
        runs={history.runs}
        executor={{
          id: history.plan.agentId,
          name: executor?.name ?? nameOf(history.plan.agentId),
          avatarUrl: executor?.avatarUrl,
          avatarHue: executor?.avatarHue,
          category: executor?.category,
          isDefault: executor?.isDefault,
        }}
        onBack={() => setHistory(null)}
      />
    );
  }
  return (
    <section className="jiuwenmate-schedule-board" data-testid="jiuwenmate-schedule-board">
      <header className="jiuwenmate-schedule-board__header">
        <h2>{t('jiuwenmate.office.taskOverview')}</h2>
        <div className="jiuwenmate-schedule-board__filters">
          <label className="jiuwenmate-schedule-board__search">
            <Search aria-hidden />
            <input value={query} placeholder={t('jiuwenmate.workspace.searchKeyword')} aria-label={t('jiuwenmate.workspace.searchKeyword')} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <select value={executor} aria-label={t('jiuwenmate.schedule.executor')} onChange={(event) => setExecutor(event.target.value)}>
            <option value="">{t('jiuwenmate.office.taskFilters.colleague')} · {t('jiuwenmate.office.taskFilters.all')}</option>
            {colleagues.map((item) => (
              <option key={item.id} value={item.id}>{item.name}</option>
            ))}
          </select>
          <select value={status} aria-label={t('jiuwenmate.schedule.runStatus')} onChange={(event) => setStatus(event.target.value)}>
            {STATUSES.map((item) => (
              <option key={item || 'all'} value={item}>{item ? t(`jiuwenmate.schedule.status.${item}`) : t('jiuwenmate.schedule.allStatuses')}</option>
            ))}
          </select>
          <select value={timeRange} aria-label={t('jiuwenmate.schedule.timeRange')} onChange={(event) => setTimeRange(event.target.value)}>
            {TIME_RANGES.map((item) => (
              <option key={item || 'all'} value={item}>{t(`jiuwenmate.schedule.timeRanges.${item || 'all'}`)}</option>
            ))}
          </select>
        </div>
      </header>
      <div className="jiuwenmate-metric-band jiuwenmate-schedule-board__metrics" data-testid="jiuwenmate-schedule-metrics">
        <div>
          <strong>{metrics.totalTasks}<small> / {metrics.todayTasks}</small></strong>
          <span><ListChecks aria-hidden />{t('jiuwenmate.office.taskMetrics.total')}</span>
        </div>
        <div>
          <strong title={t('jiuwenmate.office.taskMetrics.nearTimeoutHint')}>
            {metrics.runningTasks}
            <small> / </small>
            <small className="jiuwenmate-schedule-board__near-timeout" data-testid="jiuwenmate-near-timeout-count">
              {metrics.nearTimeoutTasks}
            </small>
          </strong>
          <span><CirclePlay aria-hidden />{t('jiuwenmate.office.taskMetrics.running')}</span>
        </div>
        <div>
          <strong>{formatDuration(metrics.averageMinutes)}</strong>
          <span><Timer aria-hidden />{t('jiuwenmate.office.taskMetrics.duration')}</span>
          <TaskSparkline values={metrics.durationTrend} tone="blue" />
        </div>
        <div>
          <strong>{metrics.failureRate}%</strong>
          <span><CircleAlert aria-hidden />{t('jiuwenmate.office.taskMetrics.failure')}</span>
          <TaskSparkline values={metrics.failureTrend} tone="green" />
        </div>
      </div>
      <div className="jiuwenmate-schedule-board__list-head">
        <h3>{t('jiuwenmate.office.taskList')}</h3>
        <button type="button" className="jiuwenmate-dark-button" onClick={() => setEditing(null)}>
          {t('jiuwenmate.schedule.createTitle')}
        </button>
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
                <td className="jiuwenmate-schedule-board__empty" colSpan={6}>
                  {t(schedules.length === 0 ? 'jiuwenmate.workspace.noSchedules' : 'jiuwenmate.schedule.noMatchingSchedules')}
                </td>
              </tr>
            ) : visible.map((plan) => {
              const colleague = colleagueOf(plan.agentId);
              const nextExecution = plan.nextFireAt ?? (plan.scheduleKind === 'once' ? plan.runAt : null);
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
                          avatarHue: colleague?.avatarHue,
                          isDefault: colleague?.isDefault,
                        }}
                      />
                      {nameOf(plan.agentId)}
                    </span>
                  </td>
                  <td>
                    <span className="jiuwenmate-schedule-board__planned">
                      <strong>{scheduleSummary(plan)}</strong>
                      {nextExecution ? (
                        <small>{t('jiuwenmate.schedule.nextExecution')} {new Date(nextExecution).toLocaleString()}</small>
                      ) : null}
                    </span>
                  </td>
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
                          { key: 'history', label: t('jiuwenmate.schedule.history'), icon: History, onSelect: () => void onLoadHistory(plan.id).then(setHistory).catch(() => undefined) },
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

function taskMetrics(schedules: readonly Schedule[], activity: JiuwenMateActivity | null, executor: string, timeRange: string) {
  const members = activity?.members.filter((member) => !executor || member.agentId === executor) ?? [];
  const plans = schedules.filter((plan) => !executor || plan.agentId === executor);
  const today = activity?.generatedAt ? new Date(activity.generatedAt).toISOString().slice(0, 10) : new Date().toISOString().slice(0, 10);
  const localToday = dateKey(new Date());
  const dayCount = timeRange === '30d' ? 30 : timeRange === '7d' ? 7 : 0;
  const start = dayCount ? new Date(Date.parse(`${today}T00:00:00Z`) - (dayCount - 1) * 24 * 60 * 60 * 1000).toISOString().slice(0, 10) : '';
  const scheduleDays = members.flatMap((member) => member.kinds).filter((day) => !start || (day.date >= start && day.date <= today));
  const totalTasks = plans.length;
  const todayTasks = plans.filter((plan) => plan.createdAt && dateKey(new Date(plan.createdAt)) === localToday).length;
  const scheduleRunCountByDay = new Map<string, number>();
  scheduleDays.forEach((day) => scheduleRunCountByDay.set(day.date, (scheduleRunCountByDay.get(day.date) ?? 0) + day.schedule));
  const scheduleHistory = members.flatMap((member) => member.history)
    .filter((item) => item.kind === 'schedule' && (!start || (item.startedAt.slice(0, 10) >= start && item.startedAt.slice(0, 10) <= today)));
  const finishedScheduleRuns = scheduleHistory.filter((item) =>
    (item.status === 'completed' || item.status === 'failed') && item.updatedAt,
  );
  const averageMinutes = finishedScheduleRuns.length
    ? finishedScheduleRuns.reduce((sum, item) => sum + Math.max(0, Date.parse(item.updatedAt!) - Date.parse(item.startedAt)) / 60000, 0) / finishedScheduleRuns.length
    : 0;
  const runningScheduleRuns = scheduleHistory.filter((item) => item.status === 'dispatched' || item.status === 'running');
  const nearTimeoutTasks = averageMinutes > 0
    ? runningScheduleRuns.filter((item) => Date.now() - Date.parse(item.startedAt) >= averageMinutes * 2 * 60 * 1000).length
    : 0;
  const failures = scheduleHistory.filter((item) => item.status === 'failed').length;
  const scheduledRunsInRange = [...scheduleRunCountByDay.values()].reduce((sum, count) => sum + count, 0);
  const trendCount = dayCount || 30;
  const trendDates = Array.from({ length: trendCount }, (_, index) => {
    const offset = trendCount - index - 1;
    return new Date(Date.parse(`${today}T00:00:00Z`) - offset * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  });
  const durationTrend = trendDates.map((date) => {
    const dayRuns = finishedScheduleRuns.filter((item) => item.startedAt.slice(0, 10) === date);
    return dayRuns.length
      ? dayRuns.reduce((sum, item) => sum + Math.max(0, Date.parse(item.updatedAt!) - Date.parse(item.startedAt)) / 60000, 0) / dayRuns.length
      : 0;
  });
  const failureTrend = trendDates.map((date) => {
    const dayFailures = scheduleHistory.filter((item) => item.status === 'failed' && item.startedAt.slice(0, 10) === date).length;
    const dayTasks = scheduleRunCountByDay.get(date) ?? 0;
    return dayTasks ? dayFailures / dayTasks * 100 : 0;
  });
  return {
    totalTasks,
    todayTasks,
    runningTasks: plans.filter((plan) => plan.lastStatus === 'running').length,
    nearTimeoutTasks,
    averageMinutes: Math.round(averageMinutes),
    failureRate: scheduledRunsInRange ? Math.round(failures / scheduledRunsInRange * 100) : 0,
    durationTrend,
    failureTrend,
  };
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes}min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}min` : `${hours}h`;
}

function dateKey(date: Date): string {
  if (Number.isNaN(date.getTime())) return '';
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

function TaskSparkline({ values, tone }: { values: number[]; tone: 'blue' | 'green' }) {
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const points = values.map((value, index) => {
    const x = values.length > 1 ? index * (100 / (values.length - 1)) : 50;
    const y = 30 - ((value - min) / range) * 24;
    return `${x},${y}`;
  });
  const line = `M ${points.join(' L ')}`;
  const area = `${line} L 100,36 L 0,36 Z`;
  return (
    <svg className="jiuwenmate-sparkline" data-tone={tone} viewBox="0 0 100 36" preserveAspectRatio="none" aria-hidden="true">
      <path className="jiuwenmate-sparkline__area" d={area} />
      <path className="jiuwenmate-sparkline__line" d={line} />
    </svg>
  );
}

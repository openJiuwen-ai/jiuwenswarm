import { useTranslation } from 'react-i18next';
import { ChevronLeft } from 'lucide-react';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import type { JiuwenMateCategory } from '../types';
import { scheduleSummary, toZonedInput } from './state';
import type { Schedule, ScheduleRun } from './types';

function runStatus(status: string): 'success' | 'failed' | 'stopped' | 'running' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'failed';
  if (status === 'stopped' || status === 'cancelled') return 'stopped';
  return 'running';
}

type HistoryExecutor = {
  id: string;
  name: string;
  avatarUrl?: string;
  avatarHue?: number;
  category?: JiuwenMateCategory;
  isDefault?: boolean;
};

export function ScheduleHistory({
  plan,
  runs,
  executor,
  onBack,
}: {
  plan: Schedule;
  runs: readonly ScheduleRun[];
  executor?: HistoryExecutor;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  return (
    <section className="jiuwenmate-schedule-history" data-testid="jiuwenmate-schedule-history">
      <button type="button" className="jiuwenmate-schedule-history__back" onClick={onBack}>
        <ChevronLeft aria-hidden />
        {t('jiuwenmate.schedule.back')}
      </button>
      <h3>{t('jiuwenmate.schedule.basic')}</h3>
      <dl className="jiuwenmate-schedule-history__facts">
        <div>
          <dt>{t('jiuwenmate.schedule.name')}</dt>
          <dd>{plan.name}</dd>
        </div>
        <div>
          <dt>{t('jiuwenmate.schedule.prompt')}</dt>
          <dd>{plan.prompt}</dd>
        </div>
        <div>
          <dt>{t('jiuwenmate.schedule.plan')}</dt>
          <dd>{scheduleSummary(plan)}</dd>
        </div>
        {executor ? (
          <div className="jiuwenmate-schedule-history__executor">
            <dt>{t('jiuwenmate.schedule.historyExecutor')}</dt>
            <dd>
              <ColleagueAvatar
                size="small"
                colleague={{
                  id: executor.id,
                  name: executor.name,
                  category: executor.category ?? 'custom',
                  avatarUrl: executor.avatarUrl,
                  avatarHue: executor.avatarHue,
                  isDefault: executor.isDefault,
                }}
              />
              {executor.name}
            </dd>
          </div>
        ) : null}
      </dl>
      <h3>{t('jiuwenmate.schedule.historyHeading', { count: runs.length })}</h3>
      <div className="jiuwenmate-schedule-history__table">
        <table>
          <thead>
            <tr>
              <th>{t('jiuwenmate.schedule.runAt')}</th>
              <th>{t('jiuwenmate.schedule.runStatus')}</th>
              <th>{t('jiuwenmate.schedule.description')}</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => {
              const tone = runStatus(run.status);
              const detail = run.description.trim() || t(`jiuwenmate.schedule.runDetail.${tone}`);
              return (
                <tr key={run.requestId}>
                  <td>{toZonedInput(run.scheduledFor, plan.timezone || 'Asia/Shanghai').replace('T', ' ')}</td>
                  <td>
                    <span className="jiuwenmate-schedule-history__status" data-tone={tone}>
                      <i aria-hidden />
                      {t(`jiuwenmate.schedule.run.${tone}`)}
                    </span>
                  </td>
                  <td>{detail}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

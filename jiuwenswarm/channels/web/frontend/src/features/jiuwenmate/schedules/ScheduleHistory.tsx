import { useTranslation } from 'react-i18next';
import { ChevronLeft } from 'lucide-react';
import { scheduleSummary, toZonedInput } from './state';
import type { Schedule, ScheduleRun } from './types';

function runStatus(status: string): 'success' | 'failed' | 'stopped' | 'running' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'failed';
  if (status === 'stopped' || status === 'cancelled') return 'stopped';
  return 'running';
}

export function ScheduleHistory({
  plan,
  runs,
  executorName,
  onBack,
}: {
  plan: Schedule;
  runs: readonly ScheduleRun[];
  executorName?: string;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  return (
    <section className="jiuwenmate-schedule-history" data-testid="jiuwenmate-schedule-history">
      <button type="button" className="jiuwenmate-link-button" onClick={onBack}>
        <ChevronLeft aria-hidden />
        {t('jiuwenmate.schedule.historyTitle', { count: runs.length })}
      </button>
      <h3>{t('jiuwenmate.schedule.basic')}</h3>
      <dl>
        <dt>{t('jiuwenmate.schedule.name')}</dt>
        <dd>{plan.name}</dd>
        <dt>{t('jiuwenmate.schedule.prompt')}</dt>
        <dd>{plan.prompt}</dd>
        <dt>{t('jiuwenmate.schedule.plan')}</dt>
        <dd>{scheduleSummary(plan)}</dd>
        {executorName ? (
          <>
            <dt>{t('jiuwenmate.schedule.executor')}</dt>
            <dd>{executorName}</dd>
          </>
        ) : null}
      </dl>
      <h3>{t('jiuwenmate.schedule.historyHeading', { count: runs.length })}</h3>
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
            return (
              <tr key={run.requestId}>
                <td>{toZonedInput(run.scheduledFor, plan.timezone || 'Asia/Shanghai').replace('T', ' ')}</td>
                <td data-tone={tone}>{t(`jiuwenmate.schedule.run.${tone}`)}</td>
                <td>{t(`jiuwenmate.schedule.runDetail.${tone}`)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

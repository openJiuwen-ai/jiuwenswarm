/** Isolated Goal assessor requests; ownership stays with the executing agent. */

import { useState } from 'react';
import type { TrajectoryKey, TrajectoryTranslate } from './client/i18n';
import { TrajectoryTable } from './client/TrajectoryTable';
import type { TrajectoryGoalAssessment } from './trajectory/model';
import css from './GoalAssessments.module.css';

function Assessment({ assessment, t }: { assessment: TrajectoryGoalAssessment; t: TrajectoryTranslate }) {
  const [open, setOpen] = useState(false);
  const [collapsedTurns, setCollapsedTurns] = useState<ReadonlySet<number>>(new Set());
  const [collapsedAssistants, setCollapsedAssistants] = useState<ReadonlySet<string>>(new Set());
  const request = assessment.request;
  const status = request.status === 'error' ? 'error' : assessment.result ?? (request.status === 'complete' ? 'unknown' : 'running');
  const duration = request.completedAt != null && request.startedAt != null
    ? Math.max(0, request.completedAt - request.startedAt) : undefined;
  return (
    <details className={css.assessment} onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>
        <span>{t('goalAssessment.title').replace('{{attempt}}', String(assessment.attempt ?? '—'))}</span>
        <span>{assessment.strategy ?? '—'} · {t(`goalAssessment.status.${status}` as TrajectoryKey)}</span>
        <span>{request.usage?.total ?? '—'} tokens{duration === undefined ? '' : ` · ${duration.toFixed(0)} ms`}</span>
      </summary>
      {open && <div className={css.body}>
        <p className={css.note}>{t('goalAssessment.isolated')} · Goal {assessment.goalId ?? '—'}</p>
        <TrajectoryTable
          turns={assessment.turns}
          requestNumbers={[request]}
          collapsedTurns={collapsedTurns}
          onToggleTurn={turn => setCollapsedTurns(current => current.has(turn) ? new Set() : new Set([turn]))}
          collapsedAssistants={collapsedAssistants}
          onToggleAssistant={id => setCollapsedAssistants(current => current.has(id) ? new Set() : new Set([id]))}
        />
      </div>}
    </details>
  );
}

export function GoalAssessments({ assessments, t }: { assessments: readonly TrajectoryGoalAssessment[]; t: TrajectoryTranslate }) {
  const total = assessments.every(item => item.request.usage?.total != null)
    ? assessments.reduce((sum, item) => sum + item.request.usage!.total!, 0)
    : '—';
  return (
    <section className={css.root} aria-label={t('goalAssessment.label')}>
      <div className={css.heading}>{t('goalAssessment.summary').replace('{{count}}', String(assessments.length)).replace('{{tokens}}', String(total))}</div>
      {assessments.map(assessment => <Assessment key={assessment.request.recordId ?? assessment.id} assessment={assessment} t={t} />)}
    </section>
  );
}

/** Overall account without a section title, plus collapsed agent accounts. Evidence stays in the JSON download. */

import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { DiagnosisRecord, SummaryStatus } from './diagnosisTypes';
import { FoldableItem } from './DiagnosisFindings';
import { DiagnosisStatusIcon } from './DiagnosisStatusIcon';
import { ClampedMarkdown, WholeMarkdown } from './DiagnosisText';
import { readableSummary } from './diagnosisView';
import css from './DiagnosisSubpage.module.css';

export function DiagnosisSummaryTree({ record, subjectLabels }: {
  record: DiagnosisRecord;
  subjectLabels: ReadonlyMap<string, string>;
}) {
  const { t } = useTranslation();
  const tree = record.summary_tree;
  const root = tree?.display_summary?.trim() || tree?.trajectory_summary || record.trajectory_summary || record.task_description || '';
  if (!tree && !root) return null;
  const segments = Object.values(tree?.turns ?? {}).flatMap(turns => turns.flatMap(turn => turn.segments));
  const completed = segments.filter(segment => segment.status === 'completed').length;
  const status = (value: SummaryStatus) => t(`trajectory.diagnosis.stageStatus.${value}`);
  const notice = tree ? [
    tree.status !== 'completed' ? status(tree.status) : '',
    completed < segments.length ? t('trajectory.diagnosis.summaryTree.coverage',
      { completed, total: segments.length }) : '',
    tree.warnings.length ? t('trajectory.diagnosis.summaryTree.warnings', { count: tree.warnings.length }) : '',
  ].filter(Boolean).join(' · ') : '';
  const agents = Object.entries(tree?.agent_summaries ?? {});
  const labels = new Map(subjectLabels);
  for (const [id, agent] of agents) labels.set(id, agent.display_name || labels.get(id) || id.split(':').pop() || id);
  const missing = t('trajectory.diagnosis.summaryTree.unavailable');
  const unmerged = agents.length > 0 && /^(归并未完成|Merge incomplete)/.test(root.trim());
  const visibleRoot = unmerged ? t('trajectory.diagnosis.summaryTree.unmerged') : readableSummary(root, labels);
  const download = () => {
    const blob = new Blob([JSON.stringify({ diagnosis_id: record.diagnosis_id, session_id: record.session_id,
      scope: record.input.scope, summary_tree: tree ?? { trajectory_summary: root } }, null, 2)],
    { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${record.diagnosis_id}.summary.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <FoldableItem title={t('trajectory.diagnosis.objectSummary')} className={css.summaryBlock}
      mark={tree && tree.status !== 'completed' ? <DiagnosisStatusIcon kind={tree.status} /> : undefined}
      action={(
        <button type="button" className={css.linkButton} onClick={download}
          data-testid="trajectory-diagnosis-summary-download">
          {t('trajectory.diagnosis.summaryTree.download')}
        </button>
      )}
      testId="trajectory-diagnosis-task" toggleTestId="trajectory-diagnosis-summary-toggle">
      <div className={css.summaryScroll} tabIndex={0} role="region" aria-label={t('trajectory.diagnosis.objectSummary')}
        data-testid="trajectory-diagnosis-summary-content">
        <section className={`${css.itemBody} ${css.summaryOverview}`} data-testid="trajectory-diagnosis-summary-root">
          {notice ? <span className={css.summaryMeta} data-testid="trajectory-diagnosis-summary-status">
            {notice}
          </span> : null}
          <ClampedMarkdown text={visibleRoot || missing} />
        </section>
        {agents.length > 0 ? (
          <details className={css.summaryAgentsFold} data-testid="trajectory-diagnosis-summary-agents">
            <summary className={css.summaryAgentsFoldHead} data-testid="trajectory-diagnosis-summary-agents-toggle">
              <span className={css.summaryHeading} data-testid="trajectory-diagnosis-summary-agents-hint">
                {t('trajectory.diagnosis.summaryTree.agentsHint')}
              </span>
              <ChevronRight className={css.summaryAgentChevron} size={16} aria-hidden="true" />
            </summary>
            <div className={css.summaryAgents}>
              {agents.map(([agentId, agent], index) => (
                <details key={`${record.diagnosis_id}:${agentId}`} className={css.summaryAgent}
                  data-testid="trajectory-diagnosis-summary-agent"
                  data-variant={agentId}>
                  <summary className={css.summaryAgentHead} data-testid="trajectory-diagnosis-summary-agent-toggle"
                    data-variant={agentId}>
                    <span className={css.summaryAgentNumber} aria-hidden="true">{index + 1}</span>
                    <h5 className={css.summaryHeading}>{labels.get(agentId)}</h5>
                    {agent.status !== 'completed'
                      ? <span className={css.summaryMeta}>{status(agent.status)}</span>
                      : null}
                    <ChevronRight className={css.summaryAgentChevron} size={16} aria-hidden="true" />
                  </summary>
                  <div className={css.summaryAgentBody} data-testid="trajectory-diagnosis-summary-agent-content"
                    data-variant={agentId}>
                    <WholeMarkdown text={/^(归并未完成|Merge incomplete)/.test(agent.summary.trim())
                      ? t('trajectory.diagnosis.summaryTree.agentUnmerged')
                      : readableSummary(agent.summary, labels) || missing} />
                  </div>
                </details>
              ))}
            </div>
          </details>
        ) : null}
      </div>
    </FoldableItem>
  );
}

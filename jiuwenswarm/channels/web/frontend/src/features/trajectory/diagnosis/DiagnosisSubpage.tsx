/** Diagnosis sub-page: what was diagnosed, how the run went, and what to do next. */

import { memo, useCallback, useMemo, useState, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { isRunning, ttseRootCauses } from './diagnosisState';
import {
  asRewardPayload,
  type DiagnosisRecord,
  type RootCause,
} from './diagnosisTypes';
import {
  diagnosisTarget,
  formatElapsed,
  formatTimeWindow,
  isStageExpanded,
  looksLikePath,
  processSummary,
  splitStageOutput,
  stageViews,
  suggestionsState,
  type StageView,
  type StageViewStatus,
} from './diagnosisView';
import { FillButton } from './DiagnosisFillButton';
import { RewardSummary } from './DiagnosisReward';
import { RootCauses } from './DiagnosisRootCauses';
import { DiagnosisStatusIcon } from './DiagnosisStatusIcon';
import { ClampedMarkdown, PlainFold } from './DiagnosisText';
import type { DiagnosisSession } from './useDiagnosisSession';
import css from './DiagnosisSubpage.module.css';

export interface DiagnosisSubpageProps {
  session: DiagnosisSession;
  /** Display names of the session's agents by subject id. */
  subjectLabels: ReadonlyMap<string, string>;
  /** Root cause currently emphasised on the trajectory lanes. */
  focusedCauseId: string | null;
  /** Emphasise a root cause on the trajectory, or `null` to stop. */
  onFocusCause: (cause: RootCause | null) => void;
}

export const DiagnosisSubpage = memo(function DiagnosisSubpage({
  session,
  subjectLabels,
  focusedCauseId,
  onFocusCause,
}: DiagnosisSubpageProps) {
  const { t } = useTranslation();
  const { record, notice, summaries } = session;

  return (
    <div className={css.root} data-testid="trajectory-diagnosis-subpage">
      {notice === null ? null : (
        <p
          className={`${css.notice} ${notice.kind === 'error' ? css.noticeError : ''}`}
          role={notice.kind === 'error' ? 'alert' : 'status'}
          data-testid="trajectory-diagnosis-notice"
        >
          <span>{notice.text}</span>
          <button
            type="button"
            className={css.noticeClose}
            onClick={session.dismissNotice}
            aria-label={t('trajectory.diagnosis.dismiss')}
          >
            ×
          </button>
        </p>
      )}
      <div className={css.body}>
        {record === null ? (
          <p className={css.empty} data-testid="trajectory-diagnosis-empty">
            {t(summaries.length === 0 ? 'trajectory.diagnosis.emptyHint' : 'trajectory.diagnosis.loadingRecord')}
          </p>
        ) : (
          <RecordCards
            key={record.diagnosis_id}
            record={record}
            session={session}
            subjectLabels={subjectLabels}
            focusedCauseId={focusedCauseId}
            onFocusCause={onFocusCause}
          />
        )}
      </div>
    </div>
  );
});

interface RecordCardsProps {
  record: DiagnosisRecord;
  session: DiagnosisSession;
  subjectLabels: ReadonlyMap<string, string>;
  focusedCauseId: string | null;
  onFocusCause: (cause: RootCause | null) => void;
}

function Card({ title, action, testId, children }: {
  title: string;
  action?: ReactNode;
  testId: string;
  children: ReactNode;
}) {
  return (
    <section className={css.card} data-testid={testId}>
      <header className={css.cardHead}>
        <h3 className={css.cardTitle}>{title}</h3>
        {action}
      </header>
      {children}
    </section>
  );
}

function RecordCards({ record, session, subjectLabels, focusedCauseId, onFocusCause }: RecordCardsProps) {
  const { t } = useTranslation();
  const running = isRunning(record);
  const done = !running;
  const target = useMemo(() => diagnosisTarget(record), [record]);
  const labelOf = (subjectId: string) => subjectLabels.get(subjectId) ?? subjectId;
  const note = record.input.user_note?.trim() ?? '';
  const taskDescription = record.task_description?.trim() ?? '';
  const { suggestions } = record;
  const reward = asRewardPayload(record.reward);
  const ttseCauses = ttseRootCauses(record);
  const outlets = suggestionsState(record.status);

  return (
    <>
      <Card title={t('trajectory.diagnosis.object')} testId="trajectory-diagnosis-object">
        {target.full ? (
          <>
            <p className={css.line}>
              <span className={css.lineLabel}>{t('trajectory.diagnosis.objectTime')}</span>
              {t('trajectory.diagnosis.objectFull')}
            </p>
            {subjectLabels.size === 0 ? null : (
              <AgentLine labels={[...subjectLabels.values()]} />
            )}
          </>
        ) : (
          target.groups.map(group => (
            <div className={css.targetGroup} key={`${group.startMs}-${group.endMs}-${group.subjectIds.join(',')}`}>
              <p className={css.line}>
                <span className={css.lineLabel}>{t('trajectory.diagnosis.objectTime')}</span>
                {formatTimeWindow(group.startMs, group.endMs)}
              </p>
              <AgentLine labels={group.subjectIds.map(labelOf)} />
            </div>
          ))
        )}
        {record.input.resolved_span_count > 0 ? (
          <p className={css.line} data-testid="trajectory-diagnosis-object-spans">
            <span className={css.lineLabel}>{t('trajectory.diagnosis.objectSteps')}</span>
            {t('trajectory.diagnosis.objectStepsCount', { count: record.input.resolved_span_count })}
          </p>
        ) : null}
        {record.input.excluded_running.length > 0 ? (
          <p className={css.hint} data-testid="trajectory-diagnosis-object-excluded">
            {t('trajectory.diagnosis.objectExcludedRunning', { count: record.input.excluded_running.length })}
          </p>
        ) : null}
        {taskDescription === '' ? null : (
          <div className={css.taskBox} data-testid="trajectory-diagnosis-task">
            <span className={css.lineLabel}>{t('trajectory.diagnosis.objectTask')}</span>
            <PlainFold text={taskDescription} />
          </div>
        )}
      </Card>

      {note === '' ? null : (
        <Card title={t('trajectory.diagnosis.note')} testId="trajectory-diagnosis-note-card">
          <p className={css.noteText}>{note}</p>
        </Card>
      )}

      <Card
        title={t('trajectory.diagnosis.details')}
        testId="trajectory-diagnosis-record"
        action={running ? (
          <button
            type="button"
            className={css.button}
            onClick={() => { void session.cancel(); }}
            data-testid="trajectory-diagnosis-cancel"
          >
            {t('trajectory.diagnosis.cancel')}
          </button>
        ) : null}
      >
        {record.error === undefined ? null : (
          <p className={`${css.hint} ${css.errorText}`} role="alert">{record.error.message}</p>
        )}
        <ProcessSection record={record} running={running} />
        {record.headline === undefined || record.headline === '' ? null : (
          <p className={css.headline} data-testid="trajectory-diagnosis-headline">{record.headline}</p>
        )}
        {done && reward !== null ? <RewardSummary reward={reward} /> : null}
        {/* Findings appear as the attribution reports them, not only once the run is over. */}
        <RootCauses
          record={record}
          subjectLabels={subjectLabels}
          focusedCauseId={focusedCauseId}
          onFocusCause={onFocusCause}
        />
      </Card>

      <Card title={t('trajectory.diagnosis.suggestions')} testId="trajectory-diagnosis-suggestions">
        {outlets === 'pending' ? (
          <p className={css.hint}>{t('trajectory.diagnosis.outletsPending')}</p>
        ) : outlets === 'unfinished' ? (
          <p className={css.hint} data-testid="trajectory-diagnosis-unfinished">
            {t('trajectory.diagnosis.suggestionsUnfinished')}
          </p>
        ) : (
          <>
            {suggestions.artifact_fix === undefined ? null : (
              <SuggestionCard
                tone="artifact"
                kind={t('trajectory.diagnosis.artifactFix')}
                testId="trajectory-diagnosis-artifact-fix"
                action={(
                  <FillButton
                    text={suggestions.artifact_fix.instruction}
                    onFill={session.fillComposer}
                    testId="trajectory-diagnosis-artifact-fix-fill"
                  />
                )}
              >
                <ClampedMarkdown text={suggestions.artifact_fix.instruction} />
              </SuggestionCard>
            )}
            {suggestions.skills.map(skill => (
              <SuggestionCard
                key={skill.id}
                tone="skill"
                kind={t('trajectory.diagnosis.skills')}
                title={skill.skill_name}
                mono={looksLikePath(skill.skill_name)}
                testId="trajectory-diagnosis-skill"
                action={skill.selectable ? (
                  <FillButton
                    text={`${t('trajectory.diagnosis.skillFillPrefix')}\n\n${skill.message}`}
                    onFill={session.fillComposer}
                    testId="trajectory-diagnosis-skill-fill"
                  />
                ) : (
                  <span className={css.hint}>{t('trajectory.diagnosis.blocked', { reason: skill.blocked_reason ?? '' })}</span>
                )}
              >
                <ClampedMarkdown text={skill.change_summary} />
                {skill.rationale === '' || skill.rationale === skill.change_summary ? null : (
                  <p className={css.hint}>{skill.rationale}</p>
                )}
              </SuggestionCard>
            ))}
            {ttseCauses.map((cause) => {
              const triggered = record.ttse_trigger[cause.id];
              const triggering = session.triggeringTtse.has(cause.id);
              return (
                <SuggestionCard
                  key={cause.id}
                  tone="experience"
                  kind={t('trajectory.diagnosis.ttse')}
                  title={t('trajectory.diagnosis.ttseReady')}
                  testId="trajectory-diagnosis-ttse"
                  action={(
                    <span className={css.row}>
                      {triggered === undefined ? null : (
                        <span className={css.hint} data-testid="trajectory-diagnosis-ttse-result">
                          {t(`trajectory.diagnosis.ttseResult.${triggered.result}`)}
                        </span>
                      )}
                      <button
                        type="button"
                        className={css.actionButton}
                        disabled={triggering || (triggered !== undefined && triggered.result !== 'failed')}
                        onClick={() => { void session.triggerTtse(cause.id); }}
                        data-testid="trajectory-diagnosis-ttse-trigger"
                      >
                        {triggering ? t('trajectory.diagnosis.triggeringTtse') : t('trajectory.diagnosis.triggerTtse')}
                      </button>
                    </span>
                  )}
                >
                  <p className={css.hint}>{t('trajectory.diagnosis.ttseHint')}</p>
                </SuggestionCard>
              );
            })}
            {suggestions.artifact_fix === undefined && suggestions.skills.length === 0 && ttseCauses.length === 0 ? (
              <p className={css.hint}>{t('trajectory.diagnosis.noSuggestions')}</p>
            ) : null}
          </>
        )}
      </Card>
    </>
  );
}

/** One suggestion: what kind it is and what it is about on top, the action beside them, the detail below. */
function SuggestionCard({ tone, kind, title, mono = false, action, testId, children }: {
  tone: 'artifact' | 'skill' | 'experience';
  kind: string;
  title?: string;
  mono?: boolean;
  action: ReactNode;
  testId: string;
  children: ReactNode;
}) {
  return (
    <div className={`${css.suggestion} ${css[`tone_${tone}`]}`} data-testid={testId}>
      <div className={css.suggestionHead}>
        <span className={css.suggestionKind}>{kind}</span>
        {title === undefined ? null : (
          <strong className={`${css.suggestionTitle} ${mono ? css.mono : ''}`}>{title}</strong>
        )}
        <span className={css.spacer} />
        {action}
      </div>
      {children}
    </div>
  );
}

function AgentLine({ labels }: { labels: readonly string[] }) {
  const { t } = useTranslation();
  return (
    <p className={css.line}>
      <span className={css.lineLabel}>{t('trajectory.diagnosis.objectAgents')}</span>
      <span className={css.chips}>
        {labels.map(label => (
          <span className={css.chip} key={label} data-testid="trajectory-diagnosis-object-agent">{label}</span>
        ))}
      </span>
    </p>
  );
}

/**
 * How the diagnosis ran. Open to its stage list from the start and after it ends; while it runs every
 * stage shows its detail, and afterwards each folds to its summary line.
 */
function ProcessSection({ record, running }: { record: DiagnosisRecord; running: boolean }) {
  const { t } = useTranslation();
  const [choice, setChoice] = useState<boolean | undefined>(undefined);
  const summary = useMemo(() => processSummary(record.stages), [record.stages]);
  // A starting run has no stage yet, but its waiting placeholders are worth showing.
  if (record.stages.length === 0 && !running) return null;
  const open = choice ?? true;
  const meta = running
    ? ''
    : [t('trajectory.diagnosis.processSteps', { count: summary.count }), summary.elapsed]
      .filter(item => item !== '')
      .join(' · ');
  return (
    <div className={css.process} data-testid="trajectory-diagnosis-process" data-expanded={open ? 'true' : 'false'}>
      <button
        type="button"
        className={`${css.stageHead} ${css.stageToggle}`}
        aria-expanded={open}
        onClick={() => setChoice(!open)}
        data-testid="trajectory-diagnosis-process-toggle"
      >
        <ChevronRight className={`${css.chevron} ${open ? css.chevronOpen : ''}`} size={14} strokeWidth={2} aria-hidden />
        <span className={css.stageTitle}>{t('trajectory.diagnosis.process')}</span>
        <span className={css.stageSummary}>{meta}</span>
      </button>
      {open ? <StageList record={record} running={running} /> : null}
    </div>
  );
}

function StageList({ record, running }: { record: DiagnosisRecord; running: boolean }) {
  const views = useMemo(() => stageViews(record.stages, running), [record.stages, running]);
  // A choice only holds for the status it was made in: a stage that moves on re-applies the default.
  const [choices, setChoices] = useState<Record<string, { status: StageViewStatus; open: boolean }>>({});
  const toggle = useCallback((view: StageView, open: boolean) => {
    setChoices(current => ({ ...current, [view.stageId]: { status: view.status, open } }));
  }, []);
  if (views.length === 0) return null;
  return (
    <ol className={css.stages} data-testid="trajectory-diagnosis-stages">
      {views.map((view) => {
        const choice = choices[view.stageId];
        const userChoice = choice !== undefined && choice.status === view.status ? choice.open : undefined;
        return (
          <StageItem
            key={view.stageId}
            view={view}
            expanded={isStageExpanded(view.status, userChoice, running)}
            onToggle={toggle}
          />
        );
      })}
    </ol>
  );
}

function StageItem({ view, expanded, onToggle }: {
  view: StageView;
  expanded: boolean;
  onToggle: (view: StageView, open: boolean) => void;
}) {
  const { t } = useTranslation();
  const { stage } = view;
  const title = stage !== null && stage.title !== ''
    ? stage.title
    : t(`trajectory.diagnosis.plannedStage.${view.stageId}`, { defaultValue: view.stageId });
  // The summary lives in the head; the body holds what is only worth reading on demand.
  const hasBody = stage !== null && ((stage.skip_reason ?? '') !== '' || stage.output !== '');
  const iconKind = view.status;
  const bodyId = `trajectory-diagnosis-stage-body-${view.stageId}`;
  const output = useMemo(() => splitStageOutput(stage?.output ?? ''), [stage?.output]);
  // The one-line conclusion and the time it took stay visible while the stage is folded.
  const conclusion = stage === null || view.status === 'pending' ? '' : (stage.summary ?? '');
  const elapsed = stage === null || view.status === 'running' ? '' : formatElapsed(stage.elapsed_ms);
  const headMeta = (
    <>
      {conclusion === '' ? null : (
        <span className={css.stageSummary} data-testid="trajectory-diagnosis-stage-summary">{conclusion}</span>
      )}
      {elapsed === '' ? null : (
        <span className={css.stageElapsed} data-testid="trajectory-diagnosis-stage-elapsed">{elapsed}</span>
      )}
    </>
  );
  return (
    <li
      className={`${css.stage} ${view.status === 'pending' ? css.stagePending : ''}`}
      data-testid="trajectory-diagnosis-stage"
      data-stage-id={view.stageId}
      data-stage-status={view.status}
      data-expanded={expanded ? 'true' : 'false'}
    >
      {view.status === 'pending' || !hasBody ? (
        <div className={css.stageHead}>
          <DiagnosisStatusIcon kind={iconKind} />
          <span className={css.stageTitle}>{title}</span>
          <span className={css.srOnly}>{t(`trajectory.diagnosis.stageStatus.${view.status}`)}</span>
          {headMeta}
        </div>
      ) : (
        <button
          type="button"
          className={`${css.stageHead} ${css.stageToggle}`}
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => onToggle(view, !expanded)}
          data-testid="trajectory-diagnosis-stage-toggle"
        >
          <DiagnosisStatusIcon kind={iconKind} />
          <span className={css.stageTitle}>{title}</span>
          <span className={css.srOnly}>{t(`trajectory.diagnosis.stageStatus.${view.status}`)}</span>
          {headMeta}
          <ChevronRight className={`${css.chevron} ${expanded ? css.chevronOpen : ''}`} size={14} strokeWidth={2} aria-hidden />
        </button>
      )}
      {expanded && stage !== null && hasBody ? (
        <div className={css.stageBody} id={bodyId}>
          {stage.skip_reason === undefined ? null : <p className={css.hint}>{stage.skip_reason}</p>}
          {output.body === '' ? null : <pre className={css.output}>{output.body}</pre>}
          {output.requestCount === 0 ? null : (
            <details className={css.requestFold}>
              <summary data-testid="trajectory-diagnosis-stage-requests">
                {t('trajectory.diagnosis.requestCount', { count: output.requestCount })}
              </summary>
              <pre className={css.output}>{output.requestLines.join('\n')}</pre>
            </details>
          )}
        </div>
      ) : null}
    </li>
  );
}

/** Diagnosis sub-page: what was diagnosed, how the run went, and what to do next. */

import { memo, useCallback, useMemo, useState, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { isRunning, ttseRootCauses } from './diagnosisState';
import {
  asRewardPayload,
  type DiagnosisRecord,
  type RewardPayload,
  type RootCause,
} from './diagnosisTypes';
import {
  diagnosisTarget,
  formatTimeWindow,
  isStageExpanded,
  stageViews,
  type StageView,
  type StageViewStatus,
} from './diagnosisView';
import { DiagnosisRecordSelect } from './DiagnosisRecordSelect';
import { DiagnosisStatusIcon } from './DiagnosisStatusIcon';
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
      <div className={css.toolbar}>
        <DiagnosisRecordSelect
          items={summaries}
          currentId={record?.diagnosis_id ?? null}
          onSelect={(id) => { void session.open(id); }}
          onDelete={(id) => { void session.remove(id); }}
        />
      </div>
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
  const { suggestions } = record;
  const reward = asRewardPayload(record.reward);
  const ttseCauses = ttseRootCauses(record);

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
        {record.headline === undefined || record.headline === '' ? null : (
          <p className={css.hint}>{record.headline}</p>
        )}
        {record.error === undefined ? null : (
          <p className={`${css.hint} ${css.errorText}`} role="alert">{record.error.message}</p>
        )}
        <StageList record={record} running={running} />
        {done ? (
          <RootCauses
            record={record}
            focusedCauseId={focusedCauseId}
            onFocusCause={onFocusCause}
          />
        ) : null}
        {done && reward !== null ? <RewardSection reward={reward} /> : null}
      </Card>

      <Card title={t('trajectory.diagnosis.suggestions')} testId="trajectory-diagnosis-suggestions">
        {!done ? (
          <p className={css.hint}>{t('trajectory.diagnosis.outletsPending')}</p>
        ) : (
          <>
            {suggestions.artifact_fix === undefined ? null : (
              <div className={css.suggestion} data-testid="trajectory-diagnosis-artifact-fix">
                <strong>{t('trajectory.diagnosis.artifactFix')}</strong>
                <p className={css.suggestionText}>{suggestions.artifact_fix.instruction}</p>
                <div className={css.row}>
                  <button
                    type="button"
                    className={css.button}
                    onClick={() => session.fillComposer(suggestions.artifact_fix?.instruction ?? '')}
                    data-testid="trajectory-diagnosis-artifact-fix-fill"
                  >
                    {t('trajectory.diagnosis.fillInput')}
                  </button>
                </div>
              </div>
            )}
            {suggestions.skills.map(skill => (
              <div className={css.suggestion} key={skill.id} data-testid="trajectory-diagnosis-skill">
                <strong>{t('trajectory.diagnosis.skills')}: {skill.skill_name}</strong>
                <p className={css.suggestionText}>{skill.change_summary}</p>
                {skill.rationale === '' ? null : <p className={css.hint}>{skill.rationale}</p>}
                {skill.selectable ? (
                  <div className={css.row}>
                    <button
                      type="button"
                      className={css.button}
                      onClick={() => session.fillComposer(skill.message)}
                      data-testid="trajectory-diagnosis-skill-fill"
                    >
                      {t('trajectory.diagnosis.fillInput')}
                    </button>
                  </div>
                ) : (
                  <p className={css.hint}>{t('trajectory.diagnosis.blocked', { reason: skill.blocked_reason ?? '' })}</p>
                )}
              </div>
            ))}
            {ttseCauses.map((cause) => {
              const triggered = record.ttse_trigger[cause.id];
              const triggering = session.triggeringTtse.has(cause.id);
              return (
                <div className={css.suggestion} key={cause.id} data-testid="trajectory-diagnosis-ttse">
                  <strong>
                    {t('trajectory.diagnosis.ttse')} · {t(`trajectory.diagnosis.ttseOutcome.${cause.category.slice('ttse_'.length)}`)}
                  </strong>
                  {cause.summary === '' ? null : <p className={css.suggestionText}>{cause.summary}</p>}
                  <div className={css.row}>
                    <button
                      type="button"
                      className={css.button}
                      disabled={triggering || (triggered !== undefined && triggered.result !== 'failed')}
                      onClick={() => { void session.triggerTtse(cause.id); }}
                      data-testid="trajectory-diagnosis-ttse-trigger"
                    >
                      {triggering ? t('trajectory.diagnosis.triggeringTtse') : t('trajectory.diagnosis.triggerTtse')}
                    </button>
                    {triggered === undefined ? null : (
                      <span className={css.hint} data-testid="trajectory-diagnosis-ttse-result">
                        {t(`trajectory.diagnosis.ttseResult.${triggered.result}`)}
                      </span>
                    )}
                  </div>
                </div>
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
            expanded={isStageExpanded(view.status, userChoice)}
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
  const hasBody = stage !== null && (
    (stage.summary ?? '') !== '' || (stage.skip_reason ?? '') !== '' || stage.output !== ''
  );
  const iconKind = view.status;
  const bodyId = `trajectory-diagnosis-stage-body-${view.stageId}`;
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
          <ChevronRight className={`${css.chevron} ${expanded ? css.chevronOpen : ''}`} size={14} strokeWidth={2} aria-hidden />
        </button>
      )}
      {expanded && stage !== null && hasBody ? (
        <div className={css.stageBody} id={bodyId}>
          {stage.summary === undefined || stage.summary === '' ? null : <p className={css.hint}>{stage.summary}</p>}
          {stage.skip_reason === undefined ? null : <p className={css.hint}>{stage.skip_reason}</p>}
          {stage.output === '' ? null : <pre className={css.output}>{stage.output}</pre>}
        </div>
      ) : null}
    </li>
  );
}

function RootCauses({ record, focusedCauseId, onFocusCause }: {
  record: DiagnosisRecord;
  focusedCauseId: string | null;
  onFocusCause: (cause: RootCause | null) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className={css.block} data-testid="trajectory-diagnosis-root-causes">
      <h4 className={css.blockTitle}>{t('trajectory.diagnosis.rootCauses')}</h4>
      {record.root_causes.length === 0 ? <p className={css.hint}>{t('trajectory.diagnosis.noRootCause')}</p> : null}
      {record.root_causes.map((cause) => {
        const focused = focusedCauseId === cause.id;
        return (
          <div className={css.cause} key={cause.id} data-testid="trajectory-diagnosis-root-cause">
            <div className={css.causeHead}>
              <span className={css.badge}>{t(`trajectory.diagnosis.category.${cause.category}`)}</span>
              {cause.confidence === undefined || cause.confidence === null ? null : (
                <span className={css.hint}>
                  {t('trajectory.diagnosis.confidence', { value: Math.round(cause.confidence * 100) })}
                </span>
              )}
              <span className={css.spacer} />
              <button
                type="button"
                className={css.button}
                aria-pressed={focused}
                onClick={() => onFocusCause(focused ? null : cause)}
                data-testid="trajectory-diagnosis-highlight"
              >
                {t('trajectory.diagnosis.highlight')}
              </button>
            </div>
            <strong>{cause.summary}</strong>
            {cause.evidence === '' ? null : <p className={css.hint}>{cause.evidence}</p>}
          </div>
        );
      })}
    </div>
  );
}

/** Structured Reward evaluation (overall / categories / dimensions) plus the raw payload. */
function RewardSection({ reward }: { reward: RewardPayload }) {
  const { t } = useTranslation();
  return (
    <div className={css.block} data-testid="trajectory-diagnosis-reward">
      <h4 className={css.blockTitle}>{t('trajectory.diagnosis.rewardTitle')}</h4>
      <div className={css.rewardOverall} data-testid="trajectory-diagnosis-reward-overall">
        <span className={css.badge}>
          {t('trajectory.diagnosis.rewardTotal', { score: reward.overall.total_score })}
        </span>
        <span className={css.hint}>{reward.overall.comment}</span>
      </div>
      {reward.categories.length === 0 ? null : (
        <table className={css.rewardTable} data-testid="trajectory-diagnosis-reward-categories">
          <thead>
            <tr>
              <th>{t('trajectory.diagnosis.rewardCategoryName')}</th>
              <th>{t('trajectory.diagnosis.rewardScore')}</th>
              <th>{t('trajectory.diagnosis.rewardComment')}</th>
            </tr>
          </thead>
          <tbody>
            {reward.categories.map(category => (
              <tr key={category.name} data-testid="trajectory-diagnosis-reward-category" data-variant={category.name}>
                <td>{category.name}</td>
                <td>{category.score === undefined ? '' : category.score}</td>
                <td>{category.comment ?? ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {reward.dimensions.length === 0 ? null : (
        <table className={css.rewardTable} data-testid="trajectory-diagnosis-reward-dimensions">
          <thead>
            <tr>
              <th>{t('trajectory.diagnosis.rewardCategoryName')}</th>
              <th>{t('trajectory.diagnosis.rewardRequirement')}</th>
              <th>{t('trajectory.diagnosis.rewardScore')}</th>
              <th>{t('trajectory.diagnosis.rewardComment')}</th>
              <th>{t('trajectory.diagnosis.rewardRelatedSignals')}</th>
            </tr>
          </thead>
          <tbody>
            {reward.dimensions.map((dimension, index) => (
              <tr
                key={`${dimension.category}-${index}`}
                data-testid="trajectory-diagnosis-reward-dimension"
                data-variant={`${dimension.category}-${index}`}
              >
                <td>{dimension.category}</td>
                <td>{dimension.requirement}</td>
                <td>{dimension.score}</td>
                <td>{dimension.comment}</td>
                <td>{(dimension.related_signal_ids ?? []).join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <details className={css.rewardRaw}>
        <summary data-testid="trajectory-diagnosis-reward-raw-toggle">{t('trajectory.diagnosis.rewardRaw')}</summary>
        <pre className={css.output} data-testid="trajectory-diagnosis-reward-raw">
          {JSON.stringify(reward, null, 2)}
        </pre>
      </details>
    </div>
  );
}

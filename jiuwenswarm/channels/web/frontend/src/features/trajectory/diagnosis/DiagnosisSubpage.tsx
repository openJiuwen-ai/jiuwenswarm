/** Diagnosis sub-page: what was diagnosed, how the run went, and what to do next. */

import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { experienceJudge, isRunning, skillJudges, ttseRootCauses } from './diagnosisState';
import {
  asRewardPayload,
  type DiagnosisRecord,
  type EvolutionJudge,
} from './diagnosisTypes';
import {
  artifactSuggestion,
  artifactSuggestionItems,
  diagnosisTarget,
  formatElapsed,
  formatTimeWindow,
  isStageExpanded,
  legacyArtifactInstruction,
  looksLikePath,
  processSummary,
  skillBatchFillText,
  skillSuggestionText,
  stageOutputBlocks,
  stageViews,
  suggestionParts,
  suggestionsState,
  truncateText,
  type ArtifactSuggestionItem,
  type StageView,
  type StageViewStatus,
} from './diagnosisView';
import { FillButton } from './DiagnosisFillButton';
import { RewardSummary } from './DiagnosisReward';
import { Findings, FoldableItem } from './DiagnosisFindings';
import { DiagnosisStatusIcon } from './DiagnosisStatusIcon';
import { ClampedMarkdown, PlainFold, WholeMarkdown } from './DiagnosisText';
import type { DiagnosisSession } from './useDiagnosisSession';
import css from './DiagnosisSubpage.module.css';

export interface DiagnosisSubpageProps {
  session: DiagnosisSession;
  /** Display names of the session's agents by subject id. */
  subjectLabels: ReadonlyMap<string, string>;
  /** Finding (failure cause or evolution item) currently emphasised on the trajectory lanes. */
  focusedFindingId: string | null;
  /** Emphasise a finding on the trajectory, or `null` to stop. */
  onFocusFinding: (findingId: string | null) => void;
  /** Leave the diagnosis view once a suggestion has been written into the composer. */
  onComposerFilled?: () => void;
}

export const DiagnosisSubpage = memo(function DiagnosisSubpage({
  session,
  subjectLabels,
  focusedFindingId,
  onFocusFinding,
  onComposerFilled,
}: DiagnosisSubpageProps) {
  const { t } = useTranslation();
  const { record, notice, summaries, fillComposer } = session;
  const fillAndReturn = useCallback((text: string) => {
    fillComposer(text);
    onComposerFilled?.();
  }, [fillComposer, onComposerFilled]);

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
            focusedFindingId={focusedFindingId}
            onFocusFinding={onFocusFinding}
            onFill={fillAndReturn}
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
  focusedFindingId: string | null;
  onFocusFinding: (findingId: string | null) => void;
  onFill: (text: string) => void;
}

function Card({ title, action, testId, className, children }: {
  title: string;
  action?: ReactNode;
  testId: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={className === undefined ? css.card : `${css.card} ${className}`} data-testid={testId}>
      <header className={css.cardHead}>
        <h3 className={css.cardTitle}>{title}</h3>
        {action}
      </header>
      {children}
    </section>
  );
}

/** Suggestions already opened in this session, so the tab dot does not return after leaving the page. */
const seenSuggestionPanes = new Set<string>();

/** The stage row to follow: the one that is running, or the last row before any has started. */
function runningItem(list: HTMLElement | null, runningId: string): HTMLElement | null {
  if (list === null) return null;
  if (runningId === '') return list.querySelector('li:last-child');
  return list.querySelector(`[data-stage-id="${CSS.escape(runningId)}"]`);
}

/** The diagnosis list scrolls as one column; find that box from a node inside it. */
function scrollBox(element: HTMLElement): HTMLElement | null {
  let node = element.parentElement;
  while (node !== null) {
    const overflow = getComputedStyle(node).overflowY;
    if (overflow === 'auto' || overflow === 'scroll') return node;
    node = node.parentElement;
  }
  return null;
}

function RecordCards({
  record,
  session,
  subjectLabels,
  focusedFindingId,
  onFocusFinding,
  onFill,
}: RecordCardsProps) {
  const { t } = useTranslation();
  const running = isRunning(record);
  const target = useMemo(() => diagnosisTarget(record), [record]);
  const labelOf = (subjectId: string) => subjectLabels.get(subjectId) ?? subjectId;
  const note = record.input.user_note?.trim() ?? '';
  const trajectorySummary = truncateText(record.trajectory_summary ?? record.task_description ?? '', 500);
  const reward = asRewardPayload(record.reward);
  const skills = skillJudges(record);
  const experience = experienceJudge(record);
  const artifactBody = artifactSuggestion(record, false);
  const artifactText = artifactBody === ''
    ? ''
    : `${t('trajectory.diagnosis.artifactFillPrefix')}\n${artifactBody}`;
  const artifactItems = artifactSuggestionItems(record);
  const outlets = suggestionsState(record.status);
  const showDetails = record.error !== undefined
    || reward !== null
    || trajectorySummary !== ''
    || (record.reward_failure_causes?.length ?? 0) > 0
    || (record.evolution_judge?.length ?? 0) > 0
    || (record.root_causes?.length ?? 0) > 0;
  const [pane, setPane] = useState<{ id: string; value: 'details' | 'suggestions'; seen: boolean }>(
    () => ({ id: record.diagnosis_id, value: 'details', seen: seenSuggestionPanes.has(record.diagnosis_id) }),
  );
  if (pane.id !== record.diagnosis_id) {
    setPane({
      id: record.diagnosis_id,
      value: 'details',
      seen: seenSuggestionPanes.has(record.diagnosis_id),
    });
  }
  const showSuggestionDot = outlets === 'ready' && pane.value !== 'suggestions' && !pane.seen;
  const tabsRef = useRef<HTMLDivElement>(null);
  const paneRef = useRef<HTMLElement>(null);
  // Viewport Y of the tab bar captured at the click, so the swap does not yank the scroll.
  const paneAnchorRef = useRef<number | null>(null);
  const selectPane = (value: 'details' | 'suggestions') => {
    if (value !== pane.value && tabsRef.current !== null) {
      paneAnchorRef.current = tabsRef.current.getBoundingClientRect().top;
    }
    if (value === 'suggestions') seenSuggestionPanes.add(record.diagnosis_id);
    setPane({ id: record.diagnosis_id, value, seen: pane.seen || value === 'suggestions' });
  };
  useLayoutEffect(() => {
    const anchor = paneAnchorRef.current;
    paneAnchorRef.current = null;
    const tabs = tabsRef.current;
    const pane = paneRef.current;
    if (anchor === null || tabs === null || pane === null) return;
    const scroller = scrollBox(tabs);
    if (scroller === null) return;
    // The two tab panels are the same element type, so React can reuse the section and keep the
    // previous pane's inline min-height. Measure the incoming content without that leftover.
    pane.style.minHeight = '';
    const shift = () => tabs.getBoundingClientRect().top - anchor;
    const delta = shift();
    if (Math.abs(delta) < 1) return;
    const desired = scroller.scrollTop + delta;
    const max = scroller.scrollHeight - scroller.clientHeight;
    // The other pane is shorter, so the browser would clamp the scroll and pull the cards above into view.
    if (desired > max) {
      pane.style.minHeight = `${Math.ceil(pane.getBoundingClientRect().height + desired - max)}px`;
    }
    scroller.scrollTop += shift();
  }, [pane.value]);

  return (
    <>
      <Card
        title={t('trajectory.diagnosis.object')}
        testId="trajectory-diagnosis-object"
        className={`${css.objectCard} ${css.spacedCard}`}
      >
        {target.full ? (
          <>
            {target.window === null ? null : (
              <p className={css.line}>
                <span className={css.lineLabel}>{t('trajectory.diagnosis.objectTime')}</span>
                {formatTimeWindow(target.window.startMs, target.window.endMs)}
              </p>
            )}
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
        {note === '' ? null : (
          <p className={css.line} data-testid="trajectory-diagnosis-object-note">
            <span className={css.lineLabel}>{t('trajectory.diagnosis.note')}</span>
            <span className={css.noteInline}>{note}</span>
          </p>
        )}
        {record.input.excluded_running.length > 0 ? (
          <p className={css.hint} data-testid="trajectory-diagnosis-object-excluded">
            {t('trajectory.diagnosis.objectExcludedRunning', { count: record.input.excluded_running.length })}
          </p>
        ) : null}
      </Card>

      <ProcessSection
        record={record}
        running={running}
        onCancel={running ? () => { void session.cancel(); } : undefined}
      />

      <div
        ref={tabsRef}
        className={css.resultTabs}
        role="tablist"
        aria-label={t('trajectory.diagnosis.resultTabs')}
      >
        <button
          type="button"
          role="tab"
          className={css.resultTab}
          aria-selected={pane.value === 'details'}
          onClick={() => selectPane('details')}
          data-testid="trajectory-diagnosis-pane-details"
        >
          {t('trajectory.diagnosis.details')}
        </button>
        <span className={css.resultTabDivider} aria-hidden />
        <button
          type="button"
          role="tab"
          className={css.resultTab}
          aria-selected={pane.value === 'suggestions'}
          onClick={() => selectPane('suggestions')}
          data-testid="trajectory-diagnosis-pane-suggestions"
        >
          {t('trajectory.diagnosis.suggestions')}
          {showSuggestionDot ? (
            <>
              <span className={css.resultTabDot} aria-hidden />
              <span className={css.srOnly}>{t('trajectory.diagnosis.suggestionsReady')}</span>
            </>
          ) : null}
        </button>
      </div>

      {pane.value === 'details' ? (
        <section
          key={pane.value}
          ref={paneRef}
          role="tabpanel"
          className={css.resultPane}
          data-testid="trajectory-diagnosis-record"
        >
          {record.error === undefined ? null : (
            <p className={`${css.hint} ${css.errorText}`} role="alert">{record.error.message}</p>
          )}
          {reward !== null ? <RewardSummary reward={reward} /> : null}
          {/* Findings stream in under the evaluation once scoring has landed. */}
          <Findings
            record={record}
            subjectLabels={subjectLabels}
            focusedFindingId={focusedFindingId}
            onFocusFinding={onFocusFinding}
          />
          {trajectorySummary === '' ? null : (
            <FoldableItem
              title={t('trajectory.diagnosis.objectSummary')}
              className={css.summaryBlock}
              testId="trajectory-diagnosis-task"
              toggleTestId="trajectory-diagnosis-summary-toggle"
            >
              <div className={css.itemBody}>
                <PlainFold text={trajectorySummary} />
              </div>
            </FoldableItem>
          )}
          {running && !showDetails ? (
            <p className={css.hint} data-testid="trajectory-diagnosis-details-pending">
              {t('trajectory.diagnosis.detailsPending')}
            </p>
          ) : null}
        </section>
      ) : (
        <section
          key={pane.value}
          ref={paneRef}
          role="tabpanel"
          className={css.resultPane}
          data-testid="trajectory-diagnosis-suggestions"
        >
        {outlets === 'pending' ? (
          <p className={css.hint}>{t('trajectory.diagnosis.outletsPending')}</p>
        ) : outlets === 'unfinished' ? (
          <p className={css.hint} data-testid="trajectory-diagnosis-unfinished">
            {t('trajectory.diagnosis.suggestionsUnfinished')}
          </p>
        ) : (
          <>
            {artifactItems.length > 0 ? (
              <SuggestionCard
                tone="artifact"
                kind={t('trajectory.diagnosis.artifactFix')}
                testId="trajectory-diagnosis-artifact-fix"
                action={(
                  <FillButton
                    text={artifactText}
                    onFill={onFill}
                    testId="trajectory-diagnosis-artifact-fix-fill"
                  />
                )}
              >
                <ArtifactCauseList items={artifactItems} />
              </SuggestionCard>
            ) : artifactText === '' ? null : (
              <SuggestionCard
                tone="artifact"
                kind={t('trajectory.diagnosis.artifactFix')}
                testId="trajectory-diagnosis-artifact-fix"
                action={(
                  <FillButton
                    text={artifactText}
                    onFill={onFill}
                    testId="trajectory-diagnosis-artifact-fix-fill"
                  />
                )}
              >
                <ClampedMarkdown text={artifactText} />
              </SuggestionCard>
            )}
            <SkillSuggestions skills={skills} onFill={onFill} />
            {experience === null ? null : (
              <EvolutionCard
                judge={experience}
                action={(
                  <span className={css.row}>
                    {record.ttse_trigger[experience.id] === undefined ? null : (
                      <span className={css.hint} data-testid="trajectory-diagnosis-ttse-result">
                        {t(`trajectory.diagnosis.ttseResult.${record.ttse_trigger[experience.id].result}`)}
                      </span>
                    )}
                    <button
                      type="button"
                      className={css.actionButton}
                      disabled={
                        session.triggeringTtse.has(experience.id)
                        || (record.ttse_trigger[experience.id] !== undefined
                          && record.ttse_trigger[experience.id].result !== 'failed')
                      }
                      onClick={() => { void session.triggerTtse(experience.id); }}
                      data-testid="trajectory-diagnosis-ttse-trigger"
                    >
                      {session.triggeringTtse.has(experience.id)
                        ? t('trajectory.diagnosis.triggeringTtse')
                        : t('trajectory.diagnosis.triggerTtse')}
                    </button>
                  </span>
                )}
              >
                <p className={css.hint}>{t('trajectory.diagnosis.ttseHint')}</p>
              </EvolutionCard>
            )}
            <LegacySuggestions record={record} onFill={onFill} />
            {artifactItems.length === 0 && artifactText === '' && skills.length === 0 && experience === null ? (
              <p className={css.hint}>{t('trajectory.diagnosis.noSuggestions')}</p>
            ) : null}
          </>
        )}
        </section>
      )}
    </>
  );
}

/** The artifact-fix suggestions: one numbered line per suggestion, not one block per cause. */
function ArtifactCauseList({ items }: { items: readonly ArtifactSuggestionItem[] }) {
  return <SuggestionLines lines={items.flatMap(item => item.parts)} />;
}

/** One suggestion as written. Several are a numbered list so they stay distinct. */
function SuggestionLines({ lines }: { lines: readonly string[] }) {
  if (lines.length === 0) return null;
  if (lines.length === 1) return <SuggestionMarkdown text={lines[0]} />;
  return (
    <ol className={css.suggestionLines} data-testid="trajectory-diagnosis-suggestion-lines">
      {lines.map((line, index) => (
        <li key={`${index}:${line.slice(0, 32)}`}>
          <SuggestionMarkdown text={line} />
        </li>
      ))}
    </ol>
  );
}

function SuggestionMarkdown({ text }: { text: string }) {
  return (
    <div className={css.suggestionBody}>
      <WholeMarkdown text={text} />
    </div>
  );
}

function skillSuggestionParts(judge: EvolutionJudge): string[] {
  return suggestionParts(judge.suggestion);
}

/** Skill cards. Two or more can be ticked and filled together; each card can still fill itself. */
function SkillSuggestions({ skills, onFill }: {
  skills: readonly EvolutionJudge[];
  onFill: (text: string) => void;
}) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const batch = skills.length > 1;
  const chosen = skills.filter(skill => selected.has(skill.id));
  const allOn = batch && chosen.length === skills.length;
  const prefix = t('trajectory.diagnosis.skillFillPrefix');
  const toggle = (id: string, on: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };
  return (
    <>
      {batch ? (
        <div className={css.skillBatch} data-testid="trajectory-diagnosis-skill-batch">
          <label className={css.skillCheck}>
            <input
              type="checkbox"
              checked={allOn}
              ref={(node) => {
                if (node !== null) node.indeterminate = chosen.length > 0 && !allOn;
              }}
              onChange={() => {
                setSelected(allOn ? new Set() : new Set(skills.map(skill => skill.id)));
              }}
              data-testid="trajectory-diagnosis-skill-select-all"
            />
            {t('trajectory.diagnosis.skillSelectAll')}
          </label>
          <span className={css.spacer} />
          <FillButton
            text={skillBatchFillText(chosen, prefix)}
            onFill={onFill}
            testId="trajectory-diagnosis-skill-fill-selected"
            label={t('trajectory.diagnosis.skillFillSelected', { count: chosen.length })}
            disabled={chosen.length === 0}
          />
        </div>
      ) : null}
      {skills.map(judge => (
        <EvolutionCard
          key={judge.id}
          judge={judge}
          selected={selected.has(judge.id)}
          onSelect={batch ? (on => toggle(judge.id, on)) : undefined}
          action={(
            <FillButton
              text={`${prefix}\n\n${skillSuggestionText(judge)}`}
              onFill={onFill}
              testId="trajectory-diagnosis-skill-fill"
            />
          )}
        >
          <SuggestionLines lines={skillSuggestionParts(judge)} />
        </EvolutionCard>
      ))}
    </>
  );
}

/** One evolution item: the advice/verdict text and its action; details live in the findings list. */
function EvolutionCard({ judge, action, children, selected = false, onSelect }: {
  judge: EvolutionJudge;
  action: ReactNode;
  children: ReactNode;
  selected?: boolean;
  onSelect?: (selected: boolean) => void;
}) {
  const { t } = useTranslation();
  const skill = judge.category === 'skill';
  const title = skill ? (judge.target ?? '') : t('trajectory.diagnosis.ttseReady');
  const leading = onSelect === undefined ? undefined : (
    <label className={css.skillCheck}>
      <input
        type="checkbox"
        checked={selected}
        onChange={event => onSelect(event.target.checked)}
        aria-label={t('trajectory.diagnosis.skillSelectOne', { name: title || judge.id })}
        data-testid="trajectory-diagnosis-skill-select"
      />
    </label>
  );
  return (
    <SuggestionCard
      tone={skill ? 'skill' : 'experience'}
      kind={t(skill ? 'trajectory.diagnosis.skills' : 'trajectory.diagnosis.ttse')}
      title={title}
      mono={skill && looksLikePath(title)}
      testId={skill ? 'trajectory-diagnosis-skill' : 'trajectory-diagnosis-ttse'}
      action={action}
      leading={leading}
    >
      {children}
    </SuggestionCard>
  );
}

/** Suggestions of a record written before the switch (read-only fallback). */
function LegacySuggestions({ record, onFill }: {
  record: DiagnosisRecord;
  onFill: (text: string) => void;
}) {
  const { t } = useTranslation();
  const suggestions = record.suggestions;
  const hasNew = (record.reward_failure_causes ?? []).length > 0 || (record.evolution_judge ?? []).length > 0;
  if (suggestions === undefined || hasNew) return null;
  const instruction = legacyArtifactInstruction(record);
  const experience = ttseRootCauses(record);
  return (
    <>
      {instruction === '' ? null : (
        <SuggestionCard
          tone="artifact"
          kind={t('trajectory.diagnosis.artifactFix')}
          testId="trajectory-diagnosis-artifact-fix"
          action={(
            <FillButton
              text={instruction}
              onFill={onFill}
              testId="trajectory-diagnosis-artifact-fix-fill"
            />
          )}
        >
          <ClampedMarkdown text={instruction} />
        </SuggestionCard>
      )}
      {(suggestions.skills ?? []).map(skill => (
        <SuggestionCard
          key={skill.id}
          tone="skill"
          kind={t('trajectory.diagnosis.skills')}
          title={skill.skill_name}
          mono={looksLikePath(skill.skill_name)}
          testId="trajectory-diagnosis-skill"
          action={(
            <FillButton
              text={`${t('trajectory.diagnosis.skillFillPrefix')}\n\n${skill.message}`}
              onFill={onFill}
              testId="trajectory-diagnosis-skill-fill"
            />
          )}
        >
          <ClampedMarkdown text={skill.change_summary} />
        </SuggestionCard>
      ))}
      {experience.map(cause => (
        <SuggestionCard
          key={cause.id}
          tone="experience"
          kind={t('trajectory.diagnosis.ttse')}
          title={t('trajectory.diagnosis.ttseReady')}
          testId="trajectory-diagnosis-ttse"
          action={<span />}
        >
          <p className={css.hint}>{t('trajectory.diagnosis.ttseHint')}</p>
        </SuggestionCard>
      ))}
    </>
  );
}

/** One suggestion: what kind it is and what it is about on top, the action beside them, the detail below. */
function SuggestionCard({ tone, kind, title, mono = false, action, testId, leading, children }: {
  tone: 'artifact' | 'skill' | 'experience';
  kind: string;
  title?: string;
  mono?: boolean;
  action: ReactNode;
  testId: string;
  leading?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className={`${css.suggestion} ${css[`tone_${tone}`]}`} data-testid={testId}>
      <div className={css.suggestionHead}>
        {leading}
        <span className={css.suggestionKind}>{kind}</span>
        {title === undefined ? null : (
          <strong className={`${css.suggestionTitle} ${mono ? css.mono : ''}`}>{title}</strong>
        )}
        <span className={css.spacer} />
        {action}
      </div>
      <div className={css.suggestionScroll}>
        {children}
      </div>
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
 * How the diagnosis ran, in its own card. While it runs every stage shows its detail, and afterwards
 * each folds to its summary line.
 */
function ProcessSection({ record, running, onCancel }: {
  record: DiagnosisRecord;
  running: boolean;
  onCancel?: () => void;
}) {
  const { t } = useTranslation();
  const summary = useMemo(() => processSummary(record.stages), [record.stages]);
  // A starting run has no stage yet, but its waiting placeholders are worth showing.
  if (record.stages.length === 0 && !running) return null;
  const meta = running
    ? ''
    : [t('trajectory.diagnosis.processSteps', { count: summary.count }), summary.elapsed]
      .filter(item => item !== '')
      .join(' · ');
  const action = onCancel === undefined ? (
    meta === '' ? undefined : <span className={css.hint}>{meta}</span>
  ) : (
    <button
      type="button"
      className={css.button}
      onClick={onCancel}
      data-testid="trajectory-diagnosis-cancel"
    >
      {t('trajectory.diagnosis.cancel')}
    </button>
  );
  return (
    <Card
      title={t('trajectory.diagnosis.process')}
      testId="trajectory-diagnosis-process"
      className={`${css.objectCard} ${css.spacedCard}`}
      action={action}
    >
      <StageList record={record} running={running} />
    </Card>
  );
}

function StageList({ record, running }: { record: DiagnosisRecord; running: boolean }) {
  const views = useMemo(() => stageViews(record.stages, running), [record.stages, running]);
  // A choice only holds for the status it was made in: a stage that moves on re-applies the default.
  const [choices, setChoices] = useState<Record<string, { status: StageViewStatus; open: boolean }>>({});
  const toggle = useCallback((view: StageView, open: boolean) => {
    setChoices(current => ({ ...current, [view.stageId]: { status: view.status, open } }));
  }, []);
  const listRef = useRef<HTMLOListElement>(null);
  const followRef = useRef(true);
  const programmaticRef = useRef(false);
  const seenRunningRef = useRef('');
  const runningStage = views.find(view => view.status === 'running');
  const runningId = runningStage?.stageId ?? '';
  const runningOutput = runningStage?.stage?.output ?? '';
  const onListScroll = useCallback(() => {
    if (programmaticRef.current) {
      programmaticRef.current = false;
      return;
    }
    const list = listRef.current;
    const item = runningItem(list, runningId);
    followRef.current = list !== null && item !== null && item.getBoundingClientRect().bottom <= list.getBoundingClientRect().bottom + 24;
  }, [runningId]);
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!running || list === null) return;
    // A newly started step is the one to read, even if an earlier step was scrolled into view.
    if (seenRunningRef.current !== runningId) {
      seenRunningRef.current = runningId;
      followRef.current = true;
    }
    const item = runningItem(list, runningId);
    if (item === null || !followRef.current) return;
    const listRect = list.getBoundingClientRect();
    const itemRect = item.getBoundingClientRect();
    const overflowBelow = itemRect.bottom - listRect.bottom;
    const overflowAbove = listRect.top - itemRect.top;
    const bringUp = itemRect.height <= listRect.height && overflowAbove > 1;
    if (overflowBelow <= 1 && !bringUp) return;
    const before = list.scrollTop;
    programmaticRef.current = true;
    list.scrollTop += overflowBelow > 1 ? overflowBelow : -overflowAbove;
    if (list.scrollTop === before) programmaticRef.current = false;
  }, [running, runningId, runningOutput]);
  if (views.length === 0) return null;
  return (
    <ol
      ref={listRef}
      className={css.stages}
      data-testid="trajectory-diagnosis-stages"
      onScroll={onListScroll}
    >
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
  const live = view.status === 'running';
  const blocks = useMemo(
    () => stageOutputBlocks(stage?.output ?? '', live),
    [live, stage?.output],
  );
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
          {blocks.map((block, index) => (
            block.kind === 'body' ? (
              <pre key={`body-${index}`} className={css.output}>{block.text}</pre>
            ) : (
              <details key={`requests-${index}`} className={css.requestFold}>
                <summary data-testid="trajectory-diagnosis-stage-requests">
                  {t('trajectory.diagnosis.requestCount', { count: block.count })}
                </summary>
                <pre className={css.output}>{block.lines.join('\n')}</pre>
              </details>
            )
          ))}
        </div>
      ) : null}
    </li>
  );
}

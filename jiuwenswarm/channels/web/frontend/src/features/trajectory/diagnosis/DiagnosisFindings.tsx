/** Findings of a diagnosis: the deliverable failure causes, plus legacy root causes of older records. */

import { useMemo, useState, type ReactNode } from 'react';
import { ChevronRight, CircleCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type {
  DiagnosisRecord,
  EvolutionJudge,
  LegacyRootCause,
  RewardFailureCause,
  SignalRef,
} from './diagnosisTypes';
import {
  causeNodeLabel,
  artifactCauseGroups,
  causeReasonParts,
  evidenceNodeLabel,
  findingCounts,
  findingItems,
  signalEvidenceBody,
  signalTier,
  usesLegacyFindings,
} from './diagnosisView';
import { MarkdownText } from '../primitives/index.ts';
import { FoldedMarkdown, PlainText } from './DiagnosisText';
import css from './DiagnosisSubpage.module.css';

interface FindingListProps {
  record: DiagnosisRecord;
  subjectLabels: ReadonlyMap<string, string>;
  focusedFindingId: string | null;
  onFocusFinding: (findingId: string | null) => void;
}

/** Section mark: a drawn circle-check, sized to the title line. */
export function ItemMark() {
  return <CircleCheck className={css.itemMark} size={16} strokeWidth={2} aria-hidden />;
}

/** One details section: the title stays, and the body folds from the chevron on the right. */
export function FoldableItem({
  title,
  extra,
  children,
  className,
  testId,
  toggleTestId,
}: {
  title: string;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
  testId?: string;
  toggleTestId?: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(true);
  return (
    <div className={className === undefined ? css.block : `${css.block} ${className}`} data-testid={testId}>
      <button
        type="button"
        className={`${css.itemHead} ${css.itemToggle}`}
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
        data-testid={toggleTestId}
      >
        <ItemMark />
        <h4 className={css.blockTitle}>{title}</h4>
        {extra}
        <ChevronRight
          className={`${css.chevron} ${css.itemChevron} ${open ? css.chevronOpen : ''}`}
          size={16}
          strokeWidth={2}
          aria-hidden
        />
        <span className={css.srOnly}>
          {t(open ? 'trajectory.diagnosis.sectionCollapse' : 'trajectory.diagnosis.sectionExpand')}
        </span>
      </button>
      {open ? children : null}
    </div>
  );
}

function HighlightButton({ id, focused, onFocusFinding }: {
  id: string;
  focused: boolean;
  onFocusFinding: (findingId: string | null) => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      className={css.highlightButton}
      aria-pressed={focused}
      onClick={() => onFocusFinding(focused ? null : id)}
      data-testid="trajectory-diagnosis-highlight"
    >
      {t('trajectory.diagnosis.highlight')}
    </button>
  );
}

function KindBadge({ kind }: { kind: 'artifact' | 'skill' | 'experience' }) {
  const { t } = useTranslation();
  return (
    <span className={`${css.kind} ${css[`kind_${kind}`]}`} data-testid="trajectory-diagnosis-attribution">
      {t('trajectory.diagnosis.attributionLabel', {
        kind: t(`trajectory.diagnosis.attributionKind.${kind}`),
      })}
    </span>
  );
}

export function SignalTags({ signal }: { signal: SignalRef | null | undefined }) {
  const { t } = useTranslation();
  if (signal === null || signal === undefined) return null;
  const tier = signalTier(signal);
  return (
    <>
      <span className={css.chip}>
        {t('trajectory.diagnosis.signalSourceLabel', {
          source: t(`trajectory.diagnosis.signalSource.${signal.source}`, { defaultValue: signal.source }),
        })}
      </span>
      {tier === null ? null : (
        <span className={`${css.tag} ${css[`tier_${tier}`]}`} data-testid="trajectory-diagnosis-finding-tag">
          {t(`trajectory.diagnosis.signalTier.${tier}`)}
        </span>
      )}
      {signal.category === undefined || signal.category === null || signal.category === '' ? null : (
        <span className={css.badge} data-testid="trajectory-diagnosis-signal-category">{signal.category}</span>
      )}
      {typeof signal.score === 'number' ? (
        <span className={css.hint}>{t('trajectory.diagnosis.signalScore', { score: signal.score })}</span>
      ) : null}
    </>
  );
}

/** Category and the dimension requirement on one line; either part alone when the other is empty. */
function causeTopic(
  category: string | null | undefined,
  dimension: string,
  join: (category: string, dimension: string) => string,
): string {
  const name = (category ?? '').trim();
  const requirement = dimension.trim();
  if (name !== '' && requirement !== '') return join(name, requirement);
  return name !== '' ? name : requirement;
}

function subjectIdsOf(evidence: readonly { subject_id: string }[]): string[] {
  const subjectIds: string[] = [];
  for (const ref of evidence) {
    if (ref.subject_id !== '' && !subjectIds.includes(ref.subject_id)) subjectIds.push(ref.subject_id);
  }
  return subjectIds;
}

function SubjectChips({ subjectIds, subjectLabels }: {
  subjectIds: readonly string[];
  subjectLabels: ReadonlyMap<string, string>;
}) {
  if (subjectLabels.size <= 1) return null;
  return subjectIds.map(subjectId => (
    <span key={subjectId} className={css.chip} data-testid="trajectory-diagnosis-cause-agent">
      {subjectLabels.get(subjectId) ?? subjectId}
    </span>
  ));
}

function CauseLocation({ causes, subjectLabels, focusedFindingId, onFocusFinding }: {
  causes: readonly RewardFailureCause[];
  subjectLabels: ReadonlyMap<string, string>;
  focusedFindingId: string | null;
  onFocusFinding: (findingId: string | null) => void;
}) {
  const { t } = useTranslation();
  const nodes = evidenceNodeLabel({ evidence: causes.flatMap(cause => cause.evidence) });
  const focused = causes.some(cause => cause.id === focusedFindingId);
  return (
    <>
      <SubjectChips
        subjectIds={subjectIdsOf(causes.flatMap(cause => cause.evidence))}
        subjectLabels={subjectLabels}
      />
      {nodes === '' ? null : (
        <span className={`${css.hint} ${css.causeAt}`} data-testid="trajectory-diagnosis-cause-nodes">
          {t('trajectory.diagnosis.causeAt', { nodes })}
        </span>
      )}
      <HighlightButton
        id={causes[0].id}
        focused={focused}
        onFocusFinding={onFocusFinding}
      />
    </>
  );
}

function CauseCard({ causes, subjectLabels, focusedFindingId, onFocusFinding }: {
  causes: readonly RewardFailureCause[];
  subjectLabels: ReadonlyMap<string, string>;
  focusedFindingId: string | null;
  onFocusFinding: (findingId: string | null) => void;
}) {
  const { t } = useTranslation();
  const cause = causes[0];
  const topic = causeTopic(cause.signal?.category, cause.dimension, (category, dimension) => (
    t('trajectory.diagnosis.causeTopic', { category, dimension })
  ));
  const comment = signalEvidenceBody(cause.signal, cause.dimension);
  const score = causes.find(item => typeof item.signal?.score === 'number')?.signal?.score;
  const reasons = causeReasonParts(causes);
  const hasBody = comment !== '' || reasons.length > 0;
  return (
    <div className={css.cause} data-testid="trajectory-diagnosis-finding">
      <div className={`${css.causeHead} ${css.causeHeadTop}`}>
        <KindBadge kind="artifact" />
        {topic === '' ? null : (
          <p className={css.causeDimension} data-testid="trajectory-diagnosis-cause-topic">
            <span className={css.lineLabel}>{t('trajectory.diagnosis.findingDimension')}</span>
            <span className={css.findingLineBody}>{topic}</span>
          </p>
        )}
        {typeof score === 'number' ? (
          <span className={css.hint}>{t('trajectory.diagnosis.signalScore', { score })}</span>
        ) : null}
        <CauseLocation
          causes={causes}
          subjectLabels={subjectLabels}
          focusedFindingId={focusedFindingId}
          onFocusFinding={onFocusFinding}
        />
      </div>
      {hasBody ? (
        <div className={css.itemBody}>
          {comment === '' ? null : (
            <p className={css.findingLine} data-testid="trajectory-diagnosis-cause-problem">
              <span className={css.lineLabel}>{t('trajectory.diagnosis.findingProblem')}</span>
              <span className={css.findingLineBody}>{comment}</span>
            </p>
          )}
          {reasons.length === 0 ? null : (
            <div className={css.findingLine} data-testid="trajectory-diagnosis-cause-reason">
              <span className={css.lineLabel}>{t('trajectory.diagnosis.findingReason')}</span>
              <div className={css.findingLineBody}>
                {reasons.length === 1 ? <FoldedMarkdown text={reasons[0]} /> : (
                  <div className={css.reasonList}>
                    {reasons.map((part, index) => (
                      <div key={index} className={css.reasonItem}>
                        <span className={css.reasonIndex}>{index + 1}.</span>
                        <div className={css.reasonText}>
                          <MarkdownText text={part} />
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

/** One skill/experience finding in the unified findings list (no suggestion/actions here). */
function EvolutionFindingCard({ judge, subjectLabels, focused, onFocusFinding }: {
  judge: EvolutionJudge;
  subjectLabels: ReadonlyMap<string, string>;
  focused: boolean;
  onFocusFinding: (findingId: string | null) => void;
}) {
  const { t } = useTranslation();
  const skill = judge.category === 'skill';
  const nodes = evidenceNodeLabel(judge);
  return (
    <div className={css.cause} data-testid="trajectory-diagnosis-finding">
      <div className={`${css.causeHead} ${css.causeHeadTop}`}>
        <KindBadge kind={judge.category} />
        {!skill || judge.target === null || judge.target === undefined || judge.target === '' ? null : (
          <span className={css.badge} title={judge.target}>{judge.target}</span>
        )}
        {skill ? (
          <span className={css.tag} data-testid="trajectory-diagnosis-evolution-score">
            {t('trajectory.diagnosis.confidence', { value: Math.round(judge.score * 100) })}
          </span>
        ) : null}
        <span className={css.spacer} />
        <SubjectChips subjectIds={subjectIdsOf(judge.evidence)} subjectLabels={subjectLabels} />
        {nodes === '' ? null : (
          <span className={`${css.hint} ${css.causeAt}`} data-testid="trajectory-diagnosis-cause-nodes">
            {t('trajectory.diagnosis.causeAt', { nodes })}
          </span>
        )}
        <HighlightButton id={judge.id} focused={focused} onFocusFinding={onFocusFinding} />
      </div>
      {judge.summary.trim() === '' ? null : (
        <div className={css.itemBody}>
          <FoldedMarkdown text={judge.summary} />
        </div>
      )}
    </div>
  );
}

function LegacyCauseCard({ cause, subjectLabels, focused, onFocusFinding }: {
  cause: LegacyRootCause;
  subjectLabels: ReadonlyMap<string, string>;
  focused: boolean;
  onFocusFinding: (findingId: string | null) => void;
}) {
  const { t } = useTranslation();
  const nodes = causeNodeLabel(cause);
  return (
    <div className={css.cause} data-testid="trajectory-diagnosis-finding">
      <div className={css.causeHead}>
        <span className={css.badge}>{t(`trajectory.diagnosis.category.${cause.category}`, { defaultValue: cause.category })}</span>
        <SignalTags signal={cause.signal} />
        {subjectLabels.size > 1 ? (
          <span className={css.chip} data-testid="trajectory-diagnosis-cause-agent">
            {subjectLabels.get(cause.subject_id) ?? cause.subject_id}
          </span>
        ) : null}
        {nodes === '' ? null : (
          <span className={css.hint} data-testid="trajectory-diagnosis-cause-nodes">
            {t('trajectory.diagnosis.causeAt', { nodes })}
          </span>
        )}
        <span className={css.spacer} />
        <HighlightButton id={cause.id} focused={focused} onFocusFinding={onFocusFinding} />
      </div>
      {cause.summary.trim() === '' && cause.evidence.trim() === '' ? null : (
        <div className={css.itemBody}>
          {cause.summary.trim() === '' ? null : <FoldedMarkdown text={cause.summary} />}
          {cause.evidence.trim() === '' ? null : (
            <details className={css.evidence} data-testid="trajectory-diagnosis-evidence">
              <summary>{t('trajectory.diagnosis.evidence')}</summary>
              <div className={css.evidenceBody}>
                <PlainText text={cause.evidence} />
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Everything the diagnosis found. Deliverable causes that answer one problem
 * share a card: the problem once, then each reason with its own location.
 * Records written before the interface switch show their legacy root causes
 * read-only.
 */
export function Findings({ record, subjectLabels, focusedFindingId, onFocusFinding }: FindingListProps) {
  const { t } = useTranslation();
  const counts = useMemo(() => findingCounts(record), [record]);
  const items = useMemo(() => findingItems(record), [record]);
  const causeGroups = useMemo(
    () => artifactCauseGroups(items.flatMap(item => (item.cause === undefined ? [] : [item.cause]))),
    [items],
  );
  const legacy = usesLegacyFindings(record) ? record.root_causes ?? [] : [];
  if (items.length === 0 && legacy.length === 0) return null;
  const causeText = counts.causes === 0
    ? ''
    : t(counts.legacy ? 'trajectory.diagnosis.findingLegacyCauses' : 'trajectory.diagnosis.findingCauses', {
      count: counts.causes,
    });
  const evolution = [
    counts.skill === 0 ? '' : t('trajectory.diagnosis.findingSkill', { count: counts.skill }),
    counts.experience === 0 ? '' : t('trajectory.diagnosis.findingExperience', { count: counts.experience }),
  ].filter(text => text !== '').join(t('trajectory.diagnosis.findingJoin'));
  const countsLabel = causeText !== '' && evolution !== ''
    ? t('trajectory.diagnosis.findingWithEvolution', { causes: causeText, evolution })
    : (causeText !== '' ? causeText : evolution);
  return (
    <FoldableItem
      title={t('trajectory.diagnosis.rootCauses')}
      extra={<span className={css.hint} data-testid="trajectory-diagnosis-findings-count">{countsLabel}</span>}
      testId="trajectory-diagnosis-root-causes"
      toggleTestId="trajectory-diagnosis-findings-toggle"
    >
      <div className={css.findingList} data-testid="trajectory-diagnosis-finding-list">
        {causeGroups.map(group => (
          <CauseCard
            key={group.id}
            causes={group.causes}
            subjectLabels={subjectLabels}
            focusedFindingId={focusedFindingId}
            onFocusFinding={onFocusFinding}
          />
        ))}
        {items.flatMap(item => (item.judge === undefined ? [] : [
          <EvolutionFindingCard
            key={item.id}
            judge={item.judge}
            subjectLabels={subjectLabels}
            focused={focusedFindingId === item.id}
            onFocusFinding={onFocusFinding}
          />,
        ]))}
        {legacy.map(cause => (
          <LegacyCauseCard
            key={cause.id}
            cause={cause}
            subjectLabels={subjectLabels}
            focused={focusedFindingId === cause.id}
            onFocusFinding={onFocusFinding}
          />
        ))}
      </div>
    </FoldableItem>
  );
}

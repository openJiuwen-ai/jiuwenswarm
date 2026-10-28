/** Findings of a diagnosis: the deliverable failure causes, plus legacy root causes of older records. */

import { useMemo } from 'react';
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
  evidenceNodeLabel,
  findingCounts,
  findingItems,
  signalEvidenceBody,
  signalTier,
  usesLegacyFindings,
} from './diagnosisView';
import { FoldedMarkdown, PlainText } from './DiagnosisText';
import css from './DiagnosisSubpage.module.css';

interface FindingListProps {
  record: DiagnosisRecord;
  subjectLabels: ReadonlyMap<string, string>;
  focusedFindingId: string | null;
  onFocusFinding: (findingId: string | null) => void;
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
      className={css.button}
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

function CauseCard({ cause, subjectLabels, focused, onFocusFinding }: {
  cause: RewardFailureCause;
  subjectLabels: ReadonlyMap<string, string>;
  focused: boolean;
  onFocusFinding: (findingId: string | null) => void;
}) {
  const { t } = useTranslation();
  const nodes = evidenceNodeLabel(cause);
  const subjectId = cause.evidence[0]?.subject_id ?? '';
  const topic = causeTopic(cause.signal?.category, cause.dimension, (category, dimension) => (
    t('trajectory.diagnosis.causeTopic', { category, dimension })
  ));
  const comment = signalEvidenceBody(cause.signal, cause.dimension);
  const score = cause.signal?.score;
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
        {subjectLabels.size > 1 && subjectId !== '' ? (
          <span className={css.chip} data-testid="trajectory-diagnosis-cause-agent">
            {subjectLabels.get(subjectId) ?? subjectId}
          </span>
        ) : null}
        {nodes === '' ? null : (
          <span className={`${css.hint} ${css.causeAt}`} data-testid="trajectory-diagnosis-cause-nodes">
            {t('trajectory.diagnosis.causeAt', { nodes })}
          </span>
        )}
        <HighlightButton id={cause.id} focused={focused} onFocusFinding={onFocusFinding} />
      </div>
      {comment === '' ? null : (
        <p className={css.findingLine} data-testid="trajectory-diagnosis-cause-problem">
          <span className={css.lineLabel}>{t('trajectory.diagnosis.findingProblem')}</span>
          <span className={css.findingLineBody}>{comment}</span>
        </p>
      )}
      {cause.summary.trim() === '' ? null : (
        <div className={css.findingLine} data-testid="trajectory-diagnosis-cause-reason">
          <span className={css.lineLabel}>{t('trajectory.diagnosis.findingReason')}</span>
          <div className={css.findingLineBody}>
            <FoldedMarkdown text={cause.summary} />
          </div>
        </div>
      )}
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
  const subjectId = judge.evidence[0]?.subject_id ?? '';
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
        {subjectLabels.size > 1 && subjectId !== '' ? (
          <span className={css.chip}>{subjectLabels.get(subjectId) ?? subjectId}</span>
        ) : null}
        <span className={css.spacer} />
        {nodes === '' ? null : (
          <span className={`${css.hint} ${css.causeAt}`} data-testid="trajectory-diagnosis-cause-nodes">
            {t('trajectory.diagnosis.causeAt', { nodes })}
          </span>
        )}
        <HighlightButton id={judge.id} focused={focused} onFocusFinding={onFocusFinding} />
      </div>
      <FoldedMarkdown text={judge.summary} />
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
      <FoldedMarkdown text={cause.summary} />
      {cause.evidence.trim() === '' ? null : (
        <details className={css.evidence} data-testid="trajectory-diagnosis-evidence">
          <summary>{t('trajectory.diagnosis.evidence')}</summary>
          <div className={css.evidenceBody}>
            <PlainText text={cause.evidence} />
          </div>
        </details>
      )}
    </div>
  );
}

/**
 * Everything the diagnosis found: one card per deliverable failure cause. Records
 * written before the interface switch show their legacy root causes read-only.
 */
export function Findings({ record, subjectLabels, focusedFindingId, onFocusFinding }: FindingListProps) {
  const { t } = useTranslation();
  const counts = useMemo(() => findingCounts(record), [record]);
  const items = useMemo(() => findingItems(record), [record]);
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
    <div className={css.block} data-testid="trajectory-diagnosis-root-causes">
      <div className={css.row}>
        <h4 className={css.blockTitle}>{t('trajectory.diagnosis.rootCauses')}</h4>
        <span className={css.hint} data-testid="trajectory-diagnosis-findings-count">{countsLabel}</span>
      </div>
      <div className={css.findingList} data-testid="trajectory-diagnosis-finding-list">
        {items.map(item => (item.cause !== undefined ? (
          <CauseCard
            key={item.id}
            cause={item.cause}
            subjectLabels={subjectLabels}
            focused={focusedFindingId === item.id}
            onFocusFinding={onFocusFinding}
          />
        ) : (
          <EvolutionFindingCard
            key={item.id}
            judge={item.judge as EvolutionJudge}
            subjectLabels={subjectLabels}
            focused={focusedFindingId === item.id}
            onFocusFinding={onFocusFinding}
          />
        )))}
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
    </div>
  );
}

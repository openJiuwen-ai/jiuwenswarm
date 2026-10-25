/** Findings of a diagnosis: the deliverable failure causes, plus legacy root causes of older records. */

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { DiagnosisRecord, LegacyRootCause, RewardFailureCause, SignalRef } from './diagnosisTypes';
import {
  causeNodeLabel,
  evidenceNodeLabel,
  findingCounts,
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

export function SignalTags({ signal }: { signal: SignalRef | null | undefined }) {
  const { t } = useTranslation();
  if (signal === null || signal === undefined) return null;
  const tier = signalTier(signal);
  return (
    <>
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
      <span className={css.chip}>
        {t(`trajectory.diagnosis.signalSource.${signal.source}`, { defaultValue: signal.source })}
      </span>
    </>
  );
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
  return (
    <div className={css.cause} data-testid="trajectory-diagnosis-finding">
      <div className={css.causeHead}>
        <span className={css.badge} title={cause.dimension} data-testid="trajectory-diagnosis-dimension">
          {t('trajectory.diagnosis.dimension')}
        </span>
        <SignalTags signal={cause.signal} />
        {subjectLabels.size > 1 && subjectId !== '' ? (
          <span className={css.chip} data-testid="trajectory-diagnosis-cause-agent">
            {subjectLabels.get(subjectId) ?? subjectId}
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
      <CauseEvidence cause={cause} nodes={nodes} />
    </div>
  );
}

/** The evidence a new-interface finding stands on: its dimension, the surfaced nodes and the signal text. */
function CauseEvidence({ cause, nodes }: { cause: RewardFailureCause; nodes: string }) {
  const { t } = useTranslation();
  const text = signalEvidenceBody(cause.signal, cause.dimension);
  return (
    <details className={css.evidence} data-testid="trajectory-diagnosis-evidence">
      <summary>{t('trajectory.diagnosis.evidence')}</summary>
      <div className={css.evidenceBody}>
        <p className={css.hint} data-testid="trajectory-diagnosis-dimension-text">
          {t('trajectory.diagnosis.dimension')}：{cause.dimension}
        </p>
        {nodes === '' ? null : (
          <p className={css.hint}>{t('trajectory.diagnosis.signalSurfacedAt', { nodes })}</p>
        )}
        {text === '' ? null : <PlainText text={text} />}
      </div>
    </details>
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
  const causes = record.reward_failure_causes ?? [];
  const legacy = usesLegacyFindings(record) ? record.root_causes ?? [] : [];
  if (causes.length === 0 && legacy.length === 0) return null;
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
      {causes.map(cause => (
        <CauseCard
          key={cause.id}
          cause={cause}
          subjectLabels={subjectLabels}
          focused={focusedFindingId === cause.id}
          onFocusFinding={onFocusFinding}
        />
      ))}
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
  );
}

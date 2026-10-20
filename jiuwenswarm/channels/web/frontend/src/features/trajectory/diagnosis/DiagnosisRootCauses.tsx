/** Findings of a diagnosis: every root cause, the experience verdict included, drawn as one kind of card. */

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { DiagnosisRecord, RootCause } from './diagnosisTypes';
import {
  causeNodeLabel,
  findingCounts,
  findingEvidence,
  findingTag,
  orderFindings,
  showCategoryBadge,
} from './diagnosisView';
import { FoldedMarkdown, PlainText } from './DiagnosisText';
import css from './DiagnosisSubpage.module.css';

interface CauseListProps {
  record: DiagnosisRecord;
  subjectLabels: ReadonlyMap<string, string>;
  focusedCauseId: string | null;
  onFocusCause: (cause: RootCause | null) => void;
}

function HighlightButton({ cause, focused, onFocusCause }: {
  cause: RootCause;
  focused: boolean;
  onFocusCause: (cause: RootCause | null) => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      className={css.button}
      aria-pressed={focused}
      onClick={() => onFocusCause(focused ? null : cause)}
      data-testid="trajectory-diagnosis-highlight"
    >
      {t('trajectory.diagnosis.highlight')}
    </button>
  );
}

/** The lead tag: the experience verdict, or how the signal behind the finding fared. */
function LeadTag({ cause }: { cause: RootCause }) {
  const { t } = useTranslation();
  const tag = findingTag(cause);
  if (tag === null) return null;
  if (tag.kind === 'experience') {
    return (
      <span
        className={`${css.tag} ${css.tagExperience}`}
        title={t(`trajectory.diagnosis.ttseOutcome.${tag.outcome}`)}
        data-testid="trajectory-diagnosis-finding-tag"
      >
        {t('trajectory.diagnosis.ttseReady')}
      </span>
    );
  }
  if (tag.tier !== null) {
    return (
      <span className={`${css.tag} ${css[`tier_${tag.tier}`]}`} data-testid="trajectory-diagnosis-finding-tag">
        {t(`trajectory.diagnosis.signalTier.${tag.tier}`)}
      </span>
    );
  }
  if (tag.source !== null) {
    return (
      <span className={css.tag} data-testid="trajectory-diagnosis-finding-tag">
        {t(`trajectory.diagnosis.signalSource.${tag.source}`)}
      </span>
    );
  }
  return null;
}

/** The signal text the finding stands on, folded away: the requirement stays in view as the label. */
function EvidenceFold({ cause }: { cause: RootCause }) {
  const { t } = useTranslation();
  const evidence = findingEvidence(cause);
  if (evidence === null) return null;
  const surfaced = (cause.signal?.node_ids ?? []).join(', ');
  const label = evidence.requirement === ''
    ? t('trajectory.diagnosis.evidence')
    : `${t('trajectory.diagnosis.evidence')}：${evidence.requirement}`;
  if (evidence.text === '' && surfaced === '') {
    return <p className={css.hint} data-testid="trajectory-diagnosis-evidence">{label}</p>;
  }
  return (
    <details className={css.evidence} data-testid="trajectory-diagnosis-evidence">
      <summary>{label}</summary>
      <div className={css.evidenceBody}>
        {surfaced === '' ? null : (
          <p className={css.hint}>{t('trajectory.diagnosis.signalSurfacedAt', { nodes: surfaced })}</p>
        )}
        <PlainText text={evidence.text} />
      </div>
    </details>
  );
}

function FindingCard({ cause, subjectLabels, focused, onFocusCause }: {
  cause: RootCause;
  subjectLabels: ReadonlyMap<string, string>;
  focused: boolean;
  onFocusCause: (cause: RootCause | null) => void;
}) {
  const { t } = useTranslation();
  const nodes = causeNodeLabel(cause);
  const signal = cause.signal ?? null;
  return (
    <div className={css.cause} data-testid="trajectory-diagnosis-finding">
      <div className={css.causeHead}>
        <LeadTag cause={cause} />
        {signal?.category === undefined || signal.category === null || signal.category === '' ? null : (
          <span className={css.badge} data-testid="trajectory-diagnosis-signal-category">{signal.category}</span>
        )}
        {typeof signal?.score === 'number' ? (
          <span className={css.hint}>{t('trajectory.diagnosis.signalScore', { score: signal.score })}</span>
        ) : null}
        {showCategoryBadge(cause) ? (
          <span className={css.badge}>{t(`trajectory.diagnosis.category.${cause.category}`)}</span>
        ) : null}
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
        {cause.confidence === undefined || cause.confidence === null ? null : (
          <span className={css.hint}>
            {t('trajectory.diagnosis.confidence', { value: Math.round(cause.confidence * 100) })}
          </span>
        )}
        <span className={css.spacer} />
        <HighlightButton cause={cause} focused={focused} onFocusCause={onFocusCause} />
      </div>
      <FoldedMarkdown text={cause.summary} />
      <EvidenceFold cause={cause} />
    </div>
  );
}

/**
 * Everything the diagnosis found, one card per root cause with the same parts: a lead tag and where
 * it sits, the finding itself, and the signal it stands on. The experience verdict on the whole
 * stretch comes last, since it only arrives once attribution is done. The title counts problems the
 * way the headline does and counts the verdict apart. Nothing is drawn when there is none; the
 * headline already says so.
 */
export function RootCauses({ record, subjectLabels, focusedCauseId, onFocusCause }: CauseListProps) {
  const { t } = useTranslation();
  const findings = useMemo(() => orderFindings(record.root_causes), [record.root_causes]);
  const counts = useMemo(() => findingCounts(record.root_causes), [record.root_causes]);
  if (findings.length === 0) return null;
  const countKey = counts.issues === 0
    ? 'findingsExperienceOnly'
    : counts.experience === 0 ? 'findingsIssuesOnly' : 'findingsIssuesAndExperience';
  return (
    <div className={css.block} data-testid="trajectory-diagnosis-root-causes">
      <div className={css.row}>
        <h4 className={css.blockTitle}>{t('trajectory.diagnosis.rootCauses')}</h4>
        <span className={css.hint} data-testid="trajectory-diagnosis-findings-count">
          {t(`trajectory.diagnosis.${countKey}`, { issues: counts.issues, experience: counts.experience })}
        </span>
      </div>
      {findings.map(cause => (
        <FindingCard
          key={cause.id}
          cause={cause}
          subjectLabels={subjectLabels}
          focused={focusedCauseId === cause.id}
          onFocusCause={onFocusCause}
        />
      ))}
    </div>
  );
}

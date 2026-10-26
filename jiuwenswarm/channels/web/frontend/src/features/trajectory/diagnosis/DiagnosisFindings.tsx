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
        <span className={css.badge} title={cause.dimension} data-testid="trajectory-diagnosis-attribution">
          {t('trajectory.diagnosis.attributionLabel', {
            kind: t('trajectory.diagnosis.attributionKind.artifact'),
          })}
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

/** Read-only related signals of a judge: the same labels/body as the suggestion expansion. */
function FindingRelatedSignals({ judge }: { judge: EvolutionJudge }) {
  const { t } = useTranslation();
  const signals = judge.related_signals ?? [];
  if (signals.length === 0) return null;
  return (
    <details className={css.evidence} data-testid="trajectory-diagnosis-related-signals">
      <summary>{t('trajectory.diagnosis.relatedSignals')}</summary>
      <div className={`${css.evidenceBody} ${css.relatedSignals}`}>
        {signals.flatMap(signal => signal.entries.map((entry, index) => {
          const nodes = evidenceNodeLabel({ evidence: entry.refs });
          return (
            <div className={css.relatedSignal} key={`${signal.signal_id}:${index}`}>
              <div className={css.row}>
                <span className={css.badge}>
                  {t('trajectory.diagnosis.attributionLabel', {
                    kind: t(`trajectory.diagnosis.attributionKind.${judge.category}`),
                  })}
                </span>
                <SignalTags
                  signal={{
                    id: signal.signal_id,
                    source: signal.source,
                    tier: signal.tier,
                    category: signal.category,
                    score: signal.score,
                    node_ids: signal.node_ids,
                  }}
                />
                {nodes === '' ? null : (
                  <span className={css.hint}>{t('trajectory.diagnosis.causeAt', { nodes })}</span>
                )}
              </div>
              <FoldedMarkdown text={entry.text_md} />
            </div>
          );
        }))}
      </div>
    </details>
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
      <div className={css.causeHead}>
        <span className={css.badge} data-testid="trajectory-diagnosis-attribution">
          {t('trajectory.diagnosis.attributionLabel', {
            kind: t(`trajectory.diagnosis.attributionKind.${judge.category}`),
          })}
        </span>
        {!skill || judge.target === null || judge.target === undefined || judge.target === '' ? null : (
          <span className={css.badge} title={judge.target}>{judge.target}</span>
        )}
        {skill ? (
          <span className={css.tag} data-testid="trajectory-diagnosis-evolution-score">
            {t('trajectory.diagnosis.confidence', { value: Math.round(judge.score * 100) })}
          </span>
        ) : (
          <>
            {judge.outcome === null || judge.outcome === undefined ? null : (
              <span className={css.tag} data-testid="trajectory-diagnosis-evolution-outcome">
                {t(`trajectory.diagnosis.ttseOutcome.${judge.outcome}`)}
              </span>
            )}
            <span className={css.hint} data-testid="trajectory-diagnosis-evolution-score">
              {t('trajectory.diagnosis.signalScore', { score: judge.score })}
            </span>
          </>
        )}
        {subjectLabels.size > 1 && subjectId !== '' ? (
          <span className={css.chip}>{subjectLabels.get(subjectId) ?? subjectId}</span>
        ) : null}
        {nodes === '' ? null : (
          <span className={css.hint} data-testid="trajectory-diagnosis-cause-nodes">
            {t('trajectory.diagnosis.causeAt', { nodes })}
          </span>
        )}
        <span className={css.spacer} />
        <HighlightButton id={judge.id} focused={focused} onFocusFinding={onFocusFinding} />
      </div>
      <FoldedMarkdown text={judge.summary} />
      <FindingRelatedSignals judge={judge} />
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
  );
}

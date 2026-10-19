/** Root causes of a diagnosis: the TTSE experience verdict and the attribution causes grouped by signal. */

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { ttseRootCauses } from './diagnosisState';
import type { DiagnosisRecord, RootCause, SignalRef } from './diagnosisTypes';
import {
  attributionCauses,
  causeNodeLabel,
  groupCausesBySignal,
  showCategoryBadge,
  signalTier,
  splitRewardSignalText,
  ttseTier,
  type CauseGroup,
} from './diagnosisView';
import { FoldedMarkdown, PlainFold } from './DiagnosisText';
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

/** Where a signal came from and how it fared; rule signals carry no tier, score or category. */
function SignalHead({ signal }: { signal: SignalRef }) {
  const { t } = useTranslation();
  const tier = signalTier(signal);
  const sourceKey = ['keyword_tool', 'keyword_user', 'reward'].includes(signal.source) ? signal.source : null;
  const nodes = (signal.node_ids ?? []).join(', ');
  return (
    <div className={css.row}>
      {tier === null ? null : (
        <span className={`${css.tag} ${css[`tier_${tier}`]}`} data-testid="trajectory-diagnosis-signal-tier">
          {t(`trajectory.diagnosis.signalTier.${tier}`)}
        </span>
      )}
      {signal.category === undefined || signal.category === null || signal.category === '' ? null : (
        <span className={css.badge} data-testid="trajectory-diagnosis-signal-category">{signal.category}</span>
      )}
      {typeof signal.score === 'number' ? (
        <span className={css.hint}>{t('trajectory.diagnosis.signalScore', { score: signal.score })}</span>
      ) : null}
      {sourceKey === null || sourceKey === 'reward' ? null : (
        <span className={css.badge} data-testid="trajectory-diagnosis-signal-source">
          {t(`trajectory.diagnosis.signalSource.${sourceKey}`)}
        </span>
      )}
      {nodes === '' ? null : (
        <span className={css.hint}>{t('trajectory.diagnosis.signalSurfacedAt', { nodes })}</span>
      )}
    </div>
  );
}

function SignalBody({ group }: { group: CauseGroup }) {
  if (group.evidence === '') return null;
  if (group.signal?.source === 'reward') {
    const { requirement, comment } = splitRewardSignalText(group.evidence);
    return (
      <div className={css.signalText}>
        <strong>{requirement}</strong>
        {comment === '' ? null : <p className={css.hint}>{comment}</p>}
      </div>
    );
  }
  return <PlainFold text={group.evidence} />;
}

function CauseCard({ cause, subjectLabels, focused, onFocusCause }: {
  cause: RootCause;
  subjectLabels: ReadonlyMap<string, string>;
  focused: boolean;
  onFocusCause: (cause: RootCause | null) => void;
}) {
  const { t } = useTranslation();
  const nodes = causeNodeLabel(cause);
  return (
    <div className={css.cause} data-testid="trajectory-diagnosis-root-cause">
      <div className={css.causeHead}>
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
    </div>
  );
}

function CauseGroupView({ group, subjectLabels, focusedCauseId, onFocusCause }: {
  group: CauseGroup;
  subjectLabels: ReadonlyMap<string, string>;
  focusedCauseId: string | null;
  onFocusCause: (cause: RootCause | null) => void;
}) {
  const hasSignalHead = group.signal !== null || group.evidence !== '';
  return (
    <div className={css.causeGroup} data-testid="trajectory-diagnosis-cause-group">
      {hasSignalHead ? (
        <div className={css.signal} data-testid="trajectory-diagnosis-signal" data-source={group.signal?.source}>
          {group.signal === null ? null : <SignalHead signal={group.signal} />}
          <SignalBody group={group} />
        </div>
      ) : null}
      {group.causes.map(cause => (
        <CauseCard
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

/** The TTSE verdict on the diagnosed stretch, drawn like a root cause; the trigger lives in the suggestions card. */
function ExperienceCard({ cause, focused, onFocusCause }: {
  cause: RootCause;
  focused: boolean;
  onFocusCause: (cause: RootCause | null) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className={css.cause} data-testid="trajectory-diagnosis-experience-cause">
      <div className={css.causeHead}>
        <span className={`${css.tag} ${css[`tier_${ttseTier(cause.category)}`]}`}>
          {t('trajectory.diagnosis.experience')}
          {' · '}
          {t(`trajectory.diagnosis.ttseOutcome.${cause.category.slice('ttse_'.length)}`)}
        </span>
        <span className={css.spacer} />
        <HighlightButton cause={cause} focused={focused} onFocusCause={onFocusCause} />
      </div>
      {cause.summary === '' ? null : <PlainFold text={cause.summary} />}
    </div>
  );
}

/**
 * Everything the diagnosis found, in one list: the TTSE verdict on the whole stretch first, then the
 * attribution root causes grouped by the signal they answer. Nothing is drawn when there is none; the
 * headline already says so.
 */
export function RootCauses({ record, subjectLabels, focusedCauseId, onFocusCause }: CauseListProps) {
  const { t } = useTranslation();
  const groups = useMemo(() => groupCausesBySignal(attributionCauses(record)), [record]);
  const verdicts = useMemo(() => ttseRootCauses(record), [record]);
  if (groups.length === 0 && verdicts.length === 0) return null;
  return (
    <div className={css.block} data-testid="trajectory-diagnosis-root-causes">
      <h4 className={css.blockTitle}>{t('trajectory.diagnosis.rootCauses')}</h4>
      {verdicts.map(cause => (
        <ExperienceCard
          key={cause.id}
          cause={cause}
          focused={focusedCauseId === cause.id}
          onFocusCause={onFocusCause}
        />
      ))}
      {groups.map(group => (
        <CauseGroupView
          key={group.key}
          group={group}
          subjectLabels={subjectLabels}
          focusedCauseId={focusedCauseId}
          onFocusCause={onFocusCause}
        />
      ))}
    </div>
  );
}
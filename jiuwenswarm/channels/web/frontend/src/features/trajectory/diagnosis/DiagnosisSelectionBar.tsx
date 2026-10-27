/** Bottom bar of selection mode: scope, note and the run / cancel buttons. */

import { memo } from 'react';
import { useTranslation } from 'react-i18next';
import css from './DiagnosisSelectionBar.module.css';

export const DIAGNOSIS_NOTE_MAX_LENGTH = 2000;

export interface DiagnosisSelectionBarProps {
  fullScope: boolean;
  /** The session is running, so a whole-trajectory diagnosis is refused. */
  fullScopeBlocked: boolean;
  onFullScopeChange: (value: boolean) => void;
  note: string;
  onNoteChange: (value: string) => void;
  /** Agents in the pick; `0` when nothing is picked. */
  pickedAgents: number;
  /** OTel spans contained whole by the pick (start and end both covered). */
  pickedNodes: number;
  /** Shown when the pick contains the main agent's last turn. */
  lastTurnNotice: string | null;
  /** Shown when the pick contains more OTel spans than a diagnosis may cover. */
  nodeLimitNotice: string | null;
  /** Shown after Start when the pick contains no complete OTel span. */
  emptyNodesNotice: string | null;
  canStart: boolean;
  submitting: boolean;
  /** Why the last start failed. Stays until the next attempt. */
  error: string | null;
  onCancel: () => void;
  onStart: () => void;
}

export const DiagnosisSelectionBar = memo(function DiagnosisSelectionBar({
  fullScope,
  fullScopeBlocked,
  onFullScopeChange,
  note,
  onNoteChange,
  pickedAgents,
  pickedNodes,
  lastTurnNotice,
  nodeLimitNotice,
  emptyNodesNotice,
  canStart,
  submitting,
  error,
  onCancel,
  onStart,
}: DiagnosisSelectionBarProps) {
  const { t } = useTranslation();
  let status: string;
  if (fullScope && fullScopeBlocked) status = t('trajectory.diagnosis.fullScopeBusy');
  else if (fullScope || pickedAgents > 0) {
    status = t('trajectory.analysis.selection.summary', { count: pickedAgents, spans: pickedNodes });
  } else {
    status = t('trajectory.diagnosis.selectionHint');
  }
  const statusIsError = fullScope && fullScopeBlocked;
  return (
    <div className={css.bar} data-testid="trajectory-analysis-selection-bar">
      <div className={css.fields}>
        <label
          className={css.check}
          title={fullScopeBlocked ? t('trajectory.diagnosis.fullScopeBusy') : undefined}
        >
          <input
            type="checkbox"
            checked={fullScope}
            onChange={event => onFullScopeChange(event.target.checked)}
            data-testid="trajectory-analysis-full-scope"
          />
          <span>{t('trajectory.analysis.selection.fullScope')}</span>
        </label>
        <input
          type="text"
          className={css.note}
          value={note}
          maxLength={DIAGNOSIS_NOTE_MAX_LENGTH}
          placeholder={t('trajectory.analysis.selection.notePlaceholder')}
          aria-label={t('trajectory.diagnosis.noteLabel')}
          onChange={event => onNoteChange(event.target.value)}
          data-testid="trajectory-analysis-note"
        />
      </div>
      {nodeLimitNotice === null ? null : (
        <p className={css.warning} role="alert" data-testid="trajectory-analysis-selection-node-limit">
          {nodeLimitNotice}
        </p>
      )}
      {emptyNodesNotice === null ? null : (
        <p className={css.warning} role="alert" data-testid="trajectory-analysis-selection-empty-nodes">
          {emptyNodesNotice}
        </p>
      )}
      <div className={css.actions}>
        <span
          className={`${css.status} ${statusIsError ? css.statusError : ''}`}
          role="status"
          data-testid="trajectory-analysis-selection-status"
        >
          {status}
          {lastTurnNotice === null || statusIsError ? null : (
            <>
              {status === '' ? null : ' · '}
              <span className={css.lastTurnInline} data-testid="trajectory-analysis-selection-last-turn">
                {lastTurnNotice}
              </span>
            </>
          )}
        </span>
        {error === null ? null : (
          <span className={`${css.status} ${css.statusError}`} role="alert" data-testid="trajectory-analysis-selection-error">
            {error}
          </span>
        )}
        <button
          type="button"
          className={css.cancel}
          onClick={onCancel}
          data-testid="trajectory-analysis-selection-cancel"
        >
          {t('trajectory.analysis.selection.cancel')}
        </button>
        <button
          type="button"
          className={css.start}
          disabled={!canStart}
          onClick={onStart}
          data-testid="trajectory-analysis-selection-start"
        >
          {submitting ? t('trajectory.analysis.selection.starting') : t('trajectory.analysis.selection.start')}
        </button>
      </div>
    </div>
  );
});

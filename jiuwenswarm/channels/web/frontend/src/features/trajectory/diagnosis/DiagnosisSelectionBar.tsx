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
  canStart,
  submitting,
  error,
  onCancel,
  onStart,
}: DiagnosisSelectionBarProps) {
  const { t } = useTranslation();
  let status: string;
  if (fullScope) status = fullScopeBlocked ? t('trajectory.diagnosis.fullScopeBusy') : '';
  else if (pickedAgents === 0) status = t('trajectory.diagnosis.selectionHint');
  else status = t('trajectory.analysis.selection.summary', { count: pickedAgents });
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
      <div className={css.actions}>
        <span
          className={`${css.status} ${statusIsError ? css.statusError : ''}`}
          role="status"
          data-testid="trajectory-analysis-selection-status"
        >
          {status}
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

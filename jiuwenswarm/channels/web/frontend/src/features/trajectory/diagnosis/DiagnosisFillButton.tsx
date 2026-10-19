/** Button that puts a suggestion into the chat composer, asking first when the composer already has text. */

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import css from './DiagnosisSubpage.module.css';

const FILLED_HINT_MS = 3000;

export interface FillButtonProps {
  text: string;
  composerHasText: () => boolean;
  onFill: (text: string, mode: 'replace' | 'append') => void;
  testId: string;
}

export function FillButton({ text, composerHasText, onFill, testId }: FillButtonProps) {
  const { t } = useTranslation();
  const [state, setState] = useState<'idle' | 'confirm' | 'filled'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
  }, []);

  const fill = (mode: 'replace' | 'append') => {
    onFill(text, mode);
    setState('filled');
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState('idle'), FILLED_HINT_MS);
  };

  if (state === 'confirm') {
    return (
      <span className={css.row} role="group" aria-label={t('trajectory.diagnosis.fillConflict')}>
        <span className={css.hint}>{t('trajectory.diagnosis.fillConflict')}</span>
        <button type="button" className={css.button} onClick={() => fill('replace')} data-testid={`${testId}-replace`}>
          {t('trajectory.diagnosis.fillReplace')}
        </button>
        <button type="button" className={css.button} onClick={() => fill('append')} data-testid={`${testId}-append`}>
          {t('trajectory.diagnosis.fillAppend')}
        </button>
        <button type="button" className={css.button} onClick={() => setState('idle')}>
          {t('trajectory.diagnosis.fillCancel')}
        </button>
      </span>
    );
  }
  return (
    <span className={css.row}>
      <button
        type="button"
        className={css.button}
        onClick={() => (composerHasText() ? setState('confirm') : fill('replace'))}
        data-testid={testId}
      >
        {t('trajectory.diagnosis.fillInput')}
      </button>
      {state === 'filled' ? (
        <span className={css.hint} role="status" data-testid="trajectory-diagnosis-filled">
          {t('trajectory.diagnosis.filled')}
        </span>
      ) : null}
    </span>
  );
}

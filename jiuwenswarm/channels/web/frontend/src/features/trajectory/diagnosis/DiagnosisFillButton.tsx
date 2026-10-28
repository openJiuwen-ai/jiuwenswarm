/** Button that puts a suggestion into the chat composer, replacing whatever is there. */

import { useEffect, useRef, useState } from 'react';
import { Check } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import css from './DiagnosisSubpage.module.css';

const FILLED_HINT_MS = 3000;

export interface FillButtonProps {
  text: string;
  onFill: (text: string) => void;
  testId: string;
  /** Visible label. Defaults to the single-suggestion "fill input" wording. */
  label?: string;
  disabled?: boolean;
}

export function FillButton({ text, onFill, testId, label, disabled = false }: FillButtonProps) {
  const { t } = useTranslation();
  const [filled, setFilled] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
  }, []);

  const fill = () => {
    onFill(text);
    setFilled(true);
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setFilled(false), FILLED_HINT_MS);
  };

  return (
    <span className={css.row}>
      {filled ? (
        <span className={`${css.hint} ${css.filledHint}`} role="status" data-testid="trajectory-diagnosis-filled">
          <Check size={12} strokeWidth={2.5} aria-hidden />
          {t('trajectory.diagnosis.filled')}
        </span>
      ) : null}
      <button type="button" className={css.actionButton} onClick={fill} disabled={disabled} data-testid={testId}>
        {label ?? t('trajectory.diagnosis.fillInput')}
      </button>
    </span>
  );
}

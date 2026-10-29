import { TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { JiuwenMateDeleteMode } from '../jiuwenmateApi';
import { JiuwenMateDialog } from './JiuwenMateDialog';

const MODES: readonly JiuwenMateDeleteMode[] = ['unbind', 'purge'];

interface ColleagueDeleteDialogProps {
  name: string;
  onCancel: () => void;
  onConfirm: (mode: JiuwenMateDeleteMode) => void;
}

/** Deleting a colleague either keeps every connection's data (unbind) or removes all of it (purge). */
export function ColleagueDeleteDialog({ name, onCancel, onConfirm }: ColleagueDeleteDialogProps) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<JiuwenMateDeleteMode>('unbind');
  return (
    <JiuwenMateDialog
      title={t('jiuwenmate.office.delete')}
      width={520}
      onClose={onCancel}
      testId="jiuwenmate-delete-dialog"
      footer={
        <>
          <button type="button" className="jiuwenmate-outline-button" onClick={onCancel} data-testid="jiuwenmate-delete-cancel">
            {t('jiuwenmate.common.cancel')}
          </button>
          <button
            type="button"
            className="jiuwenmate-dark-button"
            onClick={() => onConfirm(mode)}
            data-testid="jiuwenmate-delete-confirm"
            data-variant={mode}
          >
            {t('jiuwenmate.common.confirm')}
          </button>
        </>
      }
    >
      <p className="jiuwenmate-dialog__message">
        {t('jiuwenmate.office.deleteConfirm')}
        <strong>{name}</strong>
      </p>
      <div className="jiuwenmate-delete-dialog__modes" role="radiogroup" aria-label={t('jiuwenmate.office.delete')}>
        {MODES.map((item) => (
          <label key={item} className="jiuwenmate-delete-dialog__mode" data-testid="jiuwenmate-delete-mode" data-variant={item}>
            <input
              type="radio"
              name="jiuwenmate-delete-mode"
              value={item}
              checked={mode === item}
              onChange={() => setMode(item)}
              data-testid="jiuwenmate-delete-mode-input"
              data-variant={item}
            />
            <span>
              <strong data-testid="jiuwenmate-delete-mode-label" data-variant={item}>
                {t(`jiuwenmate.office.deleteModes.${item}`)}
              </strong>
              <small data-testid="jiuwenmate-delete-mode-hint" data-variant={item}>
                {t(`jiuwenmate.office.deleteModes.${item}Hint`)}
              </small>
            </span>
          </label>
        ))}
      </div>
      {mode === 'purge' ? (
        <p className="jiuwenmate-dialog__warning" data-testid="jiuwenmate-delete-purge-warning">
          <TriangleAlert aria-hidden />
          <span>{t('jiuwenmate.office.deleteWarning')}</span>
        </p>
      ) : null}
    </JiuwenMateDialog>
  );
}

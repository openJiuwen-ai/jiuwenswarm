import { Send } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import customDefault from '../../../assets/jiuwenmate/bees/custom-default.png';
import sparkleIcon from '../../../assets/jiuwenmate/icons/sparkle.svg';
import { JIUWENMATE_CUSTOM_SUGGESTIONS } from '../mockData';
import { JiuwenMateDialog } from './JiuwenMateDialog';

interface CustomCreateDialogProps {
  onCancel: () => void;
  onConfirm: (basics: { name: string; description: string }) => void;
  onOpenPlaza: () => void;
}

export function CustomCreateDialog({ onCancel, onConfirm, onOpenPlaza }: CustomCreateDialogProps) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const canConfirm = name.trim().length > 0 && description.trim().length > 0;

  return (
    <JiuwenMateDialog
      title={t('jiuwenmate.create.customTitle')}
      width={560}
      onClose={onCancel}
      testId="jiuwenmate-custom-create-dialog"
      footer={
        <>
          <button type="button" className="jiuwenmate-link-button" onClick={onOpenPlaza}>
            {t('jiuwenmate.create.goPlaza')}
          </button>
          <span className="jiuwenmate-dialog__footer-spacer" />
          <button type="button" className="jiuwenmate-outline-button" onClick={onCancel}>
            {t('jiuwenmate.common.cancel')}
          </button>
          <button
            type="button"
            className="jiuwenmate-dark-button"
            disabled={!canConfirm}
            onClick={() => onConfirm({ name: name.trim(), description: description.trim() })}
            data-testid="jiuwenmate-custom-create-confirm"
          >
            {t('jiuwenmate.common.confirm')}
          </button>
        </>
      }
    >
      <div className="jiuwenmate-avatar-row">
        <span className="jiuwenmate-avatar jiuwenmate-avatar--medium">
          <img src={customDefault} alt="" />
        </span>
        <span className="jiuwenmate-avatar-row__generate" aria-hidden>
          <img src={sparkleIcon} alt="" />
        </span>
        <small>{t('jiuwenmate.create.avatarHint')}</small>
      </div>
      <label className="jiuwenmate-field">
        <span>{t('jiuwenmate.create.dialogName')}</span>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={t('jiuwenmate.create.dialogNamePlaceholder')}
          data-testid="jiuwenmate-custom-name-input"
        />
      </label>
      <label className="jiuwenmate-field">
        <span>{t('jiuwenmate.create.dialogDuty')}</span>
        <textarea
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder={t('jiuwenmate.create.dialogDutyPlaceholder')}
          rows={4}
          data-testid="jiuwenmate-custom-duty-input"
        />
      </label>
      <div className="jiuwenmate-suggestions">
        {JIUWENMATE_CUSTOM_SUGGESTIONS.map((suggestion) => (
          <button
            key={suggestion.name}
            type="button"
            onClick={() => {
              setName(suggestion.name);
              setDescription(suggestion.description);
            }}
          >
            <span className="jiuwenmate-avatar jiuwenmate-avatar--tiny">
              <img src={customDefault} alt="" />
            </span>
            <strong>{suggestion.name}</strong>
            <span>{suggestion.description}</span>
            <Send aria-hidden />
          </button>
        ))}
      </div>
    </JiuwenMateDialog>
  );
}

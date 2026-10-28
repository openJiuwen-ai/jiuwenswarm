import { Send } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import customDefault from '../../../assets/jiuwenmate/bees/custom-default.png';
import { pickAvatarHue } from '../avatarHue';
import { customAvatarHue } from '../colleaguePortraits';
import { JIUWENMATE_CUSTOM_SUGGESTIONS } from '../mockData';
import { JiuwenMateDialog } from './JiuwenMateDialog';

interface CustomCreateDialogProps {
  usedHues?: readonly number[];
  onCancel: () => void;
  onConfirm: (basics: { name: string; description: string; avatarHue: number }) => void;
  onOpenPlaza: () => void;
}

export function CustomCreateDialog({ usedHues = [], onCancel, onConfirm, onOpenPlaza }: CustomCreateDialogProps) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [avatarHue] = useState(() => pickAvatarHue(usedHues));
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
            onClick={() => onConfirm({ name: name.trim(), description: description.trim(), avatarHue })}
            data-testid="jiuwenmate-custom-create-confirm"
          >
            {t('jiuwenmate.common.confirm')}
          </button>
        </>
      }
    >
      <div className="jiuwenmate-avatar-row">
        <span
          className="jiuwenmate-avatar jiuwenmate-avatar--medium"
          data-hue={avatarHue}
          style={{ ['--jm-avatar-hue' as string]: `${avatarHue}deg` }}
        >
          <img src={customDefault} alt="" />
        </span>
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
        {JIUWENMATE_CUSTOM_SUGGESTIONS.map((suggestion) => {
          const suggestionHue = customAvatarHue({ id: suggestion.name, category: 'custom' });
          return (
            <button
              key={suggestion.name}
              type="button"
              onClick={() => {
                setName(suggestion.name);
                setDescription(suggestion.description);
              }}
            >
              <span
                className="jiuwenmate-avatar jiuwenmate-avatar--tiny"
                data-hue={suggestionHue ?? undefined}
                style={suggestionHue === null ? undefined : { ['--jm-avatar-hue' as string]: `${suggestionHue}deg` }}
              >
                <img src={customDefault} alt="" />
              </span>
              <strong>{suggestion.name}</strong>
              <span>{suggestion.description}</span>
              <Send aria-hidden />
            </button>
          );
        })}
      </div>
    </JiuwenMateDialog>
  );
}

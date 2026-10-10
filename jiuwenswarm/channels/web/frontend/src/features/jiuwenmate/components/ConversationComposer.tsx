import { ChevronDown, Plus, Send, ShieldUser } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import autoIcon from '../../../assets/jiuwenmate/icons/auto.svg';

interface ConversationComposerProps {
  value: string;
  onChange: (value: string) => void;
}

export function ConversationComposer({ value, onChange }: ConversationComposerProps) {
  const { t } = useTranslation();
  return (
    <div className="jiuwenmate-composer" data-testid="jiuwenmate-conversation-composer">
      <textarea
        rows={2}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={t('jiuwenmate.workspace.composerPlaceholder')}
        data-testid="jiuwenmate-composer-input"
      />
      <div className="jiuwenmate-composer__toolbar">
        <span className="jiuwenmate-composer__left">
          <button type="button" aria-label={t('jiuwenmate.workspace.attach')}>
            <Plus aria-hidden />
          </button>
          <button type="button" className="jiuwenmate-composer__mode">
            <ShieldUser aria-hidden />
            <span>{t('jiuwenmate.workspace.defaultPermission')}</span>
            <ChevronDown aria-hidden />
          </button>
        </span>
        <span className="jiuwenmate-composer__right">
          <span className="jiuwenmate-composer__usage" aria-hidden />
          <button type="button" className="jiuwenmate-composer__auto">
            <img src={autoIcon} alt="" aria-hidden />
            <span>{t('jiuwenmate.workspace.autoMode')}</span>
          </button>
          <button
            type="button"
            className="jiuwenmate-composer__send"
            aria-label={t('jiuwenmate.workspace.send')}
            disabled
          >
            <Send aria-hidden />
          </button>
        </span>
      </div>
    </div>
  );
}

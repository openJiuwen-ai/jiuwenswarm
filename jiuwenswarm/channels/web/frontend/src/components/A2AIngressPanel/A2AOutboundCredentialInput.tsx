import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Eye, EyeOff } from 'lucide-react';
import { ConfigFieldHintLabel } from '../ConfigPanel/ConfigFieldHintLabel';

import { describeA2AOutboundAuthentication } from './a2aOutboundPanelState';

export function A2AOutboundCredentialInput({
  card,
  value,
  onChange,
  disabled,
}: {
  card: Record<string, unknown>;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const id = useId();
  const [visible, setVisible] = useState(false);
  const method = describeA2AOutboundAuthentication(card, t);
  const help = `${t('a2aIngress.outbound.credentialHelp.method', { method })}\n${t('a2aIngress.outbound.credentialHelp.value')}`;
  return (
    <div>
      <ConfigFieldHintLabel
        label={<label htmlFor={id}>{t('a2aIngress.outbound.fields.credential')}</label>}
        help={help}
        className="text-xs font-medium text-text-muted"
      />
      <div className="relative mt-2">
        <input
          id={id}
          type={visible ? 'text' : 'password'}
          className="w-full rounded-md border border-border bg-bg px-3 py-2 pr-10 text-sm text-text outline-none focus:border-accent disabled:opacity-50"
          value={value}
          onChange={event => onChange(event.target.value)}
          autoComplete="new-password"
          spellCheck={false}
          disabled={disabled}
          placeholder={t('a2aIngress.security.credentialPlaceholder')}
        />
        <button
          type="button"
          className="absolute inset-y-0 right-0 flex w-9 items-center justify-center rounded-r-md text-text-muted transition-colors hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50"
          aria-label={t(visible ? 'a2aIngress.security.hide' : 'a2aIngress.security.show')}
          title={t(visible ? 'a2aIngress.security.hide' : 'a2aIngress.security.show')}
          aria-pressed={visible}
          disabled={disabled}
          onClick={() => setVisible(current => !current)}
        >
          {visible ? <EyeOff size={16} strokeWidth={1.8} aria-hidden="true" /> : <Eye size={16} strokeWidth={1.8} aria-hidden="true" />}
        </button>
      </div>
    </div>
  );
}

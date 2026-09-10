import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Eye, EyeOff } from 'lucide-react';
import { ConfigFieldHintLabel } from '../ConfigPanel/ConfigFieldHintLabel';
import { DEFAULT_API_KEY_HEADER, isValidA2AIngressApiKeyHeader, type A2AIngressDraft } from './a2aIngressPanelState';

export function A2AIngressSecurityFields({
  draft,
  disabled,
  onChange,
}: {
  draft: A2AIngressDraft;
  disabled: boolean;
  onChange: <K extends keyof A2AIngressDraft>(field: K, value: A2AIngressDraft[K]) => void;
}) {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(false);
  const credentialInputId = useId();
  const inputClass = 'w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-accent';
  const generate = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    onChange('credential', Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join(''));
    onChange('clear_credential', false);
  };
  return (
    <fieldset className="mt-5 rounded-lg border border-border p-4" disabled={disabled}>
      <legend className="px-2 text-sm font-semibold text-text">
        <ConfigFieldHintLabel label={t('a2aIngress.security.title')} help={t('a2aIngress.security.description')} />
      </legend>
      <div className="grid gap-4 md:grid-cols-2">
        <label>
          <span className="text-xs font-medium text-text-muted">{t('a2aIngress.fields.auth_type')}</span>
          <span className="relative mt-2 block">
            <select
              className={`${inputClass} appearance-none pr-10 transition-colors hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-50`}
              value={draft.auth_type}
              onChange={event => {
                const nextType = event.target.value as A2AIngressDraft['auth_type'];
                onChange('auth_type', nextType);
                if (nextType === 'none') onChange('card_auth_required', false);
                else onChange('clear_credential', false);
                if (nextType !== 'api_key' && !isValidA2AIngressApiKeyHeader(draft.api_key_header)) {
                  onChange('api_key_header', DEFAULT_API_KEY_HEADER);
                }
              }}
            >
              {(['none', 'bearer', 'api_key'] as const).map(type => (
                <option key={type} value={type}>
                  {t(`a2aIngress.security.types.${type}`)}
                </option>
              ))}
            </select>
            <ChevronDown size={16} className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-text-muted" aria-hidden="true" />
          </span>
        </label>
        {draft.auth_type === 'api_key' && (
          <label>
            <span className="text-xs font-medium text-text-muted">{t('a2aIngress.fields.api_key_header')}</span>
            <input
              className={`mt-2 ${inputClass}`}
              value={draft.api_key_header}
              onChange={event => onChange('api_key_header', event.target.value)}
              spellCheck={false}
            />
          </label>
        )}
        <div className="md:col-span-2">
          <div className="flex items-center gap-2">
            <ConfigFieldHintLabel
              label={<label htmlFor={credentialInputId}>{t('a2aIngress.fields.credential')}</label>}
              help={t('a2aIngress.security.credentialHint')}
              className="text-xs font-medium text-text-muted"
            />
            <span className="text-xs text-text-muted">
              {t(draft.credential_configured ? 'a2aIngress.security.configured' : 'a2aIngress.security.notConfigured')}
            </span>
          </div>
          <div className="mt-2 flex items-stretch gap-2">
            <div className="relative min-w-0 flex-1">
              <input
                id={credentialInputId}
                className={`${inputClass} block h-full pr-10`}
                type={visible ? 'text' : 'password'}
                value={draft.credential}
                onChange={event => onChange('credential', event.target.value)}
                autoComplete="new-password"
                spellCheck={false}
                maxLength={512}
                disabled={disabled || draft.clear_credential}
                placeholder={t('a2aIngress.security.credentialPlaceholder')}
              />
              <button
                type="button"
                className="absolute inset-y-0 right-0 flex w-9 items-center justify-center rounded-r-md text-text-muted transition-colors hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50"
                aria-label={t(visible ? 'a2aIngress.security.hide' : 'a2aIngress.security.show')}
                title={t(visible ? 'a2aIngress.security.hide' : 'a2aIngress.security.show')}
                aria-pressed={visible}
                disabled={disabled || draft.clear_credential}
                onClick={() => setVisible(value => !value)}
              >
                {visible ? <EyeOff size={16} strokeWidth={1.8} aria-hidden="true" /> : <Eye size={16} strokeWidth={1.8} aria-hidden="true" />}
              </button>
            </div>
            <button type="button" className="btn secondary shrink-0 whitespace-nowrap" onClick={generate}>
              {t('a2aIngress.security.generate')}
            </button>
          </div>
        </div>
        <label className="flex items-center gap-3 md:col-span-2">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--color-accent)]"
            checked={draft.clear_credential}
            disabled={disabled || draft.auth_type !== 'none' || !draft.credential_configured}
            onChange={event => {
              onChange('clear_credential', event.target.checked);
              if (event.target.checked) onChange('credential', '');
            }}
          />
          <span className="text-sm text-text">{t('a2aIngress.security.clear')}</span>
        </label>
        <div className="md:col-span-2">
          <ConfigFieldHintLabel
            label={
              <label className="flex items-center gap-3">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-[var(--color-accent)]"
                  checked={!draft.card_auth_required}
                  disabled={disabled || draft.auth_type === 'none'}
                  onChange={event => onChange('card_auth_required', !event.target.checked)}
                />
                <span className="text-sm text-text">{t('a2aIngress.fields.card_auth_required')}</span>
              </label>
            }
            help={t('a2aIngress.security.cardHint')}
          />
        </div>
        <label className="flex items-center gap-3 md:col-span-2">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--color-accent)]"
            checked={draft.expose_reasoning}
            onChange={event => onChange('expose_reasoning', event.target.checked)}
          />
          <span className="text-sm text-text">{t('a2aIngress.fields.expose_reasoning')}</span>
        </label>
      </div>
    </fieldset>
  );
}

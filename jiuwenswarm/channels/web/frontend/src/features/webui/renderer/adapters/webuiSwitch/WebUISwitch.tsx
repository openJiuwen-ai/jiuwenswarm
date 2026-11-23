import { useEffect, useRef, useState } from 'react';
import { Switch, type SwitchProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { useTranslation } from 'react-i18next';
import type { PreferenceBinding } from '../../../catalog/components/appearance.ts';
import { useClientPreference, writePreference } from '../../../client/preferences.ts';
import type { LocalizedText } from '../../../catalog/components/localizedText.ts';
import { useLocalizedText } from '../../useLocalizedText.ts';

export function WebUISwitch({ node, context, emit, appearance }: WebUIAdapterProps) {
  const { t } = useTranslation();
  const preference = node.props.preference as PreferenceBinding | undefined;
  const preferred = useClientPreference(context.packageId, preference);
  const [saveError, setSaveError] = useState(false);
  const preferenceSaved = useRef(true);
  const p = node.props as Pick<
    SwitchProps,
    'checked' | 'defaultChecked' | 'disabled' | 'loading' | 'size' | 'checkedChildren' | 'unCheckedChildren'
  > & { ariaLabel?: LocalizedText };
  const ariaLabel = useLocalizedText(p.ariaLabel);
  const [checked, setChecked] = useState(p.checked ?? p.defaultChecked ?? false);
  useEffect(() => {
    if (p.checked !== undefined) setChecked(p.checked);
  }, [p.checked]);
  const disabled = p.disabled || p.loading || !context.connected;
  return (
    <>
      <Switch
        data-testid="webui-switch"
        aria-label={ariaLabel}
        checked={preference ? preferred : checked}
        disabled={disabled}
        className={appearance?.className}
        style={appearance?.style}
        loading={p.loading}
        size={p.size}
        checkedChildren={p.checkedChildren}
        unCheckedChildren={p.unCheckedChildren}
        onChange={(next) => {
          if (disabled) return;
          if (preference) {
            preferenceSaved.current = false;
            try {
              writePreference(context.packageId, preference, next);
              preferenceSaved.current = true;
              setSaveError(false);
            } catch {
              setSaveError(true);
              return;
            }
          }
          setChecked(next);
          if (node.events['ui.change']) void emit('ui.change', { checked: next });
        }}
        onClick={(next) => {
          if (node.events['ui.click'] && !disabled && (!preference || preferenceSaved.current))
            void emit('ui.click', { checked: next });
        }}
      />
      {saveError && (
        <span role="alert" data-testid="webui-preference-save-error" style={{ color: 'var(--color-feedback-danger)' }}>
          {t('settingsPanel.feedback.saveFailed')}
        </span>
      )}
    </>
  );
}

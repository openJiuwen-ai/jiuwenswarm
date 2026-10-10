import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Select, Tag, type TagVariant } from '../../../../components/ui';
import { SettingRow } from '../../components';
import { useSettingsServices } from '../../services/SettingsServicesProvider';

export function ConnectionStatusSetting() {
  const { t } = useTranslation();
  const { connectionState } = useSettingsServices();
  const connectionKey =
    connectionState === 'ready'
      ? 'connected'
      : connectionState === 'connecting' || connectionState === 'reconnecting'
        ? 'connecting'
        : 'disconnected';
  const connectionVariant: TagVariant =
    connectionKey === 'connected' ? 'success' : connectionKey === 'connecting' ? 'warning' : 'danger';
  return (
    <>
      <DesktopCloseBehaviorSetting />
      <SettingRow
        title={t('settingsPanel.general.connection')}
        description={t('settingsPanel.general.connectionDescription')}
        meta={
          <Tag
            variant={connectionVariant}
            role="status"
            data-testid="settings-connection-status-tag"
            data-variant={connectionKey}
          >
            {t(`settingsPanel.general.connectionStatus.${connectionKey}`)}
          </Tag>
        }
      />
    </>
  );
}

type CloseAction = 'ask' | 'hide' | 'quit';

function isCloseAction(value: unknown): value is CloseAction {
  return value === 'ask' || value === 'hide' || value === 'quit';
}

function closeActionApi() {
  const api = window.pywebview?.api;
  if (!api?.get_close_action || !api.set_close_action) return null;
  return { get: api.get_close_action, set: api.set_close_action };
}

function DesktopCloseBehaviorSetting() {
  const { t } = useTranslation();
  const [action, setAction] = useState<CloseAction>('ask');
  const [available, setAvailable] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const load = () => {
      const api = closeActionApi();
      if (!api) return;
      void Promise.resolve(api.get())
        .then((loaded) => {
          setAvailable(isCloseAction(loaded));
          if (isCloseAction(loaded)) setAction(loaded);
        })
        .catch(() => setAvailable(false));
    };
    load();
    window.addEventListener('pywebviewready', load);
    return () => window.removeEventListener('pywebviewready', load);
  }, []);

  if (!available) return null;
  return (
    <SettingRow
      title={t('settingsPanel.general.closeBehavior')}
      description={t('settingsPanel.general.closeBehaviorDescription')}
    >
      <Select
        aria-label={t('settingsPanel.general.closeBehavior')}
        value={action}
        disabled={saving}
        options={(
          [
            ['ask', t('settingsPanel.general.closeBehaviorOptions.ask')],
            ['hide', t('settingsPanel.general.closeBehaviorOptions.hide')],
            ['quit', t('settingsPanel.general.closeBehaviorOptions.quit')],
          ] as const
        ).map(([value, label]) => ({ value, label }))}
        onChange={(next) => {
          const api = closeActionApi();
          if (!api) return;
          const previous = action;
          const selected = next as CloseAction;
          setAction(selected);
          setSaving(true);
          void Promise.resolve(api.set(selected))
            .then((saved) => {
              if (!saved) setAction(previous);
            })
            .catch(() => setAction(previous))
            .finally(() => setSaving(false));
        }}
        data-testid="settings-desktop-close-behavior"
      />
    </SettingRow>
  );
}

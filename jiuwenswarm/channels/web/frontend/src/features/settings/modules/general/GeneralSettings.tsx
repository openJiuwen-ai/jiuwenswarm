import { useTranslation } from 'react-i18next';
import { Tag, type TagVariant } from '../../../../components/ui';
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
    <SettingRow
      title={t('settingsPanel.general.connection')}
      description={t('settingsPanel.general.connectionDescription')}
      meta={
        <Tag variant={connectionVariant} role="status">
          {t(`settingsPanel.general.connectionStatus.${connectionKey}`)}
        </Tag>
      }
    />
  );
}

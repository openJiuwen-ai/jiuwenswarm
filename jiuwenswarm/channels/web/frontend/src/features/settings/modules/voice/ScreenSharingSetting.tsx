import { useTranslation } from 'react-i18next';
import { Switch } from '../../../../components/ui';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import {
  setTaskDuplexScreenSharingEnabled,
  useTaskDuplexScreenSharingEnabled,
} from '../../../taskFullDuplex/mediaPreferences';

export function ScreenSharingSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const enabled = useTaskDuplexScreenSharingEnabled();
  return (
    <SettingRow
      title={t('settingsPanel.voice.screenSharing')}
      description={t('settingsPanel.voice.screenSharingDescription')}
    >
      <Switch
        checked={enabled}
        disabled={disabled}
        onChange={setTaskDuplexScreenSharingEnabled}
        aria-label={t('settingsPanel.voice.screenSharing')}
        data-testid="settings-voice-screen-sharing-switch"
      />
    </SettingRow>
  );
}

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SettingsChannelsPanel } from './SettingsChannelsPanel';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useUnsavedChanges } from '../../services/useUnsavedChanges';

export function ChannelsModule() {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const [hasChanges, setHasChanges] = useState(false);
  useUnsavedChanges('channels', hasChanges);
  return (
    <SettingsChannelsPanel
      isConnected={isConnected}
      discardConfirmMessage={t('settingsPanel.dialog.discardConfirm')}
      onHasChangesChange={setHasChanges}
    />
  );
}

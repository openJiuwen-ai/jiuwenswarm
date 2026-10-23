import { SettingsSection } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { SettingsSourceProvider } from '../../services/SettingsSourceProvider';
import { JevKeySettings } from '../browser/JevKeySettings';

export function JevSettings({ disabled }: SettingsCustomItemProps) {
  return (
    <div data-testid="settings-models-jev-panel">
      <SettingsSection separatedRows>
        <SettingsSourceProvider source="browser">
          <JevKeySettings disabled={disabled} />
        </SettingsSourceProvider>
      </SettingsSection>
    </div>
  );
}

import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { SystemPluginRecoverySettings } from './SystemPluginRecoverySettings';

export const systemPluginRecoveryModule: SettingsModuleDefinition = {
  id: 'systemPluginRecovery',
  titleKey: 'settingsPanel.categories.systemPluginRecovery',
  hideHeader: true,
  icon: settingsNavigationIcons.systemPluginRecovery,
  sections: [
    {
      id: 'system-plugin-recovery',
      items: [{ id: 'system-plugin-recovery-panel', component: 'custom', render: SystemPluginRecoverySettings }],
    },
  ],
};

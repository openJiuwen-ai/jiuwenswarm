import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';

export const memoryModule: SettingsModuleDefinition = {
  id: 'memory',
  titleKey: 'settingsPanel.categories.memory',
  icon: settingsNavigationIcons.memory,
  source: 'config',
  sections: [
    {
      id: 'memory-switches',
      items: [{ id: 'memory-portal-enabled', component: 'switch', key: 'memory_portal_enabled' }],
    },
  ],
};

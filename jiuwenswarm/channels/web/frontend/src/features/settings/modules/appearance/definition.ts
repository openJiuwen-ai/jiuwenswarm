import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { AppearanceSettingsPanel } from './AppearanceSettings';

export const appearanceModule: SettingsModuleDefinition = {
  id: 'appearance',
  titleKey: 'settingsPanel.categories.appearance',
  icon: settingsNavigationIcons.appearance,
  sections: [
    {
      id: 'appearance',
      items: [
        {
          id: 'appearance-preferences',
          component: 'custom',
          render: AppearanceSettingsPanel,
        },
      ],
    },
  ],
};

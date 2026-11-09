import { Puzzle } from 'lucide-react';
import type { SettingsModuleDefinition } from '../../registry/types';
import { SplashProviderSettings } from './SplashProviderSettings';

/**
 * Host features for Gateway extensions: the startup splash order (Gateway S_10). Extensions add their own
 * settings through the shared settings catalog (AgentServer S_18), usually as a menu after this module.
 */
export const extensionsModule: SettingsModuleDefinition = {
  id: 'extensions',
  titleKey: 'settingsPanel.categories.extensions',
  descriptionKey: 'settingsPanel.extensions.description',
  icon: Puzzle,
  sections: [
    {
      id: 'extensions-splash',
      titleKey: 'settingsPanel.extensions.splash.title',
      descriptionKey: 'settingsPanel.extensions.splash.description',
      items: [{ id: 'extensions-splash-order', component: 'custom', render: SplashProviderSettings }],
    },
  ],
};

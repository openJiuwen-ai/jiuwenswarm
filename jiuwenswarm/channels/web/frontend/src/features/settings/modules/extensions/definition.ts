import { Puzzle } from 'lucide-react';
import type { SettingsModuleDefinition } from '../../registry/types';
import { ExtensionSettingsPages } from './ExtensionSettingsPages';
import { SplashProviderSettings } from './SplashProviderSettings';

/** Host-owned startup splash order and the settings pages of Gateway extensions (Gateway S_10). */
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
    {
      id: 'extensions-pages',
      titleKey: 'settingsPanel.extensions.pages.title',
      separatedRows: true,
      items: [{ id: 'extensions-pages', component: 'custom', render: ExtensionSettingsPages }],
    },
  ],
};

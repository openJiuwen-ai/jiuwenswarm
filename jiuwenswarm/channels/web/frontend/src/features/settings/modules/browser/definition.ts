import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { JevKeySettings } from './JevKeySettings';

export const browserModule: SettingsModuleDefinition = {
  id: 'browser',
  titleKey: 'settingsPanel.categories.browser',
  icon: settingsNavigationIcons.browser,
  source: 'browser',
  sections: [
    {
      id: 'browser-runtime',
      items: [
        { id: 'browser-path', component: 'input', key: 'chrome_path' },
        {
          id: 'browser-run-mode',
          component: 'select',
          key: 'headless',
          options: [
            { value: false, labelKey: 'settingsPanel.browser.headed' },
            { value: true, labelKey: 'settingsPanel.browser.headless' },
          ],
        },
        {
          id: 'browser-decision-mode',
          component: 'select',
          key: 'decision_mode',
          options: [
            { value: 'llm', labelKey: 'settingsPanel.browser.decisionLlm' },
            { value: 'shadow', labelKey: 'settingsPanel.browser.decisionShadow' },
            { value: 'hybrid', labelKey: 'settingsPanel.browser.decisionHybrid' },
          ],
        },
        {
          id: 'browser-decision-provider',
          component: 'select',
          key: 'decision_provider',
          options: [
            { value: 'openrouter', labelKey: 'settingsPanel.browser.providerOpenrouter' },
            { value: 'typesafe', labelKey: 'settingsPanel.browser.providerTypesafe' },
          ],
        },
        { id: 'browser-jev-key', component: 'custom', render: JevKeySettings },
      ],
    },
  ],
};

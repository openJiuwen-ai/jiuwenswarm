import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';

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
          id: 'browser-type',
          component: 'select',
          key: 'browser_type',
          options: [
            { value: 'auto', labelKey: 'browser.browserTypeAuto' },
            { value: 'chrome', labelKey: 'browser.browserTypeChrome' },
            { value: 'msedge', labelKey: 'browser.browserTypeEdge' },
          ],
        },
        {
          id: 'browser-run-mode',
          component: 'select',
          key: 'headless',
          options: [
            { value: false, labelKey: 'settingsPanel.browser.headed' },
            { value: true, labelKey: 'settingsPanel.browser.headless' },
          ],
        },
      ],
    },
  ],
};

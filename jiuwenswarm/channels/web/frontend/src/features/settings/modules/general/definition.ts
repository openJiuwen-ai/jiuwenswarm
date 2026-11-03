import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { ConnectionStatusSetting } from './GeneralSettings';
import { TopicModelConfiguration, TopicModelSummary } from './TopicModelSettings';

export const generalModule: SettingsModuleDefinition = {
  id: 'general',
  titleKey: 'settingsPanel.categories.general',
  icon: settingsNavigationIcons.general,
  source: 'locale',
  sections: [
    {
      id: 'general',
      items: [
        {
          id: 'preferred-language',
          component: 'select',
          key: 'preferred_language',
          options: [
            { value: 'zh', labelKey: 'settingsPanel.general.chinese' },
            { value: 'en', labelKey: 'settingsPanel.general.english' },
          ],
        },
        { id: 'connection-status', component: 'custom', render: ConnectionStatusSetting },
        {
          id: 'topic-recommendation-enabled', component: 'switch', key: 'topic_recommendation_enabled', source: 'config',
          rowClassName: 'settings-agent-media__row',
          configure: TopicModelConfiguration,
          subItems: { show: 'always', disabled: 'never', items: [{ id: 'topic-model', component: 'custom', render: TopicModelSummary }] },
        },
      ],
    },
  ],
};

import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { RecommendationTopicSettings } from './RecommendationSettings';

/** 话题推荐与提示词推荐，排在「知识」之后。 */
export const recommendationModule: SettingsModuleDefinition = {
  id: 'recommendation',
  titleKey: 'settingsPanel.categories.recommendation',
  icon: settingsNavigationIcons.recommendation,
  source: 'config',
  sections: [
    {
      id: 'recommendation',
      separatedRows: true,
      items: [
        { id: 'topic-recommendation-enabled', component: 'switch', key: 'topic_recommendation_enabled' },
        { id: 'prompt-suggestion-enabled', component: 'switch', key: 'prompt_suggestion_enabled' },
        { id: 'recommendation-topic-settings', component: 'custom', render: RecommendationTopicSettings },
      ],
    },
  ],
};

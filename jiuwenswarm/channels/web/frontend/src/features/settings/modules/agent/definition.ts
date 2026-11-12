import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { AgentSearchSettings } from './AgentSettings';
import { multimodalToolItems, MultimodalSettingsFeedback } from './MultimodalToolSettings';
import { ExternalCliSettingsItem } from './ExternalCliAgentsSettings';

export const agentModule: SettingsModuleDefinition = {
  id: 'agent',
  titleKey: 'settingsPanel.categories.agent',
  icon: settingsNavigationIcons.agent,
  source: 'config',
  sections: [
    {
      id: 'skills',
      titleKey: 'settingsPanel.agent.skills',
      items: [
        { id: 'skill-evolution', component: 'switch', key: 'skill_evolution' },
        { id: 'ttse-enabled', component: 'switch', key: 'ttse_enabled' },
        {
          id: 'skill-retrieval',
          component: 'switch',
          key: 'skill_retrieval_enabled',
        },
      ],
    },
    {
      id: 'web-search',
      titleKey: 'settingsPanel.agent.webSearch',
      items: [
        { id: 'duckduckgo-search', component: 'switch', key: 'free_search_ddg_enabled' },
        { id: 'bing-search', component: 'switch', key: 'free_search_bing_enabled' },
        { id: 'search-credentials', component: 'custom', render: AgentSearchSettings },
      ],
    },
    {
      id: 'media-tools',
      titleKey: 'settingsPanel.agent.mediaTools',
      items: [
        { id: 'multimodal-feedback', component: 'custom', render: MultimodalSettingsFeedback },
        ...multimodalToolItems('vision'),
        ...multimodalToolItems('audio'),
        ...multimodalToolItems('video'),
        ...multimodalToolItems('video_gen'),
        ...multimodalToolItems('visual_gen'),
      ],
    },
    {
      id: 'external-cli-agents',
      titleKey: 'settingsPanel.agent.externalCliAgents',
      items: [
        { id: 'external-cli-agents', component: 'custom', render: ExternalCliSettingsItem },
      ],
    },
  ],
};

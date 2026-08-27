import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { AgentMediaSettings, AgentSearchSettings } from './AgentSettings';

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
        { id: 'skill-retrieval', component: 'switch', key: 'skill_retrieval_enabled' },
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
      items: [{ id: 'media-tools-settings', component: 'custom', render: AgentMediaSettings }],
    },
    {
      id: 'team',
      titleKey: 'settingsPanel.agent.team',
      items: [{ id: 'swarmflow', component: 'switch', key: 'swarmflow_enabled' }],
    },
  ],
};

import { createElement } from 'react';
import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsTabDefinition } from '../../components';
import type { SettingsModuleDefinition } from '../../registry/types';
import { ModelsSettings, type ModelSettingsTabId } from './ModelsSettings';

export const modelSettingsTabs = [
  { id: 'models', labelKey: 'settingsPanel.models.modelsTab' },
  { id: 'groups', labelKey: 'settingsPanel.models.modelGroups' },
] as const satisfies readonly SettingsTabDefinition<ModelSettingsTabId>[];

export const defaultModelSettingsTab: ModelSettingsTabId = 'models';

function ModelsSettingsModule() {
  return createElement(ModelsSettings, {
    tabs: modelSettingsTabs,
    defaultTab: defaultModelSettingsTab,
  });
}

export const modelsModule: SettingsModuleDefinition = {
  id: 'models',
  titleKey: 'settingsPanel.categories.models',
  icon: settingsNavigationIcons.models,
  source: 'config',
  sections: [
    {
      id: 'model-manager',
      separatedRows: true,
      items: [{ id: 'model-manager', component: 'custom', render: ModelsSettingsModule }],
    },
  ],
};

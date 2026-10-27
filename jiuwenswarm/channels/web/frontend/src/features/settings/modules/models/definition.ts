import { createElement } from 'react';
import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsTabDefinition } from '../../components';
import type { SettingsCustomItemProps, SettingsModuleDefinition } from '../../registry/types';
import { ModelsSettings, type ModelSettingsTabId } from './ModelsSettings';

export const modelSettingsTabs = [
  { id: 'models', labelKey: 'settingsPanel.models.modelsTab' },
  { id: 'groups', labelKey: 'settingsPanel.models.modelGroups' },
  { id: 'jev', labelKey: 'settingsPanel.models.jevTab', helpKey: 'settingsPanel.models.jevTabHelp' },
] as const satisfies readonly SettingsTabDefinition<ModelSettingsTabId>[];

export const defaultModelSettingsTab: ModelSettingsTabId = 'models';

function ModelsSettingsModule({ disabled }: SettingsCustomItemProps) {
  return createElement(ModelsSettings, {
    tabs: modelSettingsTabs,
    defaultTab: defaultModelSettingsTab,
    disabled,
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

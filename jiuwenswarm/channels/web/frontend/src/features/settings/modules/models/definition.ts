import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { ModelsSettings } from './ModelsSettings';

export const modelsModule: SettingsModuleDefinition = {
  id: 'models',
  titleKey: 'settingsPanel.categories.models',
  icon: settingsNavigationIcons.models,
  source: 'config',
  sections: [
    {
      id: 'model-manager',
      separatedRows: true,
      items: [{ id: 'model-manager', component: 'custom', render: ModelsSettings }],
    },
    {
      id: 'free-models',
      titleKey: 'settingsPanel.models.freeModels',
      items: [{ id: 'enable-free-models', component: 'switch', key: 'enable_free_models' }],
    },
  ],
};

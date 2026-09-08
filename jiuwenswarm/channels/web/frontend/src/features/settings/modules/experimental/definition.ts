import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import {
  A2UISetting,
  ExternalCliSettingsItem,
  ProactiveLimitsSetting,
  TaskFullDuplexSetting,
  TrajectoryUiSetting,
} from './ExperimentalSettings';

export const experimentalModule: SettingsModuleDefinition = {
  id: 'experimental',
  titleKey: 'settingsPanel.categories.experimental',
  icon: settingsNavigationIcons.experimental,
  source: 'config',
  sections: [
    {
      id: 'asr',
      titleKey: 'settingsPanel.experimental.taskAsr',
      items: [
        { id: 'asr-api-base', component: 'input', key: 'asr_api_base' },
        { id: 'asr-api-key', component: 'input', key: 'asr_api_key', inputType: 'password' },
        { id: 'asr-model', component: 'input', key: 'asr_model' },
      ],
    },
    {
      id: 'task-full-duplex',
      titleKey: 'settingsPanel.experimental.taskFullDuplex',
      items: [{ id: 'task-full-duplex-enabled', component: 'custom', render: TaskFullDuplexSetting }],
    },
    {
      id: 'external-cli-agents',
      titleKey: 'settingsPanel.experimental.externalCliAgents',
      items: [{ id: 'external-cli-agents', component: 'custom', render: ExternalCliSettingsItem }],
    },
    {
      id: 'a2ui',
      titleKey: 'settingsPanel.experimental.a2ui',
      items: [{ id: 'a2ui', component: 'custom', render: A2UISetting }],
    },
    {
      id: 'trajectory-ui',
      titleKey: 'settingsPanel.experimental.trajectoryUi',
      items: [{ id: 'trajectory-ui-enabled', component: 'custom', render: TrajectoryUiSetting }],
    },
    {
      id: 'proactive-recommendation',
      titleKey: 'settingsPanel.experimental.proactive',
      items: [
        {
          id: 'proactive-recommendation',
          component: 'switch',
          key: 'proactive_recommendation_enabled',
          subItems: {
            show: 'always',
            disabled: 'when-parent-unchecked',
            items: [{ id: 'proactive-limits', component: 'custom', render: ProactiveLimitsSetting }],
          },
        },
      ],
    },
  ],
};

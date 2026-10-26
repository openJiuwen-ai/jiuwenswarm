import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import { PersonalContextSettingsModule } from './PersonalContextSettingsModule';

/**
 * 个人上下文设置模块——排在「频道」模块之后。
 * 仿 channels 模块：单 section + 单 custom item，render 指向自绘面板。
 */
export const personalContextSettingsModule: SettingsModuleDefinition = {
  id: 'personalContext',
  titleKey: 'settingsPanel.categories.personalContext',
  icon: settingsNavigationIcons.personalContext,
  sections: [
    {
      id: 'personalContext',
      separatedRows: true,
      items: [{ id: 'personalContext-panel', component: 'custom', render: PersonalContextSettingsModule }],
    },
  ],
};

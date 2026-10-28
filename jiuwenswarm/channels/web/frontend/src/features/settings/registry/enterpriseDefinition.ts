import { createSettingsPageDefinition } from './createSettingsPageDefinition';
import { openSourceSettingsAccessPolicy } from './accessPolicy';
import { generalModule } from '../modules/general';
import { ConnectionStatusSetting, VersionSetting } from '../modules/general/GeneralSettings';
import { archivedTasksModule } from '../modules/archivedTasks';

export const enterpriseSettingsPageDefinition = createSettingsPageDefinition({
  id: 'enterprise-settings',
  compositionMode: 'base',
  accessPolicy: openSourceSettingsAccessPolicy,
  modules: [
    {
      ...generalModule,
      sections: [
        {
          id: 'general',
          items: [
            { id: 'app-version', component: 'custom', render: VersionSetting },
            { id: 'connection-status', component: 'custom', render: ConnectionStatusSetting },
            ...generalModule.sections[0].items.filter((item) => item.id === 'preferred-language'),
          ],
        },
      ],
    },
    // 企业版用户前端此前只挂了精简「通用」；归档成功后需能在设置里查看/恢复。
    archivedTasksModule,
  ],
});

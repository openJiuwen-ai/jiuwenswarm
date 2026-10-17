import { Archive } from 'lucide-react';
import type { SettingsModuleDefinition } from '../../registry/types';
import { ArchivedTasksSettingsModule } from './ArchivedTasksSettings';

/** 已归档的任务——排在「个人上下文」之后、「实验功能」之前。 */
export const archivedTasksModule: SettingsModuleDefinition = {
  id: 'archivedTasks',
  titleKey: 'settingsPanel.categories.archivedTasks',
  icon: Archive,
  sections: [
    {
      id: 'archivedTasks',
      separatedRows: true,
      items: [{ id: 'archivedTasks-panel', component: 'custom', render: ArchivedTasksSettingsModule }],
    },
  ],
};

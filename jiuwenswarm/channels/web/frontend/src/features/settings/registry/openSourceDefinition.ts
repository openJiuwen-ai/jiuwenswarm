import { createSettingsPageDefinition } from './createSettingsPageDefinition';
import { openSourceSettingsAccessPolicy } from './accessPolicy';
import { memoryModule } from '../modules/memory/definition';
import { generalModule } from '../modules/general';
import { modelsModule } from '../modules/models';
import { agentModule } from '../modules/agent';
import { browserModule } from '../modules/browser';
import { channelsModule } from '../modules/channels';
import { personalContextSettingsModule } from '../modules/personalContext';
import { archivedTasksModule } from '../modules/archivedTasks';
import { voiceModule } from '../modules/voice/definition';
import { experimentalModule } from '../modules/experimental';
import { appearanceModule } from '../modules/appearance';
import { systemPluginRecoveryModule } from '../modules/systemPluginRecovery';

export const openSourceSettingsPageDefinition = createSettingsPageDefinition({
  id: 'open-source-settings',
  compositionMode: 'base',
  accessPolicy: openSourceSettingsAccessPolicy,
  modules: [
    generalModule,
    modelsModule,
    voiceModule,
    agentModule,
    memoryModule,
    browserModule,
    channelsModule,
    personalContextSettingsModule,
    archivedTasksModule,
    experimentalModule,
    appearanceModule,
    systemPluginRecoveryModule,
  ],
});

export const settingsPageDefinition = openSourceSettingsPageDefinition;

import { AudioWaveform } from 'lucide-react';
import type { SettingsModuleDefinition } from '../../registry/types';
import { TaskAsrSetting, TaskFullDuplexSetting } from '../experimental/ExperimentalSettings';
import { ScreenSharingSetting } from './ScreenSharingSetting';

export const voiceModule: SettingsModuleDefinition = {
  id: 'voice',
  titleKey: 'settingsPanel.categories.voice',
  descriptionKey: 'settingsPanel.voice.description',
  icon: AudioWaveform,
  source: 'config',
  sections: [
    {
      id: 'voice',
      items: [
        { id: 'task-full-duplex-enabled', component: 'custom', render: TaskFullDuplexSetting },
        { id: 'screen-sharing', component: 'custom', render: ScreenSharingSetting },
      ],
    },
    {
      id: 'asr',
      titleKey: 'settingsPanel.experimental.taskAsr',
      items: [{ id: 'task-asr-enabled', component: 'custom', render: TaskAsrSetting }],
    },
  ],
};

import type { SettingsAccessPolicy } from './types';

export const openSourceSettingsAccessPolicy: SettingsAccessPolicy = {
  evaluate() {
    return { level: 'editable' };
  },
};

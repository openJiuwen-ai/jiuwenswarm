import type { SettingsAccessPolicy } from './types';

export const openSourceSettingsAccessPolicy: SettingsAccessPolicy = {
  evaluate(node) {
    if (node.kind === 'section' && node.moduleId === 'experimental' && node.sectionId === 'kv-cache-affinity') {
      return { level: 'hidden' };
    }
    return { level: 'editable' };
  },
};

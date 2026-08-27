import type { SettingsAccessPolicy } from './types';

export const openSourceSettingsAccessPolicy: SettingsAccessPolicy = { evaluate: () => ({ level: 'editable' }) };

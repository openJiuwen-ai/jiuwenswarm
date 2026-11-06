import { getVoiceInputRoutingMode, setVoiceInputRoutingMode } from '../../voice/routingMode';
import type { SettingValue } from '../registry/types';

export const LOCAL_SETTING_COMPONENTS = new Map<string, 'select'>([['voice_input_routing', 'select']]);

export function readLocalSettings(): Record<string, SettingValue> {
  return { voice_input_routing: getVoiceInputRoutingMode() };
}

export function saveLocalSettings(updates: Record<string, SettingValue>): void {
  for (const [key, value] of Object.entries(updates)) {
    if (key !== 'voice_input_routing' || (value !== 'leader' && value !== 'none')) {
      throw new Error(`Unsupported local setting: ${key}`);
    }
  }
  if (updates.voice_input_routing !== undefined) {
    setVoiceInputRoutingMode(updates.voice_input_routing as 'leader' | 'none');
  }
}

import type { SettingItemDefinition, SettingValue } from '../../registry/types';

export const personalContextCapacitySetting: SettingItemDefinition = {
  id: 'personal-context-capacity',
  component: 'input',
  source: 'personalContext',
  key: 'max_context_size_mb',
  errorDisplay: 'inline',
  inputMode: 'numeric',
};

export function parseContextCapacity(value: SettingValue): number | null {
  if (typeof value === 'boolean' || !/^\d+$/.test(String(value).trim())) return null;
  const size = Number(value);
  return Number.isInteger(size) && size >= 10 && size <= 10000 ? size : null;
}

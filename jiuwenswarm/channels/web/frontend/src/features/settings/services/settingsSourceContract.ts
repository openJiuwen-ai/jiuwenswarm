import type { SettingValue, SettingsSource } from '../registry/types';
import { SETTINGS_CONFIG_FIELD_BY_KEY } from './settingsContract';

type SimpleSettingComponent = 'switch' | 'select' | 'input';

const BROWSER_SETTING_COMPONENTS = new Map<string, SimpleSettingComponent>([
  ['chrome_path', 'input'],
  ['browser_type', 'select'],
  ['headless', 'select'],
]);
const LOCALE_SETTING_COMPONENTS = new Map<string, SimpleSettingComponent>([['preferred_language', 'select']]);

export function isSettingsSource(value: unknown): value is SettingsSource {
  return value === 'config' || value === 'browser' || value === 'locale';
}

export function isSettingsSourceKey(source: SettingsSource, key: string): boolean {
  if (source === 'config') return SETTINGS_CONFIG_FIELD_BY_KEY.has(key);
  if (source === 'browser') return BROWSER_SETTING_COMPONENTS.has(key);
  if (source === 'locale') return LOCALE_SETTING_COMPONENTS.has(key);
  return false;
}

export function isSettingsSourceComponent(
  source: SettingsSource,
  key: string,
  component: SimpleSettingComponent,
): boolean {
  if (source === 'browser') return BROWSER_SETTING_COMPONENTS.get(key) === component;
  if (source === 'locale') return LOCALE_SETTING_COMPONENTS.get(key) === component;
  const field = SETTINGS_CONFIG_FIELD_BY_KEY.get(key);
  if (!field) return false;
  if (component === 'switch') return field.kind === 'boolean';
  if (component === 'input') return field.kind === 'text';
  return true;
}

export function serializeConfigSettingValue(value: SettingValue): string {
  return typeof value === 'string' ? value : String(value);
}

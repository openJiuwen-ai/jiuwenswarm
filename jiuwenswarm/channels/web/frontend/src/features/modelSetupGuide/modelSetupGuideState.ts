const ENABLED_VALUES = new Set(['true', '1', 'yes', 'on']);
const DISABLED_VALUES = new Set(['false', '0', 'no', 'off']);

/**
 * Missing configuration is enabled by default so existing installations receive
 * the setup guide once after upgrading.
 */
export function isSetupGuideEnabled(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value !== 'string') return true;

  const normalized = value.trim().toLowerCase();
  if (ENABLED_VALUES.has(normalized)) return true;
  if (DISABLED_VALUES.has(normalized)) return false;
  return true;
}

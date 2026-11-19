import type { VendorPreset } from '../../../../types';
import type { ModelTranslate } from './modelTranslation';

const VENDOR_TRANSLATION_KEYS: Record<string, string> = {
  alibaba: 'alibaba',
  baidu: 'baidu',
  deepseek: 'deepseek',
  kimi: 'kimi',
  maas: 'maas',
  minimax: 'minimax',
  mimo: 'mimo',
  'opencode-go': 'opencode-go',
  openrouter: 'openrouter',
  'vllm-omni': 'vllm-omni',
  volcengine: 'volcengine',
  zhipu: 'zhipu',
};

export function getVendorLabel(vendorKey: string, t: ModelTranslate): string {
  const translationKey = VENDOR_TRANSLATION_KEYS[vendorKey];
  return translationKey
    ? t(`settingsPanel.models.vendors.${translationKey}`)
    : t('settingsPanel.models.vendors.unrecognized', { vendor: vendorKey });
}

export function getPresetLabel(preset: VendorPreset, t: ModelTranslate): string {
  return getVendorLabel(preset.vendor_key, t);
}

/**
 * Placeholder for the address field of a preset that leaves the address to the
 * user (``api_base === ""``).
 *
 * The generic hint names api.openai.com, which is a wrong example for a vendor
 * that wanted its own address filled in. Vendors with a documented address get
 * a vendor-specific hint; anything else keeps the generic one, so a newly
 * added preset is no worse off than before this helper existed.
 */
export function getPresetApiBasePlaceholder(
  vendorKey: string,
  t: ModelTranslate,
  fallbackKey = 'settingsPanel.fields.api_base.placeholder',
): string {
  const translationKey = VENDOR_TRANSLATION_KEYS[vendorKey];
  if (!translationKey) return t(fallbackKey);
  const specificKey = `settingsPanel.models.apiBasePlaceholders.${translationKey}`;
  const specific = t(specificKey, { defaultValue: '' });
  return specific || t(fallbackKey);
}

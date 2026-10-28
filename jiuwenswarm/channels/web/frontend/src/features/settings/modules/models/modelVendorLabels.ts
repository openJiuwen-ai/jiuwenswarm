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

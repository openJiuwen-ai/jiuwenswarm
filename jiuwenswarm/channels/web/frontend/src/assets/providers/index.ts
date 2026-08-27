import openAIModelIcon from './openai.svg';
import customModelIcon from '../settings/models/custom.svg';

const PROVIDER_ICONS_PNG = import.meta.glob<string>('./*.png', {
  eager: true,
  import: 'default',
});

const PROVIDER_ICONS_SVG = import.meta.glob<string>('./*.svg', {
  eager: true,
  import: 'default',
});

const VENDOR_ICON_KEYS = new Map<string, string>([
  ['alibaba', 'qwen'],
  ['baidu', 'baidu'],
  ['deepseek', 'deepseek'],
  ['kimi', 'kimi'],
  ['maas', 'pangu'],
  ['minimax', 'minimax'],
  ['mimo', 'mimo'],
  ['openrouter', 'openrouter'],
  ['volcengine', 'doubao'],
  ['zhipu', 'zhipu'],
]);

export function getProviderLogoUrl(iconKey: string): string | undefined {
  const normalizedKey = iconKey.trim().toLowerCase();
  if (!normalizedKey) return undefined;

  return PROVIDER_ICONS_SVG[`./${normalizedKey}.svg`] ?? PROVIDER_ICONS_PNG[`./${normalizedKey}.png`];
}

export function getVendorLogoUrl(vendorKey: string): string | undefined {
  const iconKey = VENDOR_ICON_KEYS.get(vendorKey);
  return iconKey ? getProviderLogoUrl(iconKey) : undefined;
}

export type ModelLogoIdentity = {
  model_provider?: string;
  vendor_key?: string;
  is_free?: boolean;
};

/**
 * 与模型设置页一致的图标分类规则：免费模型和未绑定厂商预设的模型都是自定义模型；
 * OpenAI 账号使用 OpenAI 图标；只有明确携带 vendor_key 的厂商模型才使用厂商图标。
 */
export function getModelLogoUrl(model: ModelLogoIdentity): string {
  if (model.is_free === true) return customModelIcon;
  if (model.model_provider === 'OpenAIAccount') return openAIModelIcon;

  const vendorKey = model.vendor_key?.trim();
  return vendorKey ? getVendorLogoUrl(vendorKey) ?? customModelIcon : customModelIcon;
}

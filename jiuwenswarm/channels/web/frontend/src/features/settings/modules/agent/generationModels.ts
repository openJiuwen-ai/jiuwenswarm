// 视频生成 / 图像生成 这两个"生成类"能力的模型目录。
//
// 生成类能力用的是专门的模型（不是对话模型），所以 vendors.list 里各厂商
// 预设的 model_options（都是对话模型）不适用，这里按"厂商 → 协议 → 模型"单独
// 维护每种能力可选的模型。
//
// "协议"决定后端用哪种接口去调用，同时也是模型所属的家族：
// - OpenRouter：协议是模型 ID 里 "/" 前面的那一段（bytedance/seedance-2.0-fast 的
//   协议是 bytedance，google/gemini-3.1-flash-image 的协议是 google），走
//   OpenRouter 的 /videos 和 chat/completions（modalities=image,text）。模型 ID
//   取自 OpenRouter 公开的 /api/v1/models?output_modalities=video|image，并且只收录
//   当前生成工具真正能驱动的那部分（视频不含编辑/放大类；图像只收录同时输出
//   图片和文字的模型）。
// - ModelArk（BytePlus / 火山引擎 Ark）：协议固定为 modelark，走 Ark 自己的接口
//   （Seedream → /images/generations；Seedance → /contents/generations/tasks），后端由
//   gen_toolkits.py 处理。模型 ID 取自 Ark 的 /api/v3/models（域名分区域：国际
//   ark.ap-southeast.bytepluses.com / 国内 ark.cn-beijing.volces.com），模型需要在
//   Ark 控制台里先开通，否则调用会返回 ModelNotOpen。
// - MiniMax：协议固定为 minimax，走 MiniMax 自己的原生接口
//   （image-01 → /v1/image_generation；MiniMax-H3 → /v2/video_generation），后端由
//   gen_toolkits.py 处理。MiniMax 的密钥分区域（国际 api.minimax.io / 国内
//   api.minimaxi.com），所以这个厂商的 API 地址需要用户能改。

export type GenerationSlot = 'video_gen' | 'visual_gen';

export type GenerationModel = { model: string; protocol: string };

export const MINIMAX_PROTOCOL = 'minimax';
export const MODELARK_PROTOCOL = 'modelark';

const OPENROUTER_MODELS: Record<GenerationSlot, readonly string[]> = {
  video_gen: [
    'bytedance/seedance-2.0-fast',
    'bytedance/seedance-2.0',
    'bytedance/seedance-2.0-mini',
    'bytedance/seedance-2.5',
    'bytedance/seedance-1-5-pro',
    'google/veo-3.1',
    'google/veo-3.1-fast',
    'google/veo-3.1-lite',
    'openai/sora-2-pro',
    'kwaivgi/kling-v3.0-pro',
    'kwaivgi/kling-v3.0-std',
    'kwaivgi/kling-video-o1',
    'alibaba/wan-3.0-prime',
    'alibaba/wan-3.0',
    'alibaba/wan-2.7',
    'alibaba/wan-2.6',
    'alibaba/happyhorse-1.1',
    'alibaba/happyhorse-1.0',
    'minimax/hailuo-3-max',
    'minimax/hailuo-3',
    'minimax/hailuo-2.3',
    'x-ai/grok-imagine-video-1.5',
    'x-ai/grok-imagine-video',
    'runway/gen-4.5',
    'black-forest-labs/flux-3-video',
  ],
  visual_gen: [
    'google/gemini-3.1-flash-image',
    'google/gemini-3.1-flash-image-preview',
    'google/gemini-3.1-flash-lite-image',
    'google/gemini-3-pro-image',
    'google/gemini-3-pro-image-preview',
    'google/gemini-2.5-flash-image',
    'openai/gpt-5.4-image-2',
    'openai/gpt-5-image',
    'openai/gpt-5-image-mini',
  ],
};

const MINIMAX_MODELS: Record<GenerationSlot, readonly string[]> = {
  video_gen: ['MiniMax-H3'],
  visual_gen: ['image-01'],
};

const MODELARK_MODELS: Record<GenerationSlot, readonly string[]> = {
  video_gen: [
    'dreamina-seedance-2-5-260628',
    'dreamina-seedance-2-0-260128',
    'dreamina-seedance-2-0-fast-260128',
    'dreamina-seedance-2-0-mini-260615',
    'seedance-1-0-pro-250528',
    'seedance-1-0-pro-fast-251015',
  ],
  visual_gen: [
    'dola-seedream-5-0-pro-260628',
    'dola-seedream-5-0-flash-260915',
    'seedream-5-0-260128',
    'seedream-4-5-251128',
    'seedream-4-0-250828',
  ],
};

function openRouterModelProtocol(modelId: string): string {
  const slash = modelId.indexOf('/');
  return slash > 0 ? modelId.slice(0, slash) : '';
}

const CATALOG: Record<'openrouter' | 'minimax' | 'modelark', Record<GenerationSlot, readonly GenerationModel[]>> = {
  openrouter: {
    video_gen: OPENROUTER_MODELS.video_gen.map((model) => ({ model, protocol: openRouterModelProtocol(model) })),
    visual_gen: OPENROUTER_MODELS.visual_gen.map((model) => ({ model, protocol: openRouterModelProtocol(model) })),
  },
  minimax: {
    video_gen: MINIMAX_MODELS.video_gen.map((model) => ({ model, protocol: MINIMAX_PROTOCOL })),
    visual_gen: MINIMAX_MODELS.visual_gen.map((model) => ({ model, protocol: MINIMAX_PROTOCOL })),
  },
  modelark: {
    video_gen: MODELARK_MODELS.video_gen.map((model) => ({ model, protocol: MODELARK_PROTOCOL })),
    visual_gen: MODELARK_MODELS.visual_gen.map((model) => ({ model, protocol: MODELARK_PROTOCOL })),
  },
};

export type GenerationVendor = keyof typeof CATALOG;

export const DEFAULT_GENERATION_MODEL: Record<GenerationVendor, Record<GenerationSlot, string>> = {
  openrouter: { video_gen: 'bytedance/seedance-2.0-fast', visual_gen: 'google/gemini-3.1-flash-image' },
  minimax: { video_gen: 'MiniMax-H3', visual_gen: 'image-01' },
  modelark: { video_gen: 'dreamina-seedance-2-5-260628', visual_gen: 'dola-seedream-5-0-pro-260628' },
};

/** 这些厂商的密钥分区域，API 地址需要能改（选完厂商后填入预设地址，用户可改成另一区域）。 */
const REGIONAL_VENDORS: readonly string[] = ['minimax', 'volcengine'];

export function isRegionalVendor(vendorKey: string | undefined): boolean {
  return !!vendorKey && REGIONAL_VENDORS.includes(vendorKey);
}

// 后端只接受固定的 provider 名称（ProviderType，如 OpenAI / OpenRouter / MiniMax /
// VolcEngine），不能直接存厂商的展示名（"火山引擎"）或 "Custom"，否则保存时会被
// "Model provider must in: [...]" 拒绝。
const PROVIDER_NAMES: Record<string, string> = {
  openrouter: 'OpenRouter',
  minimax: 'MiniMax',
  volcengine: 'VolcEngine',
};

/** 保存到 *_provider 的值：已知厂商用对应的 ProviderType；其余用预设的 client_provider，
 *  自定义地址按 OpenAI 兼容处理（和 图片处理 一致）。 */
export function generationProviderName(vendorKey: string | undefined, clientProvider?: string): string {
  return (vendorKey && PROVIDER_NAMES[vendorKey]) || clientProvider || 'OpenAI';
}

/** 区域提示文案的 i18n key（不同厂商的区域地址不同）。 */
export function regionalHintKey(vendorKey: string | undefined): string {
  return vendorKey === 'volcengine' ? 'settingsPanel.agent.regionalBaseHintModelark' : 'settingsPanel.agent.regionalBaseHint';
}

const HOST = /^https?:\/\/([^/:]+)/i;

/** 由厂商预设（或自定义地址的域名）判断用哪份目录；都不是则没有专属目录。 */
export function generationVendor(vendorKey: string | undefined, apiBase: string): GenerationVendor | undefined {
  if (vendorKey === 'openrouter' || vendorKey === 'minimax') return vendorKey;
  if (vendorKey === 'volcengine') return 'modelark';
  const host = (HOST.exec(apiBase.trim())?.[1] ?? '').toLowerCase();
  if (/(^|\.)openrouter\.ai$/.test(host)) return 'openrouter';
  if (/(^|\.)minimaxi?\.(io|com)$/.test(host)) return 'minimax';
  if (/^ark\.[\w-]+\.(bytepluses\.com|volces\.com)$/.test(host)) return 'modelark';
  return undefined;
}

/** vendor 为 undefined 且 all=true（自定义地址）时，给出全部厂商的模型作为候选。 */
export function catalogModels(slot: GenerationSlot, vendor: GenerationVendor | undefined, all = false): GenerationModel[] {
  if (vendor) return [...CATALOG[vendor][slot]];
  return all ? Object.values(CATALOG).flatMap((byslot) => byslot[slot]) : [];
}

export function catalogProtocols(slot: GenerationSlot, vendor: GenerationVendor | undefined, all: boolean): string[] {
  const own = catalogModels(slot, vendor, all);
  const source = own.length > 0 ? own : catalogModels(slot, undefined, true);
  return Array.from(new Set(source.map((entry) => entry.protocol).filter(Boolean)));
}

/** 已经保存下来的值不在目录里时，也要出现在下拉里（放最前面），否则打开
 *  编辑框就会把用户原来的配置显示成空。 */
export function withCurrentOption(options: readonly string[], current: string): string[] {
  const value = current.trim();
  return value && !options.includes(value) ? [value, ...options] : [...options];
}

export function generationModelOptions(
  slot: GenerationSlot,
  vendor: GenerationVendor | undefined,
  all: boolean,
  protocol: string,
  currentModel: string,
): string[] {
  const family = protocol.trim();
  const matches = catalogModels(slot, vendor, all)
    .filter((entry) => !family || entry.protocol === family)
    .map((entry) => entry.model);
  return withCurrentOption(matches, currentModel);
}

/** 模型所属的协议：先按目录查，查不到再按 OpenRouter 的 "厂商/模型" 前缀推断。 */
export function modelProtocol(
  slot: GenerationSlot,
  vendor: GenerationVendor | undefined,
  all: boolean,
  model: string,
): string {
  const id = model.trim();
  const found = catalogModels(slot, vendor, all).find((entry) => entry.model === id);
  return found?.protocol ?? openRouterModelProtocol(id);
}

export function defaultGeneration(slot: GenerationSlot, vendor: GenerationVendor | undefined): { protocol: string; model: string } {
  const model = DEFAULT_GENERATION_MODEL[vendor ?? 'openrouter'][slot];
  return { protocol: modelProtocol(slot, vendor ?? 'openrouter', false, model), model: vendor ? model : '' };
}

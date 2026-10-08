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
// - DashScope（阿里云百炼）：协议固定为 dashscope，走百炼原生的 /api/v1 接口
//   （qwen-image → multimodal-generation；wan → video-synthesis 异步任务），后端由
//   gen_toolkits.py 处理。对话预设的地址是 OpenAI 兼容的 compatible-mode，这里改用
//   /api/v1；密钥分区域（国内 dashscope / 国际 dashscope-intl），地址需要能改。
// - vLLM-Omni：协议固定为 vllm-omni，自部署的推理服务，没有固定地址；API key 和模型名
//   都可以留空（模型名留空时后端用服务实际加载的模型）。

export type GenerationSlot = 'video_gen' | 'visual_gen';

export type GenerationModel = { model: string; protocol: string };

export const MINIMAX_PROTOCOL = 'minimax';
export const MODELARK_PROTOCOL = 'modelark';
export const DASHSCOPE_PROTOCOL = 'dashscope';
export const VLLM_OMNI_PROTOCOL = 'vllm-omni';

const DASHSCOPE_API_BASE = 'https://dashscope.aliyuncs.com/api/v1';

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

const DASHSCOPE_MODELS: Record<GenerationSlot, readonly string[]> = {
  video_gen: ['wan3.0-video', 'wan3.0-video-prime'],
  visual_gen: ['qwen-image-3.0', 'qwen-image-3.0-pro'],
};

const VLLM_OMNI_MODELS: Record<GenerationSlot, readonly string[]> = {
  video_gen: ['MiniMaxAI/MiniMax-H3'],
  visual_gen: ['Qwen/Qwen-Image-2512', 'black-forest-labs/FLUX.2-dev'],
};

function openRouterModelProtocol(modelId: string): string {
  const slash = modelId.indexOf('/');
  return slash > 0 ? modelId.slice(0, slash) : '';
}

function withProtocol(models: Record<GenerationSlot, readonly string[]>, protocol: string) {
  return {
    video_gen: models.video_gen.map((model) => ({ model, protocol })),
    visual_gen: models.visual_gen.map((model) => ({ model, protocol })),
  };
}

const CATALOG: Record<
  'openrouter' | 'minimax' | 'modelark' | 'dashscope' | 'vllm-omni',
  Record<GenerationSlot, readonly GenerationModel[]>
> = {
  openrouter: {
    video_gen: OPENROUTER_MODELS.video_gen.map((model) => ({ model, protocol: openRouterModelProtocol(model) })),
    visual_gen: OPENROUTER_MODELS.visual_gen.map((model) => ({ model, protocol: openRouterModelProtocol(model) })),
  },
  minimax: withProtocol(MINIMAX_MODELS, MINIMAX_PROTOCOL),
  modelark: withProtocol(MODELARK_MODELS, MODELARK_PROTOCOL),
  dashscope: withProtocol(DASHSCOPE_MODELS, DASHSCOPE_PROTOCOL),
  'vllm-omni': withProtocol(VLLM_OMNI_MODELS, VLLM_OMNI_PROTOCOL),
};

export type GenerationVendor = keyof typeof CATALOG;

export const DEFAULT_GENERATION_MODEL: Record<GenerationVendor, Record<GenerationSlot, string>> = {
  openrouter: { video_gen: 'bytedance/seedance-2.0-fast', visual_gen: 'google/gemini-3.1-flash-image' },
  minimax: { video_gen: 'MiniMax-H3', visual_gen: 'image-01' },
  modelark: { video_gen: 'dreamina-seedance-2-5-260628', visual_gen: 'dola-seedream-5-0-pro-260628' },
  dashscope: { video_gen: 'wan3.0-video', visual_gen: 'qwen-image-3.0' },
  // 模型名可空：默认留空，由后端使用服务实际加载的模型。
  'vllm-omni': { video_gen: '', visual_gen: '' },
};

/** 这些厂商的密钥分区域，API 地址需要能改（选完厂商后填入预设地址，用户可改成另一区域）。 */
const REGIONAL_VENDORS: readonly string[] = ['minimax', 'volcengine', 'alibaba'];

/** 自部署厂商：没有固定地址，预设地址只是默认值。 */
const SELF_HOSTED_VENDORS: readonly string[] = ['vllm-omni'];

export function isRegionalVendor(vendorKey: string | undefined): boolean {
  return !!vendorKey && REGIONAL_VENDORS.includes(vendorKey);
}

/** API 地址要让用户填/改的厂商：分区域的厂商和自部署厂商。 */
export function isEditableBaseVendor(vendorKey: string | undefined): boolean {
  return isRegionalVendor(vendorKey) || (!!vendorKey && SELF_HOSTED_VENDORS.includes(vendorKey));
}

/** 生成用的默认地址：百炼的对话预设是 compatible-mode（或 Token/Coding Plan 专属域名），生成走原生 /api/v1。 */
export function generationApiBase(vendorKey: string | undefined, presetBase: string): string {
  return vendorKey === 'alibaba' ? DASHSCOPE_API_BASE : presetBase;
}

/** vLLM-Omni 自部署服务可以不鉴权，模型名留空时用服务实际加载的模型。 */
export function credentialsOptional(protocol: string): boolean {
  return protocol.trim() === VLLM_OMNI_PROTOCOL;
}

/** 生成槽位是否配置完整：vLLM-Omni 只要求地址，其余还要求 API key 和模型名。 */
export function isGenerationSlotConfigured(values: Readonly<Record<string, unknown>>, slot: GenerationSlot): boolean {
  const read = (suffix: string) => String(values[`${slot}_${suffix}`] ?? '').trim();
  if (!['provider', 'protocol', 'api_base'].every(read)) return false;
  return credentialsOptional(read('protocol')) || Boolean(read('api_key') && read('model'));
}

// 后端只接受固定的 provider 名称（ProviderType，如 OpenAI / OpenRouter / MiniMax /
// VolcEngine），不能直接存厂商的展示名（"火山引擎"）或 "Custom"，否则保存时会被
// "Model provider must in: [...]" 拒绝。
const PROVIDER_NAMES: Record<string, string> = {
  openrouter: 'OpenRouter',
  minimax: 'MiniMax',
  volcengine: 'VolcEngine',
  alibaba: 'DashScope',
};

/** 保存到 *_provider 的值：已知厂商用对应的 ProviderType；其余用预设的 client_provider，
 *  自定义地址按 OpenAI 兼容处理（和 图片处理 一致）。 */
export function generationProviderName(vendorKey: string | undefined, clientProvider?: string): string {
  return (vendorKey && PROVIDER_NAMES[vendorKey]) || clientProvider || 'OpenAI';
}

/** 区域提示文案的 i18n key（不同厂商的区域地址不同）。 */
export function regionalHintKey(vendorKey: string | undefined): string {
  if (vendorKey === 'volcengine') return 'settingsPanel.agent.regionalBaseHintModelark';
  if (vendorKey === 'alibaba') return 'settingsPanel.agent.regionalBaseHintDashscope';
  if (vendorKey === 'vllm-omni') return 'settingsPanel.agent.selfHostedBaseHintVllmOmni';
  return 'settingsPanel.agent.regionalBaseHint';
}

const HOST = /^https?:\/\/([^/:]+)/i;

/** 由厂商预设（或自定义地址的域名）判断用哪份目录；都不是则没有专属目录。 */
export function generationVendor(vendorKey: string | undefined, apiBase: string): GenerationVendor | undefined {
  if (vendorKey === 'openrouter' || vendorKey === 'minimax') return vendorKey;
  if (vendorKey === 'volcengine') return 'modelark';
  if (vendorKey === 'alibaba') return 'dashscope';
  if (vendorKey === 'vllm-omni') return 'vllm-omni';
  const host = (HOST.exec(apiBase.trim())?.[1] ?? '').toLowerCase();
  if (/(^|\.)openrouter\.ai$/.test(host)) return 'openrouter';
  if (/(^|\.)minimaxi?\.(io|com)$/.test(host)) return 'minimax';
  if (/^ark\.[\w-]+\.(bytepluses\.com|volces\.com)$/.test(host)) return 'modelark';
  if (/^dashscope(-intl)?\.aliyuncs\.com$/.test(host)) return 'dashscope';
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
  const source = vendor ?? 'openrouter';
  const model = DEFAULT_GENERATION_MODEL[source][slot];
  const protocol = modelProtocol(slot, source, false, model) || (CATALOG[source][slot][0]?.protocol ?? '');
  return { protocol, model: vendor ? model : '' };
}

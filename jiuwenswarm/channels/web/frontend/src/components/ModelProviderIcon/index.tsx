import './index.css';

/**
 * 模型厂商图标组件
 *
 * 根据 model_name / model_provider / api_base 三字段加权模糊匹配，
 * 优先使用本地静态图标；无匹配时退回首字母 fallback。
 *
 * 新增厂商只需：
 *   1. 在 assets/providers/ 里放 {key}.png
 *   2. 在 PROVIDER_SPECS 里加一行
 */

interface ProviderSpec {
  key: string;       // 对应 assets/providers/{key}.png
  keywords: string[]; // 命中任意 keyword 得 keyword.length 分，取最高分厂商
}

export const PROVIDER_SPECS: ProviderSpec[] = [
  { key: 'openai',      keywords: ['openai', 'chatgpt', 'gpt-4', 'gpt-3', 'gpt4', 'gpt3', 'gpt', 'o1-', 'o3-', 'whisper', 'dall-e', 'davinci', 'text-embedding-ada'] },
  { key: 'anthropic',   keywords: ['anthropic', 'claude'] },
  { key: 'google',      keywords: ['google', 'gemini', 'bard', 'palm', 'vertex', 'generativelanguage', 'googleapis'] },
  { key: 'zhipu',       keywords: ['zhipuai', 'zhipu', 'bigmodel', 'chatglm', 'glm-', 'glm'] },
  { key: 'deepseek',    keywords: ['deepseek'] },
  { key: 'qwen',        keywords: ['tongyi', 'qwen', 'dashscope', 'aliyuncs'] },
  { key: 'kimi',        keywords: ['moonshot', 'kimi'] },
  { key: 'minimax',     keywords: ['minimaxi', 'minimax', 'hailuo', 'abab'] },
  { key: 'baidu',       keywords: ['ernie', 'wenxin', 'yiyan', 'baidu'] },
  { key: 'doubao',      keywords: ['doubao', 'volcengine', 'bytedance', 'volc-', 'ark'] },
  { key: 'mistral',     keywords: ['mistral', 'mixtral', 'codestral'] },
  { key: 'meta',        keywords: ['meta-llama', 'llama', 'meta'] },
  { key: 'cohere',      keywords: ['cohere', 'command-r'] },
  { key: 'groq',        keywords: ['groq'] },
  { key: 'xai',         keywords: ['grok', 'xai', 'x.ai'] },
  { key: 'perplexity',  keywords: ['perplexity', 'pplx', 'sonar'] },
  { key: '01ai',        keywords: ['01ai', '01.ai', 'lingyiwanwu', 'yi-'] },
  { key: 'siliconflow', keywords: ['siliconflow'] },
  { key: 'stepfun',     keywords: ['stepfun', 'step-'] },
  { key: 'baichuan',    keywords: ['baichuan'] },
  { key: 'sensetime',   keywords: ['sensetime', 'sensenova', 'nova-ptc'] },
];

// 本地静态图标（Vite 打包时自动处理）
const PROVIDER_ICONS_PNG = import.meta.glob<string>(
  '../../assets/providers/*.png',
  { eager: true, import: 'default' },
);
const PROVIDER_ICONS_SVG = import.meta.glob<string>(
  '../../assets/providers/*.svg',
  { eager: true, import: 'default' },
);

export type ModelLike = {
  model_name: string;
  model_provider?: string;
  api_base?: string;
  alias?: string;
};

/** 按 model_name → api_base → model_provider 三层优先级匹配，匹配上即返回 */
export function findProvider(model: ModelLike): ProviderSpec | null {
  const tiers = [
    (model.model_name ?? '') + ' ' + (model.alias ?? ''),
    model.api_base ?? '',
    model.model_provider ?? '',
  ];

  for (const tier of tiers) {
    const text = tier.toLowerCase();
    if (!text.trim()) continue;

    let bestScore = 0;
    let best: ProviderSpec | null = null;
    for (const spec of PROVIDER_SPECS) {
      let score = 0;
      for (const kw of spec.keywords) {
        if (text.includes(kw)) score += kw.length;
      }
      if (score > bestScore) {
        bestScore = score;
        best = spec;
      }
    }
    if (best) return best;
  }
  return null;
}

/** 获取厂商图标 URL（本地静态资源），PNG 优先，SVG 兜底，未知厂商返回 undefined */
export function getProviderIconUrl(model: ModelLike): string | undefined {
  const spec = findProvider(model);
  if (!spec) return undefined;
  return (
    PROVIDER_ICONS_PNG[`../../assets/providers/${spec.key}.png`] ??
    PROVIDER_ICONS_SVG[`../../assets/providers/${spec.key}.svg`]
  );
}

interface ModelProviderIconProps {
  model: ModelLike;
  className?: string;
}

/**
 * 厂商图标组件。
 * 有本地图标时显示图片；无匹配时显示名称首字母的中性 avatar。
 */
export function ModelProviderIcon({ model, className }: ModelProviderIconProps) {
  const iconUrl = getProviderIconUrl(model);
  const letter = (model.alias ?? model.model_name ?? '?').charAt(0).toUpperCase();

  if (iconUrl) {
    return (
      <img
        className={`model-provider-icon model-provider-icon--img${className ? ` ${className}` : ''}`}
        src={iconUrl}
        alt=""
        aria-hidden="true"
      />
    );
  }

  return (
    <span
      className={`model-provider-icon model-provider-icon--fallback${className ? ` ${className}` : ''}`}
      aria-hidden="true"
    >
      {letter}
    </span>
  );
}

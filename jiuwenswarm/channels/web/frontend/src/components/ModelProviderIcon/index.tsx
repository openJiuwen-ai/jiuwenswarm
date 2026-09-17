import './index.css';

/**
 * 模型厂商图标组件
 *
 * 仅根据 model_name 精确匹配厂商（不含 alias）：
 * model_name 须等于 keyword，或以 keyword 为家族前缀并紧跟版本/分隔符
 * （如 qwen-max、gpt-4、claude-3）；含空格的短语（如 "qwen claud"）不匹配，
 * 回退到 model_provider，再回退到默认 OpenAI。
 * 优先使用本地静态图标；无匹配时退回首字母 fallback。
 *
 * 新增厂商只需：
 *   1. 在 assets/providers/ 里放 {key}.png
 *   2. 在 PROVIDER_SPECS 里加一行
 */

interface ProviderSpec {
  key: string;       // 对应 assets/providers/{key}.png
  keywords: string[]; // 完整 model_name 或家族前缀
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
  { key: 'opencode-zen', keywords: ['opencode.ai/zen', 'opencode-zen', 'opencode zen'] },
];

const DEFAULT_PROVIDER =
  PROVIDER_SPECS.find((spec) => spec.key === 'openai') ?? PROVIDER_SPECS[0];

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

/**
 * 精确匹配：model_name 整体等于 keyword，或以 keyword 为家族前缀
 * 且下一位为版本/分隔符（数字、. _ / -）。
 * "qwen claud" / "qwenclaude" 不会命中 qwen。
 */
function keywordMatchesModelName(text: string, keyword: string): boolean {
  if (text === keyword) return true;
  if (keyword.endsWith('-')) {
    return text.startsWith(keyword);
  }
  if (!text.startsWith(keyword)) return false;
  const next = text[keyword.length];
  return next !== undefined && /[\d._/-]/.test(next);
}

/**
 * 仅当恰好一个厂商被精确命中时返回。
 * "qwen claud" / "qwen claude" / "qwen gpt-4" 等均不匹配。
 */
function matchExactModelName(modelName: string): ProviderSpec | null {
  const text = modelName.trim().toLowerCase();
  if (!text) return null;

  const matched: ProviderSpec[] = [];
  for (const spec of PROVIDER_SPECS) {
    if (spec.keywords.some((kw) => keywordMatchesModelName(text, kw))) {
      matched.push(spec);
      if (matched.length > 1) return null;
    }
  }
  return matched[0] ?? null;
}

/** 按 model_provider 字段做简单命中（用于 model_name 未精确匹配时的回退） */
function matchProviderField(provider: string): ProviderSpec | null {
  const text = provider.trim().toLowerCase();
  if (!text) return null;

  for (const spec of PROVIDER_SPECS) {
    if (spec.keywords.some((kw) => text === kw || text.includes(kw))) {
      return spec;
    }
  }
  return null;
}

/**
 * model_name 精确匹配优先；未命中时回退 model_provider；
 * 仍未命中则默认 OpenAI（与配置表单默认值一致）。
 * alias / api_base 不参与匹配。
 */
export function findProvider(model: ModelLike): ProviderSpec {
  return (
    matchExactModelName(model.model_name ?? '')
    ?? matchProviderField(model.model_provider ?? '')
    ?? DEFAULT_PROVIDER
  );
}

/** 获取厂商图标 URL（本地静态资源），PNG 优先，SVG 兜底，未知厂商返回 undefined */
export function getProviderIconUrl(model: ModelLike): string | undefined {
  const spec = findProvider(model);
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
        data-testid="model-provider-icon"
        data-variant="image"
      />
    );
  }

  return (
    <span
      className={`model-provider-icon model-provider-icon--fallback${className ? ` ${className}` : ''}`}
      aria-hidden="true"
      data-testid="model-provider-icon"
      data-variant="fallback"
    >
      {letter}
    </span>
  );
}

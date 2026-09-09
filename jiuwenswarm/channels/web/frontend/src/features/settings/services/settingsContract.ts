import type { ModelEntry } from '../../../types';

export type SettingsCategory = 'general' | 'models' | 'agent' | 'browser' | 'channels' | 'security' | 'experimental';

export type SettingsRequest = <T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  options?: { timeoutMs?: number },
) => Promise<T>;

export type ConfigValueKind = 'boolean' | 'text' | 'integer';

export type PermissionLevel = 'allow' | 'ask' | 'deny';

export type PermissionsMode = 'auto' | 'full_access' | 'strict';

export type ConfigFieldContract = {
  key: string;
  category: SettingsCategory;
  kind: ConfigValueKind;
  persistence: '.env' | 'config.yaml';
  path: string;
};

export type ModelValidationPayload = {
  api_base: string;
  api_key: string;
  model: string;
  model_provider: string;
  reasoning_level: string | undefined;
  endpoint_profile?: string;
};

const envField = (
  key: string,
  category: SettingsCategory,
  kind: ConfigValueKind,
  envName: string,
): ConfigFieldContract => ({
  key,
  category,
  kind,
  persistence: '.env',
  path: envName,
});

const yamlField = (
  key: string,
  category: SettingsCategory,
  kind: ConfigValueKind,
  path: string,
): ConfigFieldContract => ({
  key,
  category,
  kind,
  persistence: 'config.yaml',
  path,
});

/**
 * Authoritative flat backend-field contract used by Settings business modules.
 * Keep this list aligned with app_web_handlers._CONFIG_SET_ENV_MAP,
 * _CONFIG_YAML_KEYS and the Symphony-specific config specs.
 */
export const SETTINGS_CONFIG_FIELDS: readonly ConfigFieldContract[] = [
  envField('embed_api_base', 'models', 'text', 'EMBED_API_BASE'),
  envField('embed_api_key', 'models', 'text', 'EMBED_API_KEY'),
  envField('embed_model', 'models', 'text', 'EMBED_MODEL'),
  yamlField('enable_free_models', 'models', 'boolean', 'models.enable_free_models'),

  yamlField('skill_evolution', 'agent', 'boolean', 'react.evolution.skill_evolution'),
  yamlField('skill_retrieval_enabled', 'agent', 'boolean', 'symphony.skill_retrieval.enabled'),
  yamlField('skill_retrieval_index_enabled', 'agent', 'boolean', 'symphony.skill_retrieval.index.enabled'),
  envField('free_search_ddg_enabled', 'agent', 'boolean', 'FREE_SEARCH_DDG_ENABLED'),
  envField('free_search_bing_enabled', 'agent', 'boolean', 'FREE_SEARCH_BING_ENABLED'),
  envField('jina_api_key', 'agent', 'text', 'JINA_API_KEY'),
  envField('bocha_api_key', 'agent', 'text', 'BOCHA_API_KEY'),
  envField('perplexity_api_key', 'agent', 'text', 'PERPLEXITY_API_KEY'),
  envField('serper_api_key', 'agent', 'text', 'SERPER_API_KEY'),
  envField('github_token', 'agent', 'text', 'GITHUB_TOKEN'),
  envField('vision_api_base', 'agent', 'text', 'VISION_API_BASE'),
  envField('vision_api_key', 'agent', 'text', 'VISION_API_KEY'),
  envField('vision_model', 'agent', 'text', 'VISION_MODEL_NAME'),
  envField('vision_provider', 'agent', 'text', 'VISION_PROVIDER'),
  envField('vision_endpoint_profile', 'agent', 'text', 'VISION_ENDPOINT_PROFILE'),
  envField('vision_vendor_key', 'agent', 'text', 'VISION_VENDOR_KEY'),
  envField('vision_plan', 'agent', 'text', 'VISION_PLAN'),
  envField('vision_enabled', 'agent', 'boolean', 'VISION_ENABLED'),
  envField('audio_api_base', 'agent', 'text', 'AUDIO_API_BASE'),
  envField('audio_api_key', 'agent', 'text', 'AUDIO_API_KEY'),
  envField('audio_model', 'agent', 'text', 'AUDIO_MODEL_NAME'),
  envField('audio_provider', 'agent', 'text', 'AUDIO_PROVIDER'),
  envField('audio_endpoint_profile', 'agent', 'text', 'AUDIO_ENDPOINT_PROFILE'),
  envField('audio_vendor_key', 'agent', 'text', 'AUDIO_VENDOR_KEY'),
  envField('audio_plan', 'agent', 'text', 'AUDIO_PLAN'),
  envField('audio_enabled', 'agent', 'boolean', 'AUDIO_ENABLED'),
  envField('video_api_base', 'agent', 'text', 'VIDEO_API_BASE'),
  envField('video_api_key', 'agent', 'text', 'VIDEO_API_KEY'),
  envField('video_model', 'agent', 'text', 'VIDEO_MODEL_NAME'),
  envField('video_provider', 'agent', 'text', 'VIDEO_PROVIDER'),
  envField('video_endpoint_profile', 'agent', 'text', 'VIDEO_ENDPOINT_PROFILE'),
  envField('video_vendor_key', 'agent', 'text', 'VIDEO_VENDOR_KEY'),
  envField('video_plan', 'agent', 'text', 'VIDEO_PLAN'),
  envField('video_enabled', 'agent', 'boolean', 'VIDEO_ENABLED'),

  yamlField('permissions_enabled', 'security', 'boolean', 'permissions.enabled'),

  yamlField('a2ui_enabled', 'experimental', 'boolean', 'a2ui.enabled'),
  yamlField('trajectory_ui_enabled', 'experimental', 'boolean', 'trajectory_ui.enabled'),
  yamlField(
    'kv_cache_affinity_enabled',
    'experimental',
    'boolean',
    'kv_cache_affinity_config.enable_kv_cache_affinity',
  ),
  yamlField(
    'external_cli_agent_claude_enabled',
    'experimental',
    'boolean',
    'modes.team.jiuwen_team.external_cli_agents',
  ),
  yamlField(
    'external_cli_agent_claude_use_builtin',
    'experimental',
    'boolean',
    'modes.team.jiuwen_team.external_cli_agents',
  ),
  yamlField('external_cli_agent_claude_cli_path', 'experimental', 'text', 'modes.team.jiuwen_team.external_cli_agents'),
  yamlField(
    'external_cli_agent_codex_enabled',
    'experimental',
    'boolean',
    'modes.team.jiuwen_team.external_cli_agents',
  ),
  yamlField(
    'external_cli_agent_codex_use_builtin',
    'experimental',
    'boolean',
    'modes.team.jiuwen_team.external_cli_agents',
  ),
  yamlField('external_cli_agent_codex_cli_path', 'experimental', 'text', 'modes.team.jiuwen_team.external_cli_agents'),
  yamlField('proactive_recommendation_enabled', 'experimental', 'boolean', 'proactive_recommendation.enabled'),
  yamlField(
    'proactive_recommendation_max_recommend_per_day',
    'experimental',
    'integer',
    'proactive_recommendation.max_recommend_per_day',
  ),
  yamlField(
    'proactive_recommendation_max_rounds_per_tick',
    'experimental',
    'integer',
    'proactive_recommendation.max_rounds_per_tick',
  ),
] as const;

export const SETTINGS_CONFIG_FIELD_BY_KEY = new Map(SETTINGS_CONFIG_FIELDS.map((field) => [field.key, field] as const));

export const SETTINGS_OTHER_PERSISTENCE = [
  { id: 'language', method: 'locale.set_conf', persistence: 'config.yaml', path: 'preferred_language' },
  { id: 'models', method: 'models.replace_all', persistence: 'config.yaml', path: 'models.defaults' },
  {
    id: 'browser',
    method: 'path.set',
    persistence: 'config.yaml',
    path: 'browser.chrome_path, browser.browser_type, browser.headless',
  },
  {
    id: 'permissions.tools',
    method: 'permissions.tools.update',
    persistence: 'config.yaml',
    path: 'permissions.tools.<tool>',
  },
] as const;

export const OPENAI_ACCOUNT_RPC = {
  status: 'openai_account.auth.status',
  pendingLogin: 'openai_account.auth.pending_login',
  startLogin: 'openai_account.auth.start_login',
  pollLogin: 'openai_account.auth.poll_login',
  logout: 'openai_account.auth.logout',
  listModels: 'openai_account.models.list',
} as const;

export const OPEN_SOURCE_SETTINGS_REQUEST_METHODS = [
  'config.get',
  'config.save_all',
  'config.validate_model',
  'locale.set_conf',
  'models.list',
  'models.replace_all',
  'vendors.list',
  'vendors.fetch_models',
  'path.get',
  'path.set',
  'permissions.tools.get',
  'permissions.tools.update',
  'permissions.tools.delete',
  ...Object.values(OPENAI_ACCOUNT_RPC),
  'channel.get',
  'channel.xiaoyi.get_conf',
  'channel.xiaoyi.set_conf',
  'channel.feishu.get_conf',
  'channel.feishu.set_conf',
  'channel.dingtalk.get_conf',
  'channel.dingtalk.set_conf',
  'channel.telegram.get_conf',
  'channel.telegram.set_conf',
  'channel.discord.get_conf',
  'channel.discord.set_conf',
  'channel.slack.get_conf',
  'channel.slack.set_conf',
  'channel.whatsapp.get_conf',
  'channel.whatsapp.set_conf',
] as const;

export function parseConfigBoolean(value: unknown): boolean {
  if (value === true || value === 1) return true;
  return ['true', '1', 'yes', 'on', 'enabled'].includes(
    String(value ?? '')
      .trim()
      .toLowerCase(),
  );
}

export function toConfigBoolean(value: boolean): string {
  return value ? 'true' : 'false';
}

export function normalizePermissionLevel(value: unknown): PermissionLevel | undefined {
  if (typeof value !== 'string') return undefined;
  const normalized = value.trim().toLowerCase();
  return normalized === 'allow' || normalized === 'ask' || normalized === 'deny' ? normalized : undefined;
}

export function normalizePermissionsMode(value: unknown): PermissionsMode | undefined {
  if (value === 'auto' || value === 'full_access' || value === 'strict') return value;
  return undefined;
}

export function normalizeSettingsConfigUpdates(updates: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(updates).map(([key, value]) => {
      const field = SETTINGS_CONFIG_FIELD_BY_KEY.get(key);
      if (!field) throw new Error(`Unknown settings config key: ${key}`);
      return [key, field.kind === 'boolean' ? value : value.trim()];
    }),
  );
}

export function buildModelValidationPayload(model: ModelEntry): ModelValidationPayload {
  const payload: ModelValidationPayload = {
    api_base: model.api_base,
    api_key: model.api_key,
    model: model.model_name,
    model_provider: model.model_provider,
    reasoning_level: model.reasoning_level,
  };
  if (model.endpoint_profile) payload.endpoint_profile = model.endpoint_profile;
  return payload;
}

export async function addPermissionToolIfUnique(
  rawTool: string,
  permissionTools: Record<string, unknown>,
  addTool: (tool: string) => Promise<void>,
): Promise<'empty' | 'duplicate' | 'added'> {
  const tool = rawTool.trim();
  if (!tool) return 'empty';
  if (Object.keys(permissionTools).some((existingTool) => existingTool.trim() === tool)) return 'duplicate';
  await addTool(tool);
  return 'added';
}

export function buildConfigSavePayload(updates: Record<string, string>): { config: Record<string, string> } {
  return { config: normalizeSettingsConfigUpdates(updates) };
}

export function buildModelsSavePayload(models: ModelEntry[]): Record<string, unknown> {
  if (models.length === 0) {
    throw new Error('At least one model is required');
  }
  return { models };
}

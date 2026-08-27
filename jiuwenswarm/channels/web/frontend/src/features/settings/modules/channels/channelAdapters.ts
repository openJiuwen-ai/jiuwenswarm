import i18n from '../../../../i18n';
import type {
  DingtalkFormValues,
  DiscordFormValues,
  FeishuAppConfig,
  FeishuAppDraft,
  FeishuConfig,
  FeishuFormValues,
  SlackFormValues,
  SingleSettingsChannelId,
  TelegramFormValues,
  WhatsAppFormValues,
  XiaoyiAppConfig,
  XiaoyiConfig,
  XiaoyiFormValues,
} from './channelTypes';

export { channelConfigurationChecks } from './channelRequirements';

const DEFAULT_FEISHU_CONFIG: FeishuConfig = {
  enabled: false,
  enable_streaming: true,
  app_id: '',
  app_secret: '',
  encrypt_key: '',
  verification_token: '',
  chat_id: '',
  allow_from: [],
  group_digital_avatar: false,
  my_user_id: '',
  bot_name: '',
  enable_memory: false,
};

const DEFAULT_XIAOYI_CONFIG: XiaoyiConfig = {
  enabled: false,
  ak: '',
  sk: '',
  agent_id: '',
  api_id: '',
  enable_streaming: true,
};

function normalizeStringList(value: unknown): string[] {
  return (Array.isArray(value) ? value : []).map((item) => String(item ?? '').trim()).filter((item) => item.length > 0);
}

function normalizeTextList(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function normalizeFeishuConfig(input: unknown): FeishuConfig {
  if (!input || typeof input !== 'object') return DEFAULT_FEISHU_CONFIG;
  const data = input as Record<string, unknown>;
  return {
    enabled: Boolean(data.enabled),
    enable_streaming: data.enable_streaming === undefined ? true : Boolean(data.enable_streaming),
    app_id: String(data.app_id ?? '').trim(),
    app_secret: String(data.app_secret ?? '').trim(),
    encrypt_key: String(data.encrypt_key ?? '').trim(),
    verification_token: String(data.verification_token ?? '').trim(),
    chat_id: String(data.chat_id ?? '').trim(),
    allow_from: normalizeStringList(data.allow_from),
    group_digital_avatar: Boolean(data.group_digital_avatar),
    my_user_id: String(data.my_user_id ?? '').trim(),
    bot_name: String(data.bot_name ?? '').trim(),
    enable_memory: Boolean(data.enable_memory),
  };
}

function normalizeFeishuApp(input: unknown, fallbackName: string, isDefault: boolean): FeishuAppConfig {
  const config = normalizeFeishuConfig(input);
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    ...config,
    name: String(data.name ?? fallbackName).trim() || fallbackName,
    is_default: data.is_default === undefined ? isDefault : Boolean(data.is_default),
  };
}

function toFeishuDraft(config: FeishuAppConfig): FeishuAppDraft {
  return { ...config, allow_from: config.allow_from.join('\n') };
}

export function readFeishuFormValues(input: unknown): FeishuFormValues {
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  const rawApps = Array.isArray(data.apps) ? data.apps : [input];
  const apps = rawApps.map((app, index) =>
    normalizeFeishuApp(app, i18n.t('channels.feishuApps.appNameTemplate', { index: index + 1 }), index === 0),
  );
  const normalizedApps = apps.length
    ? apps
    : [normalizeFeishuApp(DEFAULT_FEISHU_CONFIG, i18n.t('channels.feishuApps.defaultAppName'), true)];
  normalizedApps.sort((left, right) => left.name.localeCompare(right.name, i18n.resolvedLanguage ?? i18n.language));
  return { apps: normalizedApps.map(toFeishuDraft) };
}

export function createDefaultFeishuFormValues(): FeishuFormValues {
  return readFeishuFormValues(DEFAULT_FEISHU_CONFIG);
}

function buildFeishuApp(draft: FeishuAppDraft): FeishuAppConfig {
  return {
    enabled: draft.enabled,
    enable_streaming: draft.enable_streaming,
    app_id: draft.app_id.trim(),
    app_secret: draft.app_secret.trim(),
    encrypt_key: draft.encrypt_key.trim(),
    verification_token: draft.verification_token.trim(),
    chat_id: draft.chat_id.trim(),
    allow_from: normalizeTextList(draft.allow_from),
    group_digital_avatar: draft.group_digital_avatar,
    my_user_id: draft.my_user_id.trim(),
    bot_name: draft.bot_name.trim(),
    enable_memory: draft.enable_memory,
    name: draft.name.trim() || i18n.t('channels.feishuApps.unnamedAppName'),
    is_default: draft.is_default,
  };
}

export function buildFeishuFormPayload(values: FeishuFormValues): Record<string, unknown> {
  return { apps: values.apps.map(buildFeishuApp) };
}

export function buildFeishuEnabledPayload(
  values: FeishuFormValues,
  accountIndex: number,
  enabled: boolean,
): Record<string, unknown> {
  if (!Number.isInteger(accountIndex) || accountIndex < 0 || accountIndex >= values.apps.length) {
    throw new RangeError(`Invalid Feishu account index: ${accountIndex}`);
  }
  return buildFeishuFormPayload({
    apps: values.apps.map((app, index) => (index === accountIndex ? { ...app, enabled } : app)),
  });
}

export function buildFeishuDeletionPayload(values: FeishuFormValues, accountIndex: number): Record<string, unknown> {
  if (!Number.isInteger(accountIndex) || accountIndex < 0 || accountIndex >= values.apps.length) {
    throw new RangeError(`Invalid Feishu account index: ${accountIndex}`);
  }
  const apps = values.apps.filter((_, index) => index !== accountIndex);
  if (apps.length > 0 && !apps.some((app) => app.is_default)) {
    apps[0] = { ...apps[0], is_default: true };
  }
  return buildFeishuFormPayload({ apps });
}

function normalizeXiaoyiConfig(input: unknown): XiaoyiConfig {
  if (!input || typeof input !== 'object') return DEFAULT_XIAOYI_CONFIG;
  const data = input as Record<string, unknown>;
  return {
    enabled: Boolean(data.enabled),
    ak: String(data.ak ?? '').trim(),
    sk: String(data.sk ?? '').trim(),
    agent_id: String(data.agent_id ?? '').trim(),
    api_id: String(data.api_id ?? '').trim(),
    enable_streaming: data.enable_streaming === undefined ? true : Boolean(data.enable_streaming),
  };
}

function normalizeXiaoyiApp(input: unknown, fallbackName: string, isDefault: boolean): XiaoyiAppConfig {
  const config = normalizeXiaoyiConfig(input);
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    ...config,
    name: String(data.name ?? fallbackName).trim() || fallbackName,
    is_default: data.is_default === undefined ? isDefault : Boolean(data.is_default),
  };
}

export function readXiaoyiFormValues(input: unknown): XiaoyiFormValues {
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  const apps = Array.isArray(data.apps)
    ? data.apps.map((app, index) =>
        normalizeXiaoyiApp(app, i18n.t('channels.xiaoyiApps.appNameTemplate', { index: index + 1 }), index === 0),
      )
    : [normalizeXiaoyiApp(input, i18n.t('channels.xiaoyiApps.defaultAppName'), true)];
  return (
    apps.find((app) => app.is_default) ??
    apps[0] ??
    normalizeXiaoyiApp({}, i18n.t('channels.xiaoyiApps.defaultAppName'), true)
  );
}

export function createDefaultXiaoyiFormValues(): XiaoyiFormValues {
  return readXiaoyiFormValues(DEFAULT_XIAOYI_CONFIG);
}

function buildXiaoyiApp(values: XiaoyiFormValues): XiaoyiAppConfig {
  return {
    enabled: values.enabled,
    ak: values.ak.trim(),
    sk: values.sk.trim(),
    agent_id: values.agent_id.trim(),
    api_id: values.api_id.trim(),
    enable_streaming: values.enable_streaming,
    name: values.name.trim() || i18n.t('channels.xiaoyiApps.unnamedAppName'),
    is_default: true,
  };
}

export function buildXiaoyiFormPayload(values: XiaoyiFormValues): Record<string, unknown> {
  return { apps: [buildXiaoyiApp(values)] };
}

export function buildXiaoyiDeletionPayload(): Record<string, unknown> {
  return { apps: [] };
}

export function readDingtalkFormValues(input: unknown): DingtalkFormValues {
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    enabled: Boolean(data.enabled),
    client_id: String(data.client_id ?? '').trim(),
    client_secret: String(data.client_secret ?? '').trim(),
    allow_from: normalizeStringList(data.allow_from).join('\n'),
  };
}

export function buildDingtalkFormPayload(values: DingtalkFormValues): Record<string, unknown> {
  return {
    enabled: values.enabled,
    client_id: values.client_id.trim(),
    client_secret: values.client_secret.trim(),
    allow_from: normalizeTextList(values.allow_from),
  };
}

export function buildDingtalkDeletionPayload(): Record<string, unknown> {
  return buildDingtalkFormPayload(readDingtalkFormValues({}));
}

export function readTelegramFormValues(input: unknown): TelegramFormValues {
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    enabled: Boolean(data.enabled),
    bot_token: String(data.bot_token ?? '').trim(),
    allow_from: normalizeStringList(data.allow_from).join('\n'),
    parse_mode: String(data.parse_mode ?? 'Markdown').trim(),
    group_chat_mode: String(data.group_chat_mode ?? 'mention').trim(),
  };
}

export function buildTelegramFormPayload(values: TelegramFormValues): Record<string, unknown> {
  return {
    enabled: values.enabled,
    bot_token: values.bot_token.trim(),
    allow_from: normalizeTextList(values.allow_from),
    parse_mode: values.parse_mode.trim(),
    group_chat_mode: values.group_chat_mode.trim(),
  };
}

export function buildTelegramDeletionPayload(): Record<string, unknown> {
  return buildTelegramFormPayload(readTelegramFormValues({}));
}

function parseDiscordBoolean(value: unknown): boolean {
  if (value === true || value === 1) return true;
  if (value === false || value === 0) return false;
  const normalized = String(value).trim().toLowerCase();
  return normalized === 'true' || normalized === '1';
}

export function readDiscordFormValues(input: unknown): DiscordFormValues {
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    enabled: Boolean(data.enabled),
    bot_token: String(data.bot_token ?? '').trim(),
    application_id: String(data.application_id ?? '').trim(),
    guild_id: String(data.guild_id ?? '').trim(),
    channel_id: String(data.channel_id ?? '').trim(),
    block_dm: parseDiscordBoolean(data.block_dm),
    allow_from: normalizeStringList(data.allow_from).join('\n'),
  };
}

export function buildDiscordFormPayload(values: DiscordFormValues): Record<string, unknown> {
  return {
    enabled: values.enabled,
    bot_token: values.bot_token.trim(),
    application_id: values.application_id.trim(),
    guild_id: values.guild_id.trim(),
    channel_id: values.channel_id.trim(),
    block_dm: values.block_dm,
    allow_from: normalizeTextList(values.allow_from),
  };
}

export function buildDiscordDeletionPayload(): Record<string, unknown> {
  return buildDiscordFormPayload(readDiscordFormValues({}));
}

export function readSlackFormValues(input: unknown): SlackFormValues {
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    enabled: Boolean(data.enabled),
    bot_token: String(data.bot_token ?? '').trim(),
    app_token: String(data.app_token ?? '').trim(),
    allow_from: normalizeStringList(data.allow_from).join('\n'),
    allowed_channel_ids: normalizeStringList(data.allowed_channel_ids).join('\n'),
    default_channel_id: String(data.default_channel_id ?? '').trim(),
    reply_in_thread: data.reply_in_thread === undefined ? true : Boolean(data.reply_in_thread),
  };
}

export function buildSlackFormPayload(values: SlackFormValues): Record<string, unknown> {
  return {
    enabled: values.enabled,
    bot_token: values.bot_token.trim(),
    app_token: values.app_token.trim(),
    allow_from: normalizeTextList(values.allow_from),
    allowed_channel_ids: normalizeTextList(values.allowed_channel_ids),
    default_channel_id: values.default_channel_id.trim(),
    reply_in_thread: values.reply_in_thread,
  };
}

export function buildSlackDeletionPayload(): Record<string, unknown> {
  return buildSlackFormPayload(readSlackFormValues({}));
}

export function readWhatsAppFormValues(input: unknown): WhatsAppFormValues {
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    enabled: Boolean(data.enabled),
    bridge_ws_url: String(data.bridge_ws_url ?? 'ws://127.0.0.1:19600/ws').trim(),
    default_jid: String(data.default_jid ?? '').trim(),
    allow_from: normalizeStringList(data.allow_from).join('\n'),
    enable_streaming: data.enable_streaming === undefined ? true : Boolean(data.enable_streaming),
    auto_start_bridge: Boolean(data.auto_start_bridge),
    bridge_command: String(data.bridge_command ?? 'node scripts/whatsapp-bridge.js').trim(),
    bridge_workdir: String(data.bridge_workdir ?? '').trim(),
  };
}

export function buildWhatsAppFormPayload(values: WhatsAppFormValues): Record<string, unknown> {
  return {
    enabled: values.enabled,
    bridge_ws_url: values.bridge_ws_url.trim(),
    default_jid: values.default_jid.trim(),
    allow_from: normalizeTextList(values.allow_from),
    enable_streaming: values.enable_streaming,
    auto_start_bridge: values.auto_start_bridge,
    bridge_command: values.bridge_command.trim(),
    bridge_workdir: values.bridge_workdir.trim(),
  };
}

export function buildWhatsAppDeletionPayload(): Record<string, unknown> {
  return {
    enabled: false,
    bridge_ws_url: '',
    default_jid: '',
    allow_from: [],
    enable_streaming: true,
    auto_start_bridge: false,
    bridge_command: '',
    bridge_workdir: '',
  };
}

const SINGLE_CHANNEL_DELETION_PAYLOAD_BUILDERS: Record<SingleSettingsChannelId, () => Record<string, unknown>> = {
  xiaoyi: buildXiaoyiDeletionPayload,
  dingtalk: buildDingtalkDeletionPayload,
  telegram: buildTelegramDeletionPayload,
  discord: buildDiscordDeletionPayload,
  slack: buildSlackDeletionPayload,
  whatsapp: buildWhatsAppDeletionPayload,
};

export function buildSingleChannelDeletionPayload(channelId: SingleSettingsChannelId): Record<string, unknown> {
  return SINGLE_CHANNEL_DELETION_PAYLOAD_BUILDERS[channelId]();
}

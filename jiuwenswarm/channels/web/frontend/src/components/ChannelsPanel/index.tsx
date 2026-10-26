import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Plus, Trash2, X } from 'lucide-react';
import i18n from '../../i18n';
import { webRequest } from '../../services/webClient';
import { AvatarPermEditor } from './AvatarPermEditor';
import { WechatQrModal } from './WechatQrModal';
import { WechatUnbindConfirmModal } from './WechatUnbindConfirmModal';
import {
  DEFAULT_WECHAT_CONF,
  buildWechatPayload,
  draftFromWechatConfig,
  normalizeWechatConfig,
  normalizeWechatLoginUi,
  validateWechatNumericDraft,
  WECHAT_NUMERIC_BOUNDS,
  type WechatConfig,
  type WechatDraft,
  type WechatLoginUiState,
} from './wechatTypes';
import './ChannelsPanel.css';

interface ChannelsPanelProps {
  isConnected: boolean;
}

type ChannelItem = {
  channel_id: SupportedChannelId;
  logo_src: string | null;
  enabled: boolean;
};

type LoadState = 'idle' | 'loading' | 'success' | 'error';
type SupportedChannelId =
  | 'web'
  | 'xiaoyi'
  | 'feishu'
  | 'dingtalk'
  | 'telegram'
  | 'discord'
  | 'slack'
  | 'whatsapp'
  | 'wechat';

const ADAPTING_CHANNEL_IDS = new Set<SupportedChannelId>([]);

type FeishuConfig = {
  enabled: boolean;
  enable_streaming: boolean;
  app_id: string;
  app_secret: string;
  encrypt_key: string;
  verification_token: string;
  chat_id: string;
  allow_from: string[];
  group_digital_avatar: boolean;
  my_user_id: string;
  bot_name: string;
  enable_memory: boolean;
};

type FeishuDraft = {
  name: string;
  is_default: boolean;
  enabled: boolean;
  enable_streaming: boolean;
  app_id: string;
  app_secret: string;
  encrypt_key: string;
  verification_token: string;
  chat_id: string;
  allow_from: string;
  group_digital_avatar: boolean;
  my_user_id: string;
  bot_name: string;
  enable_memory: boolean;
};

type FeishuAppConfig = FeishuConfig & {
  name: string;
  is_default: boolean;
};

type FeishuAppDraft = FeishuDraft;

type XiaoyiConfig = {
  enabled: boolean;
  ak: string;
  sk: string;
  agent_id: string;
  api_id: string;
  enable_streaming: boolean;
};

type XiaoyiDraft = {
  name: string;
  is_default: boolean;
  enabled: boolean;
  ak: string;
  sk: string;
  agent_id: string;
  api_id: string;
  enable_streaming: boolean;
};

type XiaoyiAppConfig = XiaoyiConfig & {
  name: string;
  is_default: boolean;
};

type XiaoyiAppDraft = XiaoyiDraft;

type DingTalkConfig = {
  enabled: boolean;
  client_id: string;
  client_secret: string;
  allow_from: string[];
};

type DingTalkDraft = {
  enabled: boolean;
  client_id: string;
  client_secret: string;
  allow_from: string;
};

type TelegramConfig = {
  enabled: boolean;
  bot_token: string;
  allow_from: string[];
  parse_mode: string;
  group_chat_mode: string;
};

type TelegramDraft = {
  enabled: boolean;
  bot_token: string;
  allow_from: string;
  parse_mode: string;
  group_chat_mode: string;
};

type DiscordConfig = {
  enabled: boolean;
  bot_token: string;
  application_id: string;
  guild_id: string;
  channel_id: string;
  block_dm: boolean;
  allow_from: string[];
};

type DiscordDraft = {
  enabled: boolean;
  bot_token: string;
  application_id: string;
  guild_id: string;
  channel_id: string;
  block_dm: boolean;
  allow_from: string;
};

type SlackConfig = {
  enabled: boolean;
  bot_token: string;
  app_token: string;
  allow_from: string[];
  allowed_channel_ids: string[];
  default_channel_id: string;
  reply_in_thread: boolean;
};

type SlackDraft = {
  enabled: boolean;
  bot_token: string;
  app_token: string;
  allow_from: string;
  allowed_channel_ids: string;
  default_channel_id: string;
  reply_in_thread: boolean;
};

type WhatsAppConfig = {
  enabled: boolean;
  bridge_ws_url: string;
  default_jid: string;
  allow_from: string[];
  enable_streaming: boolean;
  auto_start_bridge: boolean;
  bridge_command: string;
  bridge_workdir: string;
};

type WhatsAppDraft = {
  enabled: boolean;
  bridge_ws_url: string;
  default_jid: string;
  allow_from: string;
  enable_streaming: boolean;
  auto_start_bridge: boolean;
  bridge_command: string;
  bridge_workdir: string;
};

type WecomConfig = {
  enabled: boolean;
  bot_id: string;
  secret: string;
  ws_url: string;
  allow_from: string[];
  enable_streaming: boolean;
  send_thinking_message: boolean;
  /** 心跳/定时推送目标 chatid，不填则用最近一次聊天的 last_chat_id */
  default_chat_id: string;
  group_digital_avatar: boolean;
  my_user_id: string;
  bot_name: string;
  enable_memory: boolean;
};

type WecomDraft = {
  enabled: boolean;
  bot_id: string;
  secret: string;
  ws_url: string;
  allow_from: string;
  enable_streaming: boolean;
  send_thinking_message: boolean;
  default_chat_id: string;
  group_digital_avatar: boolean;
  my_user_id: string;
  bot_name: string;
  enable_memory: boolean;
};

const DEFAULT_FEISHU_CONF: FeishuConfig = {
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

const DEFAULT_XIAOYI_CONF: XiaoyiConfig = {
  enabled: false,
  ak: '',
  sk: '',
  agent_id: '',
  api_id: '',
  enable_streaming: true,
};

const DEFAULT_DINGTALK_CONF: DingTalkConfig = {
  enabled: false,
  client_id: '',
  client_secret: '',
  allow_from: [],
};

const DEFAULT_TELEGRAM_CONF: TelegramConfig = {
  enabled: false,
  bot_token: '',
  allow_from: [],
  parse_mode: 'Markdown',
  group_chat_mode: 'mention',
};

const DEFAULT_DISCORD_CONF: DiscordConfig = {
  enabled: false,
  bot_token: '',
  application_id: '',
  guild_id: '',
  channel_id: '',
  block_dm: false,
  allow_from: [],
};

const DEFAULT_SLACK_CONF: SlackConfig = {
  enabled: false,
  bot_token: '',
  app_token: '',
  allow_from: [],
  allowed_channel_ids: [],
  default_channel_id: '',
  reply_in_thread: true,
};

const DEFAULT_WHATSAPP_CONF: WhatsAppConfig = {
  enabled: false,
  bridge_ws_url: 'ws://127.0.0.1:19600/ws',
  default_jid: '',
  allow_from: [],
  enable_streaming: true,
  auto_start_bridge: false,
  bridge_command: 'node scripts/whatsapp-bridge.js',
  bridge_workdir: '',
};

const DEFAULT_WECOM_CONF: WecomConfig = {
  enabled: false,
  bot_id: '',
  secret: '',
  ws_url: 'wss://openws.work.weixin.qq.com',
  allow_from: [],
  enable_streaming: true,
  send_thinking_message: false,
  default_chat_id: '',
  group_digital_avatar: false,
  my_user_id: '',
  bot_name: '',
  enable_memory: false,
};

const SUPPORTED_CHANNELS: Array<{ channel_id: SupportedChannelId; logo_src: string | null }> = [
  { channel_id: 'web', logo_src: null },
  { channel_id: 'xiaoyi', logo_src: '/xiaoyi.webp' },
  { channel_id: 'feishu', logo_src: '/feishu.webp' },
  { channel_id: 'dingtalk', logo_src: '/dingtalk.png' },
  { channel_id: 'telegram', logo_src: '/telegram.webp' },
  { channel_id: 'discord', logo_src: '/discord.webp' },
  { channel_id: 'slack', logo_src: '/slack.svg' },
  { channel_id: 'whatsapp', logo_src: '/whatsapp.png' },
];


function formatTime(iso: string | null, locale: string): string {
  if (!iso) return '-';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleTimeString(locale, { hour12: false });
}

function isSensitiveField(field: keyof FeishuDraft): boolean {
  return field === 'app_secret' || field === 'encrypt_key' || field === 'verification_token';
}

function isSensitiveXiaoyiField(field: keyof XiaoyiDraft): boolean {
  return field === 'ak' || field === 'sk';
}

function isSensitiveDingtalkField(field: keyof DingTalkDraft): boolean {
  return field === 'client_secret';
}

function normalizeEnabledChannels(channels: unknown): Set<string> {
  if (!Array.isArray(channels)) {
    return new Set();
  }
  return new Set(
    channels
    .map((item) => {
      if (!item || typeof item !== 'object') {
        return null;
      }
      const channelId = (item as { channel_id?: unknown }).channel_id;
      if (typeof channelId !== 'string' || !channelId.trim()) {
        return null;
      }
      return channelId.trim().toLowerCase();
    })
      .filter((item): item is string => item !== null),
  );
}

function buildChannels(channels: unknown): ChannelItem[] {
  const enabledChannels = normalizeEnabledChannels(channels);
  return SUPPORTED_CHANNELS.map((channel) => ({
    ...channel,
    enabled: enabledChannels.has(channel.channel_id),
  }));
}

function getChannelLabel(t: (key: string) => string, channelId: SupportedChannelId): string {
  return t(`channels.labels.${channelId}`);
}

function normalizeFeishuConfig(input: unknown): FeishuConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_FEISHU_CONF;
  }
  const data = input as Record<string, unknown>;
  const allowFromRaw = Array.isArray(data.allow_from) ? data.allow_from : [];
  const allowFrom = allowFromRaw
    .map((item) => String(item ?? '').trim())
    .filter((item) => item.length > 0);
  return {
    enabled: Boolean(data.enabled),
    enable_streaming: data.enable_streaming === undefined ? true : Boolean(data.enable_streaming),
    app_id: String(data.app_id ?? '').trim(),
    app_secret: String(data.app_secret ?? '').trim(),
    encrypt_key: String(data.encrypt_key ?? '').trim(),
    verification_token: String(data.verification_token ?? '').trim(),
    chat_id: String(data.chat_id ?? '').trim(),
    allow_from: allowFrom,
    group_digital_avatar: Boolean(data.group_digital_avatar),
    my_user_id: String(data.my_user_id ?? '').trim(),
    bot_name: String(data.bot_name ?? '').trim(),
    enable_memory: Boolean(data.enable_memory),
  };
}

function draftFromFeishuConfig(conf: FeishuConfig): FeishuDraft {
  return {
    name: '',
    is_default: true,
    enabled: conf.enabled,
    enable_streaming: conf.enable_streaming,
    app_id: conf.app_id,
    app_secret: conf.app_secret,
    encrypt_key: conf.encrypt_key,
    verification_token: conf.verification_token,
    chat_id: conf.chat_id,
    allow_from: conf.allow_from.join('\n'),
    group_digital_avatar: conf.group_digital_avatar,
    my_user_id: conf.my_user_id,
    bot_name: conf.bot_name,
    enable_memory: conf.enable_memory,
  };
}

function normalizeAllowFromText(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function buildFeishuPayload(draft: FeishuDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    enable_streaming: draft.enable_streaming,
    app_id: draft.app_id.trim(),
    app_secret: draft.app_secret.trim(),
    encrypt_key: draft.encrypt_key.trim(),
    verification_token: draft.verification_token.trim(),
    chat_id: draft.chat_id.trim(),
    allow_from: normalizeAllowFromText(draft.allow_from),
    group_digital_avatar: draft.group_digital_avatar,
    my_user_id: draft.my_user_id.trim(),
    bot_name: draft.bot_name.trim(),
    enable_memory: draft.enable_memory,
  };
}

function sortFeishuApps(apps: FeishuAppConfig[]): FeishuAppConfig[] {
  return [...apps].sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'));
}

function normalizeFeishuAppConfig(input: unknown, fallbackName = i18n.t('channels.feishuApps.unnamedAppName'), isDefault = false): FeishuAppConfig {
  const base = normalizeFeishuConfig(input);
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    ...base,
    name: String(data.name ?? fallbackName).trim() || fallbackName,
    is_default: data.is_default === undefined ? isDefault : Boolean(data.is_default),
  };
}

function normalizeFeishuAppsConfig(input: unknown): FeishuAppConfig[] {
  if (input && typeof input === 'object') {
    const data = input as Record<string, unknown>;
    if (Array.isArray(data.apps)) {
      const apps = data.apps.map((item, idx) =>
        normalizeFeishuAppConfig(item, i18n.t('channels.feishuApps.appNameTemplate', { index: idx + 1 }), idx === 0),
      );
      return sortFeishuApps(
        apps.length > 0
          ? apps
          : [normalizeFeishuAppConfig(DEFAULT_FEISHU_CONF, i18n.t('channels.feishuApps.defaultAppName'), true)],
      );
    }
  }
  return sortFeishuApps([normalizeFeishuAppConfig(input, i18n.t('channels.feishuApps.defaultAppName'), true)]);
}

function draftFromFeishuAppConfig(conf: FeishuAppConfig): FeishuAppDraft {
  return {
    ...draftFromFeishuConfig(conf),
    name: conf.name,
    is_default: conf.is_default,
  };
}

function buildFeishuAppConfig(draft: FeishuAppDraft): FeishuAppConfig {
  const payload = buildFeishuPayload(draft);
  return {
    ...DEFAULT_FEISHU_CONF,
    ...(payload as FeishuConfig),
    name: draft.name.trim() || i18n.t('channels.feishuApps.unnamedAppName'),
    is_default: draft.is_default,
  };
}

function normalizeXiaoyiConfig(input: unknown): XiaoyiConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_XIAOYI_CONF;
  }
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

function draftFromXiaoyiConfig(conf: XiaoyiConfig): XiaoyiDraft {
  return {
    name: '',
    is_default: true,
    enabled: conf.enabled,
    ak: conf.ak,
    sk: conf.sk,
    agent_id: conf.agent_id,
    api_id: conf.api_id,
    enable_streaming: conf.enable_streaming,
  };
}

function buildXiaoyiPayload(draft: XiaoyiDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    ak: draft.ak.trim(),
    sk: draft.sk.trim(),
    agent_id: draft.agent_id.trim(),
    api_id: draft.api_id.trim(),
    enable_streaming: draft.enable_streaming,
  };
}

function sortXiaoyiApps(apps: XiaoyiAppConfig[]): XiaoyiAppConfig[] {
  return [...apps].sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'));
}

function normalizeXiaoyiAppConfig(input: unknown, fallbackName = '未命名小艺应用', isDefault = false): XiaoyiAppConfig {
  const base = normalizeXiaoyiConfig(input);
  const data = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  return {
    ...base,
    name: String(data.name ?? fallbackName).trim() || fallbackName,
    is_default: data.is_default === undefined ? isDefault : Boolean(data.is_default),
  };
}

function normalizeXiaoyiAppsConfig(input: unknown): XiaoyiAppConfig[] {
  if (input && typeof input === 'object') {
    const data = input as Record<string, unknown>;
    if (Array.isArray(data.apps)) {
      const apps = data.apps.map((item, idx) => normalizeXiaoyiAppConfig(item, `小艺应用 ${idx + 1}`, idx === 0));
      return sortXiaoyiApps(apps.length > 0 ? apps : [normalizeXiaoyiAppConfig(DEFAULT_XIAOYI_CONF, '默认小艺应用', true)]);
    }
  }
  return sortXiaoyiApps([normalizeXiaoyiAppConfig(input, '默认小艺应用', true)]);
}

function normalizeSingleXiaoyiAppConfig(input: unknown): XiaoyiAppConfig {
  const apps = normalizeXiaoyiAppsConfig(input);
  return apps.find((app) => app.is_default) ?? apps[0] ?? normalizeXiaoyiAppConfig(DEFAULT_XIAOYI_CONF, '默认小艺应用', true);
}

function draftFromXiaoyiAppConfig(conf: XiaoyiAppConfig): XiaoyiAppDraft {
  return {
    ...draftFromXiaoyiConfig(conf),
    name: conf.name,
    is_default: conf.is_default,
  };
}

function buildXiaoyiAppConfig(draft: XiaoyiAppDraft): XiaoyiAppConfig {
  const payload = buildXiaoyiPayload(draft);
  return {
    ...DEFAULT_XIAOYI_CONF,
    ...(payload as XiaoyiConfig),
    name: draft.name.trim() || '未命名小艺应用',
    is_default: draft.is_default,
  };
}

function normalizeDingtalkConfig(input: unknown): DingTalkConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_DINGTALK_CONF;
  }
  const data = input as Record<string, unknown>;
  const allowFromRaw = Array.isArray(data.allow_from) ? data.allow_from : [];
  const allowFrom = allowFromRaw
    .map((item) => String(item ?? '').trim())
    .filter((item) => item.length > 0);
  return {
    enabled: Boolean(data.enabled),
    client_id: String(data.client_id ?? '').trim(),
    client_secret: String(data.client_secret ?? '').trim(),
    allow_from: allowFrom,
  };
}

function draftFromDingtalkConfig(conf: DingTalkConfig): DingTalkDraft {
  return {
    enabled: conf.enabled,
    client_id: conf.client_id,
    client_secret: conf.client_secret,
    allow_from: conf.allow_from.join('\n'),
  };
}

function buildDingtalkPayload(draft: DingTalkDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    client_id: draft.client_id.trim(),
    client_secret: draft.client_secret.trim(),
    allow_from: normalizeAllowFromText(draft.allow_from),
  };
}

function normalizeTelegramConfig(input: unknown): TelegramConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_TELEGRAM_CONF;
  }
  const data = input as Record<string, unknown>;
  const allowFromRaw = Array.isArray(data.allow_from) ? data.allow_from : [];
  const allowFrom = allowFromRaw
    .map((item) => String(item ?? '').trim())
    .filter((item) => item.length > 0);
  return {
    enabled: Boolean(data.enabled),
    bot_token: String(data.bot_token ?? '').trim(),
    allow_from: allowFrom,
    parse_mode: String(data.parse_mode ?? 'Markdown').trim(),
    group_chat_mode: String(data.group_chat_mode ?? 'mention').trim(),
  };
}

function draftFromTelegramConfig(conf: TelegramConfig): TelegramDraft {
  return {
    enabled: conf.enabled,
    bot_token: conf.bot_token,
    allow_from: conf.allow_from.join('\n'),
    parse_mode: conf.parse_mode,
    group_chat_mode: conf.group_chat_mode,
  };
}

function buildTelegramPayload(draft: TelegramDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    bot_token: draft.bot_token.trim(),
    allow_from: normalizeAllowFromText(draft.allow_from),
    parse_mode: draft.parse_mode.trim(),
    group_chat_mode: draft.group_chat_mode.trim(),
  };
}

function isSensitiveDiscordField(field: keyof DiscordDraft): boolean {
  return field === 'bot_token';
}

/** Match backend discord_conf bool parsing (true / 1 / "true" / "1"). */
function parseDiscordBoolFlag(value: unknown): boolean {
  if (value === true || value === 1) return true;
  if (value === false || value === 0) return false;
  const s = String(value).trim().toLowerCase();
  return s === 'true' || s === '1';
}

function normalizeDiscordConfig(input: unknown): DiscordConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_DISCORD_CONF;
  }
  const data = input as Record<string, unknown>;
  const allowFromRaw = Array.isArray(data.allow_from) ? data.allow_from : [];
  const allowFrom = allowFromRaw
    .map((item) => String(item ?? '').trim())
    .filter((item) => item.length > 0);
  return {
    enabled: Boolean(data.enabled),
    bot_token: String(data.bot_token ?? '').trim(),
    application_id: String(data.application_id ?? '').trim(),
    guild_id: String(data.guild_id ?? '').trim(),
    channel_id: String(data.channel_id ?? '').trim(),
    block_dm: parseDiscordBoolFlag(data.block_dm),
    allow_from: allowFrom,
  };
}

function draftFromDiscordConfig(conf: DiscordConfig): DiscordDraft {
  return {
    enabled: conf.enabled,
    bot_token: conf.bot_token,
    application_id: conf.application_id,
    guild_id: conf.guild_id,
    channel_id: conf.channel_id,
    block_dm: conf.block_dm,
    allow_from: conf.allow_from.join('\n'),
  };
}

function buildDiscordPayload(draft: DiscordDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    bot_token: draft.bot_token.trim(),
    application_id: draft.application_id.trim(),
    guild_id: draft.guild_id.trim(),
    channel_id: draft.channel_id.trim(),
    block_dm: draft.block_dm,
    allow_from: normalizeAllowFromText(draft.allow_from),
  };
}

function isSensitiveSlackField(field: keyof SlackDraft): boolean {
  return field === 'bot_token' || field === 'app_token';
}

function normalizeSlackConfig(input: unknown): SlackConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_SLACK_CONF;
  }
  const data = input as Record<string, unknown>;
  const normalizeList = (value: unknown): string[] =>
    (Array.isArray(value) ? value : [])
      .map((item) => String(item ?? '').trim())
      .filter((item) => item.length > 0);
  return {
    enabled: Boolean(data.enabled),
    bot_token: String(data.bot_token ?? '').trim(),
    app_token: String(data.app_token ?? '').trim(),
    allow_from: normalizeList(data.allow_from),
    allowed_channel_ids: normalizeList(data.allowed_channel_ids),
    default_channel_id: String(data.default_channel_id ?? '').trim(),
    reply_in_thread: data.reply_in_thread === undefined ? true : Boolean(data.reply_in_thread),
  };
}

function draftFromSlackConfig(conf: SlackConfig): SlackDraft {
  return {
    enabled: conf.enabled,
    bot_token: conf.bot_token,
    app_token: conf.app_token,
    allow_from: conf.allow_from.join('\n'),
    allowed_channel_ids: conf.allowed_channel_ids.join('\n'),
    default_channel_id: conf.default_channel_id,
    reply_in_thread: conf.reply_in_thread,
  };
}

function buildSlackPayload(draft: SlackDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    bot_token: draft.bot_token.trim(),
    app_token: draft.app_token.trim(),
    allow_from: normalizeAllowFromText(draft.allow_from),
    allowed_channel_ids: normalizeAllowFromText(draft.allowed_channel_ids),
    default_channel_id: draft.default_channel_id.trim(),
    reply_in_thread: draft.reply_in_thread,
  };
}

function normalizeWhatsAppConfig(input: unknown): WhatsAppConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_WHATSAPP_CONF;
  }
  const data = input as Record<string, unknown>;
  const allowFromRaw = Array.isArray(data.allow_from) ? data.allow_from : [];
  const allowFrom = allowFromRaw
    .map((item) => String(item ?? '').trim())
    .filter((item) => item.length > 0);
  return {
    enabled: Boolean(data.enabled),
    bridge_ws_url: String(data.bridge_ws_url ?? 'ws://127.0.0.1:19600/ws').trim(),
    default_jid: String(data.default_jid ?? '').trim(),
    allow_from: allowFrom,
    enable_streaming: data.enable_streaming === undefined ? true : Boolean(data.enable_streaming),
    auto_start_bridge: Boolean(data.auto_start_bridge),
    bridge_command: String(data.bridge_command ?? '').trim(),
    bridge_workdir: String(data.bridge_workdir ?? '').trim(),
  };
}

function draftFromWhatsAppConfig(conf: WhatsAppConfig): WhatsAppDraft {
  return {
    enabled: conf.enabled,
    bridge_ws_url: conf.bridge_ws_url,
    default_jid: conf.default_jid,
    allow_from: conf.allow_from.join('\n'),
    enable_streaming: conf.enable_streaming,
    auto_start_bridge: conf.auto_start_bridge,
    bridge_command: conf.bridge_command,
    bridge_workdir: conf.bridge_workdir,
  };
}

function buildWhatsAppPayload(draft: WhatsAppDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    bridge_ws_url: draft.bridge_ws_url.trim(),
    default_jid: draft.default_jid.trim(),
    allow_from: normalizeAllowFromText(draft.allow_from),
    enable_streaming: draft.enable_streaming,
    auto_start_bridge: draft.auto_start_bridge,
    bridge_command: draft.bridge_command.trim(),
    bridge_workdir: draft.bridge_workdir.trim(),
  };
}

function normalizeWecomConfig(input: unknown): WecomConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_WECOM_CONF;
  }
  const data = input as Record<string, unknown>;
  const allowFromRaw = Array.isArray(data.allow_from) ? data.allow_from : [];
  const allowFrom = allowFromRaw
    .map((item) => String(item ?? '').trim())
    .filter((item) => item.length > 0);
  return {
    enabled: Boolean(data.enabled),
    bot_id: String(data.bot_id ?? '').trim(),
    secret: String(data.secret ?? '').trim(),
    ws_url: String(data.ws_url ?? 'wss://openws.work.weixin.qq.com').trim(),
    allow_from: allowFrom,
    enable_streaming: data.enable_streaming === undefined ? true : Boolean(data.enable_streaming),
    send_thinking_message: data.send_thinking_message === undefined ? true : Boolean(data.send_thinking_message),
    default_chat_id: String(data.default_chat_id ?? data.last_chat_id ?? '').trim(),
    group_digital_avatar: Boolean(data.group_digital_avatar),
    my_user_id: String(data.my_user_id ?? '').trim(),
    bot_name: String(data.bot_name ?? '').trim(),
    enable_memory: Boolean(data.enable_memory),
  };
}

function draftFromWecomConfig(conf: WecomConfig): WecomDraft {
  return {
    enabled: conf.enabled,
    bot_id: conf.bot_id,
    secret: conf.secret,
    ws_url: conf.ws_url,
    allow_from: conf.allow_from.join('\n'),
    enable_streaming: conf.enable_streaming,
    send_thinking_message: conf.send_thinking_message,
    default_chat_id: conf.default_chat_id,
    group_digital_avatar: conf.group_digital_avatar,
    my_user_id: conf.my_user_id,
    bot_name: conf.bot_name,
    enable_memory: conf.enable_memory,
  };
}

function buildWecomPayload(draft: WecomDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    bot_id: draft.bot_id.trim(),
    secret: draft.secret.trim(),
    allow_from: normalizeAllowFromText(draft.allow_from),
    default_chat_id: draft.default_chat_id.trim(),
    group_digital_avatar: draft.group_digital_avatar,
    my_user_id: draft.my_user_id.trim(),
    bot_name: draft.bot_name.trim(),
    enable_memory: draft.enable_memory,
  };
}

function isSensitiveWecomField(field: keyof WecomDraft): boolean {
  return field === 'secret';
}
function VisibilityIcon({ visible }: { visible: boolean }) {
  return visible ? (
    <svg className="channels-panel__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 3l18 18" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M10.58 10.58A2 2 0 0013.42 13.42" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.88 5.09A10.94 10.94 0 0112 4.9c5.05 0 9.27 3.11 10.5 7.5a11.6 11.6 0 01-3.06 4.88" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M6.61 6.61A11.6 11.6 0 001.5 12.4c.53 1.9 1.63 3.56 3.11 4.79" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M14.12 14.12a3 3 0 01-4.24-4.24" />
    </svg>
  ) : (
    <svg className="channels-panel__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M1.5 12s3.75-7.5 10.5-7.5S22.5 12 22.5 12s-3.75 7.5-10.5 7.5S1.5 12 1.5 12z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function ChannelLogo({ channel, label }: { channel: ChannelItem; label: string }) {
  if (channel.logo_src) {
    return (
      <img
        src={channel.logo_src}
        alt={`${label} logo`}
        className="h-6 w-6 rounded-md border border-border object-contain bg-card"
      />
    );
  }
  return (
    <span className="h-6 w-6 rounded-md border border-border bg-card flex items-center justify-center text-text-muted">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="h-5 w-5">
        <circle cx="12" cy="12" r="9" />
        <path strokeLinecap="round" d="M3 12h18M12 3c2.5 2.2 4 5.5 4 9s-1.5 6.8-4 9m0-18c-2.5 2.2-4 5.5-4 9s1.5 6.8 4 9" />
      </svg>
    </span>
  );
}

function ChannelHeaderLogo({ channelId, label }: { channelId: SupportedChannelId; label: string }) {
  const logoSrc = SUPPORTED_CHANNELS.find((channel) => channel.channel_id === channelId)?.logo_src ?? null;
  if (logoSrc) {
    return (
      <img
        src={logoSrc}
        alt={`${label} logo`}
        className="h-9 w-9 rounded-lg border border-border object-contain bg-card"
      />
    );
  }
  return (
    <span className="h-9 w-9 rounded-lg border border-border bg-card flex items-center justify-center text-text-muted">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="h-7 w-7">
        <circle cx="12" cy="12" r="9" />
        <path strokeLinecap="round" d="M3 12h18M12 3c2.5 2.2 4 5.5 4 9s-1.5 6.8-4 9m0-18c-2.5 2.2-4 5.5-4 9s1.5 6.8 4 9" />
      </svg>
    </span>
  );
}

export function ChannelsPanel({ isConnected }: ChannelsPanelProps) {
  const { t, i18n } = useTranslation();
  const [channels, setChannels] = useState<ChannelItem[]>(() => buildChannels([]));
  const [activeChannelId, setActiveChannelId] = useState<SupportedChannelId>('xiaoyi');
  const [loadState, setLoadState] = useState<LoadState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);

  const [feishuApps, setFeishuApps] = useState<FeishuAppConfig[]>(() =>
    normalizeFeishuAppsConfig(DEFAULT_FEISHU_CONF),
  );
  const [feishuDraftApps, setFeishuDraftApps] = useState<FeishuAppDraft[]>(() =>
    normalizeFeishuAppsConfig(DEFAULT_FEISHU_CONF).map(draftFromFeishuAppConfig),
  );
  const [expandedFeishuAppIndex, setExpandedFeishuAppIndex] = useState(0);
  const [visibleFields, setVisibleFields] = useState<Record<string, boolean>>({});
  const [feishuLoading, setFeishuLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [xiaoyiApp, setXiaoyiApp] = useState<XiaoyiAppConfig>(() =>
    normalizeSingleXiaoyiAppConfig(DEFAULT_XIAOYI_CONF),
  );
  const [xiaoyiDraft, setXiaoyiDraft] = useState<XiaoyiAppDraft>(() =>
    draftFromXiaoyiAppConfig(normalizeSingleXiaoyiAppConfig(DEFAULT_XIAOYI_CONF)),
  );
  const [xiaoyiVisibleFields, setXiaoyiVisibleFields] = useState<Record<string, boolean>>({});
  const [xiaoyiLoading, setXiaoyiLoading] = useState(false);
  const [xiaoyiSaving, setXiaoyiSaving] = useState(false);
  const [xiaoyiSaveError, setXiaoyiSaveError] = useState<string | null>(null);
  const [xiaoyiSuccess, setXiaoyiSuccess] = useState<string | null>(null);
  const [xiaoyiApiIdHintDismissed, setXiaoyiApiIdHintDismissed] = useState(false);
  const [dingtalkConfig, setDingtalkConfig] = useState<DingTalkConfig>(DEFAULT_DINGTALK_CONF);
  const [dingtalkDraft, setDingtalkDraft] = useState<DingTalkDraft>(draftFromDingtalkConfig(DEFAULT_DINGTALK_CONF));
  const [dingtalkVisibleFields, setDingtalkVisibleFields] = useState<Record<string, boolean>>({});
  const [dingtalkLoading, setDingtalkLoading] = useState(false);
  const [dingtalkSaving, setDingtalkSaving] = useState(false);
  const [dingtalkSaveError, setDingtalkSaveError] = useState<string | null>(null);
  const [dingtalkSuccess, setDingtalkSuccess] = useState<string | null>(null);
  const [telegramConfig, setTelegramConfig] = useState<TelegramConfig>(DEFAULT_TELEGRAM_CONF);
  const [telegramDraft, setTelegramDraft] = useState<TelegramDraft>(draftFromTelegramConfig(DEFAULT_TELEGRAM_CONF));
  const [telegramVisibleFields, setTelegramVisibleFields] = useState<Record<string, boolean>>({});
  const [telegramLoading, setTelegramLoading] = useState(false);
  const [telegramSaving, setTelegramSaving] = useState(false);
  const [telegramSaveError, setTelegramSaveError] = useState<string | null>(null);
  const [telegramSuccess, setTelegramSuccess] = useState<string | null>(null);
  const [discordConfig, setDiscordConfig] = useState<DiscordConfig>(DEFAULT_DISCORD_CONF);
  const [discordDraft, setDiscordDraft] = useState<DiscordDraft>(draftFromDiscordConfig(DEFAULT_DISCORD_CONF));
  const [discordVisibleFields, setDiscordVisibleFields] = useState<Record<string, boolean>>({});
  const [discordLoading, setDiscordLoading] = useState(false);
  const [discordSaving, setDiscordSaving] = useState(false);
  const [discordSaveError, setDiscordSaveError] = useState<string | null>(null);
  const [discordSuccess, setDiscordSuccess] = useState<string | null>(null);
  const [slackConfig, setSlackConfig] = useState<SlackConfig>(DEFAULT_SLACK_CONF);
  const [slackDraft, setSlackDraft] = useState<SlackDraft>(draftFromSlackConfig(DEFAULT_SLACK_CONF));
  const [slackVisibleFields, setSlackVisibleFields] = useState<Record<string, boolean>>({});
  const [slackLoading, setSlackLoading] = useState(false);
  const [slackSaving, setSlackSaving] = useState(false);
  const [slackSaveError, setSlackSaveError] = useState<string | null>(null);
  const [slackSuccess, setSlackSuccess] = useState<string | null>(null);
  const [whatsappConfig, setWhatsappConfig] = useState<WhatsAppConfig>(DEFAULT_WHATSAPP_CONF);
  const [whatsappDraft, setWhatsappDraft] = useState<WhatsAppDraft>(draftFromWhatsAppConfig(DEFAULT_WHATSAPP_CONF));
  const [whatsappLoading, setWhatsappLoading] = useState(false);
  const [whatsappSaving, setWhatsappSaving] = useState(false);
  const [whatsappSaveError, setWhatsappSaveError] = useState<string | null>(null);
  const [whatsappSuccess, setWhatsappSuccess] = useState<string | null>(null);
  const [wecomConfig, setWecomConfig] = useState<WecomConfig>(DEFAULT_WECOM_CONF);
  const [wecomDraft, setWecomDraft] = useState<WecomDraft>(draftFromWecomConfig(DEFAULT_WECOM_CONF));
  const [wecomVisibleFields, setWecomVisibleFields] = useState<Record<string, boolean>>({});
  const [wecomLoading, setWecomLoading] = useState(false);
  const [wecomSaving, setWecomSaving] = useState(false);
  const [wecomSaveError, setWecomSaveError] = useState<string | null>(null);
  const [wecomSuccess, setWecomSuccess] = useState<string | null>(null);
  const [wechatConfig, setWechatConfig] = useState<WechatConfig>(DEFAULT_WECHAT_CONF);
  const [wechatDraft, setWechatDraft] = useState<WechatDraft>(draftFromWechatConfig(DEFAULT_WECHAT_CONF));
  const [wechatLoading, setWechatLoading] = useState(false);
  const [wechatSaving, setWechatSaving] = useState(false);
  const [wechatUnbinding, setWechatUnbinding] = useState(false);
  const [wechatUnbindConfirmOpen, setWechatUnbindConfirmOpen] = useState(false);
  const [wechatSaveError, setWechatSaveError] = useState<string | null>(null);
  const [wechatSuccess, setWechatSuccess] = useState<string | null>(null);
  const [wechatQrModalOpen, setWechatQrModalOpen] = useState(false);
  const [wechatLoginUi, setWechatLoginUi] = useState<WechatLoginUiState | null>(null);
  const wechatLoginPollAppliedAt = useRef<number | null>(null);
  const wechatLoginPollInFlight = useRef(false);

  const fetchChannels = useCallback(async () => {
    setLoadState('loading');
    setError(null);
    try {
      const payload = await webRequest<{ channels?: unknown[] }>('channel.get');
      setChannels(buildChannels(payload?.channels));
      setLoadState('success');
      setLastUpdatedAt(new Date().toISOString());
    } catch (err) {
      setChannels(buildChannels([]));
      setLoadState('error');
      setError(err instanceof Error ? err.message : t('channels.errors.loadChannels'));
    }
  }, [t]);

  useEffect(() => {
    void fetchChannels();
  }, [fetchChannels]);

  const fetchFeishuConfig = useCallback(async () => {
    setFeishuLoading(true);
    setSaveError(null);
    setSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.feishu.get_conf');
      const normalized = normalizeFeishuAppsConfig(payload?.config);
      setFeishuApps(normalized);
      setFeishuDraftApps(normalized.map(draftFromFeishuAppConfig));
      setExpandedFeishuAppIndex(0);
      setVisibleFields({});
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : t('channels.errors.loadFeishu'));
    } finally {
      setFeishuLoading(false);
    }
  }, [t]);

  const fetchXiaoyiConfig = useCallback(async () => {
    setXiaoyiLoading(true);
    setXiaoyiSaveError(null);
    setXiaoyiSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.xiaoyi.get_conf');
      const normalized = normalizeSingleXiaoyiAppConfig(payload?.config);
      setXiaoyiApp(normalized);
      setXiaoyiDraft(draftFromXiaoyiAppConfig(normalized));
      setXiaoyiVisibleFields({});
    } catch (err) {
      setXiaoyiSaveError(err instanceof Error ? err.message : t('channels.errors.loadXiaoyi'));
    } finally {
      setXiaoyiLoading(false);
    }
  }, [t]);

  const fetchDingtalkConfig = useCallback(async () => {
    setDingtalkLoading(true);
    setDingtalkSaveError(null);
    setDingtalkSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.dingtalk.get_conf');
      const normalized = normalizeDingtalkConfig(payload?.config);
      setDingtalkConfig(normalized);
      setDingtalkDraft(draftFromDingtalkConfig(normalized));
      setDingtalkVisibleFields({});
    } catch (err) {
      setDingtalkSaveError(err instanceof Error ? err.message : t('channels.errors.loadDingtalk'));
    } finally {
      setDingtalkLoading(false);
    }
  }, [t]);

  const fetchTelegramConfig = useCallback(async () => {
    setTelegramLoading(true);
    setTelegramSaveError(null);
    setTelegramSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.telegram.get_conf');
      const normalized = normalizeTelegramConfig(payload?.config);
      setTelegramConfig(normalized);
      setTelegramDraft(draftFromTelegramConfig(normalized));
      setTelegramVisibleFields({});
    } catch (err) {
      setTelegramSaveError(err instanceof Error ? err.message : t('channels.errors.loadTelegram'));
    } finally {
      setTelegramLoading(false);
    }
  }, [t]);

  const fetchDiscordConfig = useCallback(async () => {
    setDiscordLoading(true);
    setDiscordSaveError(null);
    setDiscordSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.discord.get_conf');
      const normalized = normalizeDiscordConfig(payload?.config);
      setDiscordConfig(normalized);
      setDiscordDraft(draftFromDiscordConfig(normalized));
      setDiscordVisibleFields({});
    } catch (err) {
      setDiscordSaveError(err instanceof Error ? err.message : t('channels.errors.loadDiscord'));
    } finally {
      setDiscordLoading(false);
    }
  }, [t]);

  const fetchSlackConfig = useCallback(async () => {
    setSlackLoading(true);
    setSlackSaveError(null);
    setSlackSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.slack.get_conf');
      const normalized = normalizeSlackConfig(payload?.config);
      setSlackConfig(normalized);
      setSlackDraft(draftFromSlackConfig(normalized));
      setSlackVisibleFields({});
    } catch (err) {
      setSlackSaveError(err instanceof Error ? err.message : t('channels.errors.loadSlack'));
    } finally {
      setSlackLoading(false);
    }
  }, [t]);

  const fetchWhatsAppConfig = useCallback(async () => {
    setWhatsappLoading(true);
    setWhatsappSaveError(null);
    setWhatsappSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.whatsapp.get_conf');
      const normalized = normalizeWhatsAppConfig(payload?.config);
      setWhatsappConfig(normalized);
      setWhatsappDraft(draftFromWhatsAppConfig(normalized));
    } catch (err) {
      setWhatsappSaveError(err instanceof Error ? err.message : t('channels.errors.loadWhatsApp'));
    } finally {
      setWhatsappLoading(false);
    }
  }, [t]);
  
  const fetchWecomConfig = useCallback(async () => {
    setWecomLoading(true);
    setWecomSaveError(null);
    setWecomSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.wecom.get_conf');
      const normalized = normalizeWecomConfig(payload?.config);
      setWecomConfig(normalized);
      setWecomDraft(draftFromWecomConfig(normalized));
      setWecomVisibleFields({});
    } catch (err) {
      setWecomSaveError(err instanceof Error ? err.message : t('channels.errors.loadWecom'));
    } finally {
      setWecomLoading(false);
    }
  }, [t]);

  const fetchWechatConfig = useCallback(async () => {
    setWechatLoading(true);
    setWechatSaveError(null);
    setWechatSuccess(null);
    try {
      const payload = await webRequest<{ config?: unknown }>('channel.wechat.get_conf');
      const normalized = normalizeWechatConfig(payload?.config);
      setWechatConfig(normalized);
      setWechatDraft(draftFromWechatConfig(normalized));
    } catch (err) {
      setWechatSaveError(err instanceof Error ? err.message : t('channels.errors.loadWechat'));
    } finally {
      setWechatLoading(false);
    }
  }, [t]);

  const handleSelectChannel = useCallback(
    (channelId: SupportedChannelId) => {
      if (ADAPTING_CHANNEL_IDS.has(channelId)) {
        return;
      }
      setActiveChannelId(channelId);
    },
    [],
  );

  useEffect(() => {
    if (activeChannelId === 'feishu') {
      void fetchFeishuConfig();
      return;
    }
    if (activeChannelId === 'xiaoyi') {
      void fetchXiaoyiConfig();
      return;
    }
    if (activeChannelId === 'dingtalk') {
      void fetchDingtalkConfig();
      return;
    }
    if (activeChannelId === 'telegram') {
      void fetchTelegramConfig();
      return;
    }
    if (activeChannelId === 'discord') {
      void fetchDiscordConfig();
      return;
    }
    if (activeChannelId === 'slack') {
      void fetchSlackConfig();
      return;
    }
    if (activeChannelId === 'whatsapp') {
      void fetchWhatsAppConfig();
    }
    if ((activeChannelId as string) === 'wecom') {
      void fetchWecomConfig();
      return;
    }
    if ((activeChannelId as string) === 'wechat') {
      void fetchWechatConfig();
    }
  }, [
    activeChannelId,
    fetchDiscordConfig,
    fetchDingtalkConfig,
    fetchFeishuConfig,
    fetchTelegramConfig,
    fetchSlackConfig,
    fetchWhatsAppConfig,
    fetchWechatConfig,
    fetchXiaoyiConfig,
    fetchWecomConfig,
  ]);

  useEffect(() => {
    if (!wechatQrModalOpen || !isConnected) {
      return;
    }
    let cancelled = false;
    const poll = async () => {
      if (wechatLoginPollInFlight.current) {
        return;
      }
      wechatLoginPollInFlight.current = true;
      try {
        const raw = await webRequest<unknown>('channel.wechat.get_login_ui');
        if (cancelled) return;
        const data = normalizeWechatLoginUi(raw);
        setWechatLoginUi(data);
        if (data.phase === 'success' && data.credentials) {
          const at = data.updated_at;
          if (wechatLoginPollAppliedAt.current !== at) {
            wechatLoginPollAppliedAt.current = at;
            const c = data.credentials;
            setWechatDraft((prev) => ({
              ...prev,
              bot_token: c.bot_token !== undefined ? c.bot_token : prev.bot_token,
              base_url: (c.base_url !== undefined ? String(c.base_url).trim() || prev.base_url : prev.base_url).trim(),
              ilink_bot_id: c.ilink_bot_id !== undefined ? c.ilink_bot_id : prev.ilink_bot_id,
              ilink_user_id: c.ilink_user_id !== undefined ? c.ilink_user_id : prev.ilink_user_id,
            }));
            setWechatSuccess(t('channels.wechatLogin.filledDraft'));
          }
        }
      } catch {
        if (!cancelled) {
          setWechatLoginUi(null);
        }
      } finally {
        wechatLoginPollInFlight.current = false;
      }
    };
    void poll();
    const id = window.setInterval(poll, 1500);
    return () => {
      cancelled = true;
      wechatLoginPollInFlight.current = false;
      window.clearInterval(id);
    };
  }, [wechatQrModalOpen, isConnected, t]);

  const statusText = useMemo(() => {
    const enabledCount = channels.filter((channel) => channel.enabled).length;
    if (loadState === 'loading') {
      return t('common.loading');
    }
    if (loadState === 'error') {
      return t('channels.status.loadFailed');
    }
    return t('channels.status.enabledSummary', { enabledCount, total: channels.length });
  }, [channels, loadState, t]);

  const hasConfigChanges = useMemo(() => {
    return JSON.stringify(feishuApps) !== JSON.stringify(feishuDraftApps.map(buildFeishuAppConfig));
  }, [feishuApps, feishuDraftApps]);
  const hasXiaoyiConfigChanges = useMemo(() => {
    return JSON.stringify(xiaoyiApp) !== JSON.stringify(buildXiaoyiAppConfig(xiaoyiDraft));
  }, [xiaoyiApp, xiaoyiDraft]);
  const hasDingtalkConfigChanges = useMemo(() => {
    const baseDraft = draftFromDingtalkConfig(dingtalkConfig);
    return (
      baseDraft.enabled !== dingtalkDraft.enabled ||
      baseDraft.client_id !== dingtalkDraft.client_id ||
      baseDraft.client_secret !== dingtalkDraft.client_secret ||
      normalizeAllowFromText(baseDraft.allow_from).join('\n') !== normalizeAllowFromText(dingtalkDraft.allow_from).join('\n')
    );
  }, [dingtalkConfig, dingtalkDraft]);

  const hasTelegramConfigChanges = useMemo(() => {
    const baseDraft = draftFromTelegramConfig(telegramConfig);
    return (
      baseDraft.enabled !== telegramDraft.enabled ||
      baseDraft.bot_token !== telegramDraft.bot_token ||
      normalizeAllowFromText(baseDraft.allow_from).join('\n') !== normalizeAllowFromText(telegramDraft.allow_from).join('\n') ||
      baseDraft.parse_mode !== telegramDraft.parse_mode ||
      baseDraft.group_chat_mode !== telegramDraft.group_chat_mode
    );
  }, [telegramConfig, telegramDraft]);
  const hasDiscordConfigChanges = useMemo(() => {
    const baseDraft = draftFromDiscordConfig(discordConfig);
    return (
      baseDraft.enabled !== discordDraft.enabled ||
      baseDraft.bot_token !== discordDraft.bot_token ||
      baseDraft.application_id !== discordDraft.application_id ||
      baseDraft.guild_id !== discordDraft.guild_id ||
      baseDraft.channel_id !== discordDraft.channel_id ||
      baseDraft.block_dm !== discordDraft.block_dm ||
      normalizeAllowFromText(baseDraft.allow_from).join('\n') !== normalizeAllowFromText(discordDraft.allow_from).join('\n')
    );
  }, [discordConfig, discordDraft]);
  const hasSlackConfigChanges = useMemo(() => {
    const baseDraft = draftFromSlackConfig(slackConfig);
    return (
      baseDraft.enabled !== slackDraft.enabled ||
      baseDraft.bot_token !== slackDraft.bot_token ||
      baseDraft.app_token !== slackDraft.app_token ||
      normalizeAllowFromText(baseDraft.allow_from).join('\n') !== normalizeAllowFromText(slackDraft.allow_from).join('\n') ||
      normalizeAllowFromText(baseDraft.allowed_channel_ids).join('\n') !==
        normalizeAllowFromText(slackDraft.allowed_channel_ids).join('\n') ||
      baseDraft.default_channel_id !== slackDraft.default_channel_id ||
      baseDraft.reply_in_thread !== slackDraft.reply_in_thread
    );
  }, [slackConfig, slackDraft]);
  const hasWhatsAppConfigChanges = useMemo(() => {
    const baseDraft = draftFromWhatsAppConfig(whatsappConfig);
    return (
      baseDraft.enabled !== whatsappDraft.enabled ||
      baseDraft.bridge_ws_url !== whatsappDraft.bridge_ws_url ||
      baseDraft.default_jid !== whatsappDraft.default_jid ||
      normalizeAllowFromText(baseDraft.allow_from).join('\n') !== normalizeAllowFromText(whatsappDraft.allow_from).join('\n') ||
      baseDraft.enable_streaming !== whatsappDraft.enable_streaming ||
      baseDraft.auto_start_bridge !== whatsappDraft.auto_start_bridge ||
      baseDraft.bridge_command !== whatsappDraft.bridge_command ||
      baseDraft.bridge_workdir !== whatsappDraft.bridge_workdir
    );
  }, [whatsappConfig, whatsappDraft]);
  const hasWecomConfigChanges = useMemo(() => {
    const baseDraft = draftFromWecomConfig(wecomConfig);
    return (
      baseDraft.enabled !== wecomDraft.enabled ||
      baseDraft.bot_id !== wecomDraft.bot_id ||
      baseDraft.secret !== wecomDraft.secret ||
      baseDraft.default_chat_id !== wecomDraft.default_chat_id ||
      normalizeAllowFromText(baseDraft.allow_from).join('\n') !== normalizeAllowFromText(wecomDraft.allow_from).join('\n') ||
      baseDraft.group_digital_avatar !== wecomDraft.group_digital_avatar ||
      baseDraft.my_user_id !== wecomDraft.my_user_id ||
      baseDraft.bot_name !== wecomDraft.bot_name ||
      baseDraft.enable_memory !== wecomDraft.enable_memory
    );
  }, [wecomConfig, wecomDraft]);

  const hasWechatConfigChanges = useMemo(() => {
    const baseDraft = draftFromWechatConfig(wechatConfig);
    return (
      baseDraft.enabled !== wechatDraft.enabled ||
      baseDraft.base_url !== wechatDraft.base_url ||
      baseDraft.bot_token !== wechatDraft.bot_token ||
      baseDraft.ilink_bot_id !== wechatDraft.ilink_bot_id ||
      baseDraft.ilink_user_id !== wechatDraft.ilink_user_id ||
      normalizeAllowFromText(baseDraft.allow_from).join('\n') !==
        normalizeAllowFromText(wechatDraft.allow_from).join('\n') ||
      baseDraft.auto_login !== wechatDraft.auto_login ||
      baseDraft.enable_streaming !== wechatDraft.enable_streaming ||
      baseDraft.qrcode_poll_interval_sec !== wechatDraft.qrcode_poll_interval_sec ||
      baseDraft.long_poll_timeout_sec !== wechatDraft.long_poll_timeout_sec ||
      baseDraft.backoff_base_sec !== wechatDraft.backoff_base_sec ||
      baseDraft.backoff_max_sec !== wechatDraft.backoff_max_sec ||
      baseDraft.credential_file !== wechatDraft.credential_file
    );
  }, [wechatConfig, wechatDraft]);

  const handleFeishuAppFieldChange = <K extends keyof FeishuAppDraft>(
    index: number,
    key: K,
    value: FeishuAppDraft[K],
  ) => {
    setFeishuDraftApps((prev) => prev.map((app, i) => (i === index ? { ...app, [key]: value } : app)));
    if (saveError) {
      setSaveError(null);
    }
    if (success) {
      setSuccess(null);
    }
  };

  const handleCancelConfig = () => {
    if (!hasConfigChanges) return;
    setFeishuDraftApps(feishuApps.map(draftFromFeishuAppConfig));
    setSaveError(null);
    setSuccess(null);
  };

  const handleAddFeishuApp = () => {
    setFeishuDraftApps((prev) => {
      const next = [
        ...prev,
        {
          ...draftFromFeishuAppConfig({
            ...DEFAULT_FEISHU_CONF,
            name: t('channels.feishuApps.appNameTemplate', { index: prev.length + 1 }),
            is_default: false,
          }),
        },
      ];
      setExpandedFeishuAppIndex(next.length - 1);
      return next;
    });
    setSaveError(null);
    setSuccess(null);
  };

  const handleDeleteFeishuApp = (index: number) => {
    setFeishuDraftApps((prev) => {
      if (prev.length <= 1) return prev;
      const next = prev.filter((_, i) => i !== index);
      if (!next.some((app) => app.is_default) && next.length > 0) {
        next[0] = { ...next[0], is_default: true };
      }
      setExpandedFeishuAppIndex((current) => Math.max(0, Math.min(current >= index ? current - 1 : current, next.length - 1)));
      return next;
    });
    setSaveError(null);
    setSuccess(null);
  };

  const handleSetDefaultFeishuApp = (index: number) => {
    setFeishuDraftApps((prev) => prev.map((app, i) => ({ ...app, is_default: i === index })));
    setSaveError(null);
    setSuccess(null);
  };

  const draft =
    feishuDraftApps[0] ??
    draftFromFeishuAppConfig({ ...DEFAULT_FEISHU_CONF, name: t('channels.feishuApps.defaultAppName'), is_default: true });

  const handleFieldChange = <K extends keyof FeishuDraft>(key: K, value: FeishuDraft[K]) => {
    handleFeishuAppFieldChange(0, key, value);
  };

  const toggleFieldVisible = (field: string) => {
    setVisibleFields((prev) => ({ ...prev, [field]: !prev[field] }));
  };

  const handleXiaoyiFieldChange = <K extends keyof XiaoyiAppDraft>(key: K, value: XiaoyiAppDraft[K]) => {
    setXiaoyiDraft((prev) => ({ ...prev, [key]: value }));
    setXiaoyiSaveError(null);
    setXiaoyiSuccess(null);
    // 填入 api_id 后重置关闭状态，清空时警告横幅可再次出现
    if (key === 'api_id' && String(value ?? '').trim()) {
      setXiaoyiApiIdHintDismissed(false);
    }
  };

  const handleCancelXiaoyiConfig = () => {
    if (!hasXiaoyiConfigChanges) return;
    setXiaoyiDraft(draftFromXiaoyiAppConfig(xiaoyiApp));
    setXiaoyiSaveError(null);
    setXiaoyiSuccess(null);
  };

  const toggleXiaoyiFieldVisible = (field: string) => {
    setXiaoyiVisibleFields((prev) => ({ ...prev, [field]: !prev[field] }));
  };

  const handleDingtalkFieldChange = <K extends keyof DingTalkDraft>(key: K, value: DingTalkDraft[K]) => {
    setDingtalkDraft((prev) => ({ ...prev, [key]: value }));
    if (dingtalkSaveError) {
      setDingtalkSaveError(null);
    }
    if (dingtalkSuccess) {
      setDingtalkSuccess(null);
    }
  };

  const handleCancelDingtalkConfig = () => {
    if (!hasDingtalkConfigChanges) return;
    setDingtalkDraft(draftFromDingtalkConfig(dingtalkConfig));
    setDingtalkSaveError(null);
    setDingtalkSuccess(null);
  };

  const toggleDingtalkFieldVisible = (field: keyof DingTalkDraft) => {
    setDingtalkVisibleFields((prev) => ({ ...prev, [field]: !prev[field] }));
  };

  const handleTelegramFieldChange = <K extends keyof TelegramDraft>(key: K, value: TelegramDraft[K]) => {
    setTelegramDraft((prev) => ({ ...prev, [key]: value }));
    if (telegramSaveError) {
      setTelegramSaveError(null);
    }
    if (telegramSuccess) {
      setTelegramSuccess(null);
    }
  };

  const handleCancelTelegramConfig = () => {
    if (!hasTelegramConfigChanges) return;
    setTelegramDraft(draftFromTelegramConfig(telegramConfig));
    setTelegramSaveError(null);
    setTelegramSuccess(null);
  };

  const toggleTelegramFieldVisible = (field: keyof TelegramDraft) => {
    setTelegramVisibleFields((prev) => ({ ...prev, [field]: !prev[field] }));
  };

  const handleDiscordFieldChange = <K extends keyof DiscordDraft>(key: K, value: DiscordDraft[K]) => {
    setDiscordDraft((prev) => ({ ...prev, [key]: value }));
    if (discordSaveError) {
      setDiscordSaveError(null);
    }
    if (discordSuccess) {
      setDiscordSuccess(null);
    }
  };

  const handleCancelDiscordConfig = () => {
    if (!hasDiscordConfigChanges) return;
    setDiscordDraft(draftFromDiscordConfig(discordConfig));
    setDiscordSaveError(null);
    setDiscordSuccess(null);
  };

  const toggleDiscordFieldVisible = (field: keyof DiscordDraft) => {
    setDiscordVisibleFields((prev) => ({ ...prev, [field]: !prev[field] }));
  };

  const handleSlackFieldChange = <K extends keyof SlackDraft>(key: K, value: SlackDraft[K]) => {
    setSlackDraft((prev) => ({ ...prev, [key]: value }));
    if (slackSaveError) {
      setSlackSaveError(null);
    }
    if (slackSuccess) {
      setSlackSuccess(null);
    }
  };

  const handleCancelSlackConfig = () => {
    if (!hasSlackConfigChanges) return;
    setSlackDraft(draftFromSlackConfig(slackConfig));
    setSlackSaveError(null);
    setSlackSuccess(null);
  };

  const toggleSlackFieldVisible = (field: keyof SlackDraft) => {
    setSlackVisibleFields((prev) => ({ ...prev, [field]: !prev[field] }));
  };

  const handleWhatsAppFieldChange = <K extends keyof WhatsAppDraft>(key: K, value: WhatsAppDraft[K]) => {
    setWhatsappDraft((prev) => ({ ...prev, [key]: value }));
    if (whatsappSaveError) setWhatsappSaveError(null);
    if (whatsappSuccess) setWhatsappSuccess(null);
  };

  const handleCancelWhatsAppConfig = () => {
    if (!hasWhatsAppConfigChanges) return;
    setWhatsappDraft(draftFromWhatsAppConfig(whatsappConfig));
    setWhatsappSaveError(null);
    setWhatsappSuccess(null);
  };

  const handleSaveWhatsAppConfig = async () => {
    if (!hasWhatsAppConfigChanges || whatsappSaving) return;
    setWhatsappSaving(true);
    setWhatsappSaveError(null);
    try {
      const payload = buildWhatsAppPayload(whatsappDraft);
      const result = await webRequest<{ config?: unknown }>('channel.whatsapp.set_conf', payload);
      const normalized = normalizeWhatsAppConfig(result?.config);
      setWhatsappConfig(normalized);
      setWhatsappDraft(draftFromWhatsAppConfig(normalized));
      setWhatsappSuccess(t('channels.saved.whatsapp'));
      void fetchChannels();
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setWhatsappSaveError(message);
    } finally {
      setWhatsappSaving(false);
    }
  };

  const handleWecomFieldChange = <K extends keyof WecomDraft>(key: K, value: WecomDraft[K]) => {
    setWecomDraft((prev) => ({ ...prev, [key]: value }));
    if (wecomSaveError) {
      setWecomSaveError(null);
    }
    if (wecomSuccess) {
      setWecomSuccess(null);
    }
  };

  const handleCancelWecomConfig = () => {
    if (!hasWecomConfigChanges) return;
    setWecomDraft(draftFromWecomConfig(wecomConfig));
    setWecomSaveError(null);
    setWecomSuccess(null);
  };

  const toggleWecomFieldVisible = (field: keyof WecomDraft) => {
    setWecomVisibleFields((prev) => ({ ...prev, [field]: !prev[field] }));
  };

  const handleWechatFieldChange = <K extends keyof WechatDraft>(key: K, value: WechatDraft[K]) => {
    setWechatDraft((prev) => ({ ...prev, [key]: value }));
    if (wechatSaveError) {
      setWechatSaveError(null);
    }
    if (wechatSuccess) {
      setWechatSuccess(null);
    }
  };

  const handleCancelWechatConfig = () => {
    if (!hasWechatConfigChanges) return;
    setWechatDraft(draftFromWechatConfig(wechatConfig));
    setWechatSaveError(null);
    setWechatSuccess(null);
  };

  const handleSaveConfig = async () => {
    if (!hasConfigChanges || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const apps = feishuDraftApps.map(buildFeishuAppConfig);
      const result = await webRequest<{ config?: unknown }>('channel.feishu.set_conf', { apps });
      const normalized = normalizeFeishuAppsConfig(result?.config);
      setFeishuApps(normalized);
      setFeishuDraftApps(normalized.map(draftFromFeishuAppConfig));
      setSuccess(t('channels.saved.feishu'));
      void fetchChannels();
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setSaveError(message);
    } finally {
      setSaving(false);
    }
  };

  const handleSaveXiaoyiConfig = async () => {
    if (!hasXiaoyiConfigChanges || xiaoyiSaving) return;
    setXiaoyiSaving(true);
    setXiaoyiSaveError(null);
    try {
      const apps = [{ ...buildXiaoyiAppConfig(xiaoyiDraft), is_default: true }];
      const result = await webRequest<{ config?: unknown }>('channel.xiaoyi.set_conf', { apps });
      const normalized = normalizeSingleXiaoyiAppConfig(result?.config);
      setXiaoyiApp(normalized);
      setXiaoyiDraft(draftFromXiaoyiAppConfig(normalized));
      setXiaoyiSuccess(t('channels.saved.xiaoyi'));
      void fetchChannels();
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setXiaoyiSaveError(message);
    } finally {
      setXiaoyiSaving(false);
    }
  };

  const handleSaveDingtalkConfig = async () => {
    if (!hasDingtalkConfigChanges || dingtalkSaving) return;
    setDingtalkSaving(true);
    setDingtalkSaveError(null);
    try {
      const payload = buildDingtalkPayload(dingtalkDraft);
      const result = await webRequest<{ config?: unknown }>('channel.dingtalk.set_conf', payload);
      const normalized = normalizeDingtalkConfig(result?.config);
      setDingtalkConfig(normalized);
      setDingtalkDraft(draftFromDingtalkConfig(normalized));
      setDingtalkSuccess(t('channels.saved.dingtalk'));
      void fetchChannels();
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setDingtalkSaveError(message);
    } finally {
      setDingtalkSaving(false);
    }
  };

  const handleSaveTelegramConfig = async () => {
    if (!hasTelegramConfigChanges || telegramSaving) return;
    setTelegramSaving(true);
    setTelegramSaveError(null);
    try {
      const payload = buildTelegramPayload(telegramDraft);
      const result = await webRequest<{ config?: unknown }>('channel.telegram.set_conf', payload);
      const normalized = normalizeTelegramConfig(result?.config);
      setTelegramConfig(normalized);
      setTelegramDraft(draftFromTelegramConfig(normalized));
      setTelegramSuccess(t('channels.saved.telegram'));
      void fetchChannels();
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setTelegramSaveError(message);
    } finally {
      setTelegramSaving(false);
    }
  };

  const handleSaveDiscordConfig = async () => {
    if (!hasDiscordConfigChanges || discordSaving) return;
    setDiscordSaving(true);
    setDiscordSaveError(null);
    try {
      const payload = buildDiscordPayload(discordDraft);
      const result = await webRequest<{ config?: unknown }>('channel.discord.set_conf', payload);
      const normalized = normalizeDiscordConfig(result?.config);
      setDiscordConfig(normalized);
      setDiscordDraft(draftFromDiscordConfig(normalized));
      setDiscordSuccess(t('channels.saved.discord'));
      void fetchChannels();
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setDiscordSaveError(message);
    } finally {
      setDiscordSaving(false);
    }
  };

  const handleSaveSlackConfig = async () => {
    if (!hasSlackConfigChanges || slackSaving) return;
    setSlackSaving(true);
    setSlackSaveError(null);
    try {
      const payload = buildSlackPayload(slackDraft);
      const result = await webRequest<{ config?: unknown }>('channel.slack.set_conf', payload);
      const normalized = normalizeSlackConfig(result?.config);
      setSlackConfig(normalized);
      setSlackDraft(draftFromSlackConfig(normalized));
      setSlackSuccess(t('channels.saved.slack'));
      void fetchChannels();
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setSlackSaveError(message);
    } finally {
      setSlackSaving(false);
    }
  };

  const handleSaveWecomConfig = async () => {
    if (!hasWecomConfigChanges || wecomSaving) return;
    setWecomSaving(true);
    setWecomSaveError(null);
    try {
      const payload = buildWecomPayload(wecomDraft);
      const result = await webRequest<{ config?: unknown }>('channel.wecom.set_conf', payload);
      const normalized = normalizeWecomConfig(result?.config);
      setWecomConfig(normalized);
      setWecomDraft(draftFromWecomConfig(normalized));
      setWecomSuccess(t('channels.saved.wecom'));
      void fetchChannels();
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setWecomSaveError(message);
    } finally {
      setWecomSaving(false);
    }
  };

  const handleSaveWechatConfig = async () => {
    if (!hasWechatConfigChanges || wechatSaving) return;
    // 数值参数保存前拦截：负数 / 0 / 极大值 / 非整数 / max<base 均阻止保存并红字提示。
    const numericError = validateWechatNumericDraft(wechatDraft);
    if (numericError) {
      setWechatSaveError(
        numericError.kind === 'order'
          ? t('channels.errors.wechatBackoffOrder')
          : t('channels.errors.wechatNumericInvalid', {
              field: numericError.field,
              min: numericError.min,
              max: numericError.max,
            }),
      );
      return;
    }
    setWechatSaving(true);
    setWechatSaveError(null);
    const payload = buildWechatPayload(wechatDraft);
    const shouldOpenWechatQr = Boolean(payload.enabled) && !String(payload.bot_token ?? '').trim();
    try {
      const result = await webRequest<{ config?: unknown }>('channel.wechat.set_conf', payload);
      const normalized = normalizeWechatConfig(result?.config);
      setWechatConfig(normalized);
      setWechatDraft(draftFromWechatConfig(normalized));
      setWechatSuccess(t('channels.saved.wechat'));
      void fetchChannels();
      if (shouldOpenWechatQr) {
        wechatLoginPollAppliedAt.current = null;
        setWechatQrModalOpen(true);
      }
    } catch (saveErr) {
      const message = saveErr instanceof Error ? saveErr.message : t('channels.errors.saveGeneric');
      setWechatSaveError(message);
    } finally {
      setWechatSaving(false);
    }
  };

  const runWechatUnbind = async () => {
    if (!isConnected || wechatUnbinding) return;
    setWechatUnbinding(true);
    setWechatSaveError(null);
    setWechatSuccess(null);
    try {
      const result = await webRequest<{ config?: unknown }>('channel.wechat.unbind', {});
      const normalized = normalizeWechatConfig(result?.config);
      setWechatConfig(normalized);
      setWechatDraft(draftFromWechatConfig(normalized));
      setWechatSuccess(t('channels.wechatUnbind.success'));
      wechatLoginPollAppliedAt.current = null;
      setWechatUnbindConfirmOpen(false);
      if (normalized.enabled && !normalized.bot_token.trim()) {
        setWechatQrModalOpen(true);
      }
    } catch (err) {
      setWechatSaveError(err instanceof Error ? err.message : t('channels.errors.unbindWechat'));
      setWechatUnbindConfirmOpen(false);
    } finally {
      setWechatUnbinding(false);
    }
  };

  const isConfigRefreshing =
    feishuLoading ||
    xiaoyiLoading ||
    dingtalkLoading ||
    telegramLoading ||
    discordLoading ||
    slackLoading ||
    whatsappLoading ||
    wecomLoading ||
    wechatLoading;

  const renderToggle = (checked: boolean, onClick: () => void) => (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={onClick}
      className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
        checked ? 'bg-ok' : 'bg-secondary'
      }`}
    >
      <span
        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
          checked ? 'translate-x-4' : 'translate-x-0'
        }`}
      />
    </button>
  );

  const renderFeishuAppField = (app: FeishuAppDraft, appIndex: number, field: keyof FeishuAppDraft) => {
    const visibilityKey = `${appIndex}.${String(field)}`;
    const value = app[field];
    if (typeof value === 'boolean') {
      return (
        <tr key={String(field)} className="border-t border-border first:border-t-0 even:bg-secondary/10">
          <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{String(field)}</td>
          <td className="px-4 py-2.5 align-middle">
            {renderToggle(value, () => handleFeishuAppFieldChange(appIndex, field, !value as FeishuAppDraft[typeof field]))}
          </td>
        </tr>
      );
    }
    return (
      <tr key={String(field)} className="border-t border-border first:border-t-0 even:bg-secondary/10">
        <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{String(field)}</td>
        <td className="px-4 py-2.5 break-all text-[13px] align-middle">
          <div className="relative">
            <input
              type={isSensitiveField(field) && !visibleFields[visibilityKey] ? 'password' : 'text'}
              value={String(value ?? '')}
              onChange={(e) => handleFeishuAppFieldChange(appIndex, field, e.target.value as FeishuAppDraft[typeof field])}
              placeholder={t('channels.placeholders.configValue')}
              className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${
                isSensitiveField(field) ? 'pr-10' : ''
              }`}
            />
            {isSensitiveField(field) ? (
              <button
                type="button"
                onClick={() => toggleFieldVisible(visibilityKey)}
                className="channels-panel__visibility-toggle"
                aria-label={visibleFields[visibilityKey] ? t('channels.hideValue') : t('channels.showValue')}
                title={visibleFields[visibilityKey] ? t('channels.hideValue') : t('channels.showValue')}
              >
                <VisibilityIcon visible={Boolean(visibleFields[visibilityKey])} />
              </button>
            ) : null}
          </div>
        </td>
      </tr>
    );
  };

  const renderXiaoyiField = (app: XiaoyiAppDraft, field: keyof XiaoyiAppDraft) => {
    const visibilityKey = String(field);
    const value = app[field];
    if (typeof value === 'boolean') {
      return (
        <tr key={String(field)} className="border-t border-border first:border-t-0 even:bg-secondary/10">
          <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{String(field)}</td>
          <td className="px-4 py-2.5 align-middle">
            {renderToggle(value, () => handleXiaoyiFieldChange(field, !value as XiaoyiAppDraft[typeof field]))}
          </td>
        </tr>
      );
    }
    return (
      <tr key={String(field)} className="border-t border-border first:border-t-0 even:bg-secondary/10">
        <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{String(field)}</td>
        <td className="px-4 py-2.5 break-all text-[13px] align-middle">
          <div className="relative">
            <input
              type={isSensitiveXiaoyiField(field) && !xiaoyiVisibleFields[visibilityKey] ? 'password' : 'text'}
              value={String(value ?? '')}
              onChange={(e) => handleXiaoyiFieldChange(field, e.target.value as XiaoyiAppDraft[typeof field])}
              placeholder={t('channels.placeholders.configValue')}
              className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${
                isSensitiveXiaoyiField(field) ? 'pr-10' : ''
              }`}
            />
            {isSensitiveXiaoyiField(field) ? (
              <button
                type="button"
                onClick={() => toggleXiaoyiFieldVisible(visibilityKey)}
                className="channels-panel__visibility-toggle"
                aria-label={xiaoyiVisibleFields[visibilityKey] ? t('channels.hideValue') : t('channels.showValue')}
                title={xiaoyiVisibleFields[visibilityKey] ? t('channels.hideValue') : t('channels.showValue')}
              >
                <VisibilityIcon visible={Boolean(xiaoyiVisibleFields[visibilityKey])} />
              </button>
            ) : null}
          </div>
        </td>
      </tr>
    );
  };

  // 旧版单飞书应用配置迁移到 apps 数组时，后端会给缺失的 name 填入固定中文种子文案
  // （见 app_web_handlers.py 的 _FEISHU_APP_DEFAULTS/_normalize_single_feishu_to_app），
  // 与当前 UI 语言无关。这里仅做展示层替换：未被用户改过时按当前语言显示对应译文，
  // 不改动 app.name 的实际值，因此不会把翻译结果回写进 config.yaml。
  // legacyDefaultAppName 只用于识别后端旧迁移逻辑写入的原始种子文案（"默认应用"），
  // 命中后统一按 defaultAppName（"飞书默认应用"）展示，避免出现两种"默认应用"文案。
  const FEISHU_APP_NAME_SEED_KEYS: { detectKey: string; displayKey: string }[] = [
    { detectKey: 'channels.feishuApps.defaultAppName', displayKey: 'channels.feishuApps.defaultAppName' },
    { detectKey: 'channels.feishuApps.legacyDefaultAppName', displayKey: 'channels.feishuApps.defaultAppName' },
    { detectKey: 'channels.feishuApps.unnamedAppName', displayKey: 'channels.feishuApps.unnamedAppName' },
  ];

  const getFeishuAppNameDisplayValue = (rawName: string): string => {
    const matched = FEISHU_APP_NAME_SEED_KEYS.find((entry) =>
      ['zh', 'en'].some((lng) => rawName === t(entry.detectKey, { lng })),
    );
    return matched ? t(matched.displayKey) : rawName;
  };

  const renderFeishuAppsEditor = () => (
    <div className="space-y-3">
      {feishuDraftApps.map((app, index) => {
        const expanded = expandedFeishuAppIndex === index;
        const identifier = app.app_id.trim() || t('channels.feishuApps.appIdNotConfigured');
        return (
          <div key={`feishu-app-${index}`} className="rounded-xl border border-border bg-card overflow-hidden">
            <div className="flex items-center gap-3 px-4 py-3">
              <button
                type="button"
                onClick={() => setExpandedFeishuAppIndex(expanded ? -1 : index)}
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-text-muted hover:bg-secondary hover:text-text"
                aria-label={expanded ? t('channels.feishuApps.collapseConfig') : t('channels.feishuApps.expandConfig')}
                title={expanded ? t('channels.feishuApps.collapseConfig') : t('channels.feishuApps.expandConfig')}
              >
                <ChevronRight className={`h-4 w-4  ${expanded ? 'rotate-90' : ''}`} />
              </button>
              <input
                type="text"
                value={getFeishuAppNameDisplayValue(app.name)}
                onChange={(e) => handleFeishuAppFieldChange(index, 'name', e.target.value)}
                className="min-w-[160px] flex-1 rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
                placeholder={t('channels.feishuApps.appNamePlaceholder')}
              />
              {app.is_default ? (
                <span className="rounded-full border border-accent bg-accent-subtle px-2.5 py-1 text-xs text-accent">
                  {t('channels.feishuApps.defaultBadge')}
                </span>
              ) : null}
              {!app.is_default ? (
                <button
                  type="button"
                  onClick={() => handleSetDefaultFeishuApp(index)}
                  className="rounded-full border border-accent/50 bg-accent-subtle px-2.5 py-1 text-xs font-medium text-accent hover:border-accent hover:bg-accent/15"
                  aria-label={t('channels.feishuApps.setDefaultAria')}
                  title={t('channels.feishuApps.setDefaultAria')}
                >
                  {t('channels.feishuApps.setDefault')}
                </button>
              ) : null}
              <span className="mono max-w-[220px] truncate rounded-md border border-border bg-secondary px-2.5 py-1 text-xs text-text-muted">
                {identifier}
              </span>
              <span
                className={`rounded-full border px-2.5 py-1 text-xs font-medium ${
                  app.enabled ? 'border-ok bg-ok-subtle text-ok' : 'border-border bg-secondary text-text-muted'
                }`}
              >
                {app.enabled ? t('channels.status.enabled') : t('channels.status.disabled')}
              </span>
              <button
                type="button"
                onClick={() => handleDeleteFeishuApp(index)}
                disabled={feishuDraftApps.length <= 1}
                className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border text-text-muted hover:bg-danger-subtle hover:text-danger disabled:cursor-not-allowed disabled:opacity-40"
                aria-label={t('channels.feishuApps.deleteApp')}
                title={t('channels.feishuApps.deleteApp')}
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
            {expanded ? (
              <div className="border-t border-border bg-bg/30">
                <table className="w-full text-sm">
                  <tbody>
                    {(['enabled', 'enable_streaming', 'app_id', 'app_secret', 'encrypt_key', 'verification_token', 'group_digital_avatar'] as const).map(
                      (field) => renderFeishuAppField(app, index, field),
                    )}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        );
      })}
      <button
        type="button"
        onClick={handleAddFeishuApp}
        className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-border px-4 py-3 text-sm text-text-muted hover:border-accent hover:bg-accent-subtle hover:text-accent"
      >
        <Plus className="h-4 w-4" />
        {t('channels.feishuApps.addApp')}
      </button>
    </div>
  );

  const renderXiaoyiConfigEditor = () => {
    // 仅在启用且未填 api_id 时显示警告横幅；填入后直接消失，不切换成灰色说明条
    const showApiIdHint =
      xiaoyiDraft.enabled && !xiaoyiDraft.api_id.trim() && !xiaoyiApiIdHintDismissed;
    return (
      <>
        {showApiIdHint ? (
          <div className="mb-3 flex items-start gap-2 rounded-md border border-warn/30 bg-warn-subtle px-3 py-2 text-xs text-warn">
            <p className="min-w-0 flex-1">{t('channels.placeholders.xiaoyiApiIdRequiredForCron')}</p>
            <button
              type="button"
              onClick={() => setXiaoyiApiIdHintDismissed(true)}
              className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-current/70 hover:bg-secondary hover:text-current"
              aria-label={t('common.close')}
              title={t('common.close')}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : null}
        <table className="w-full text-sm">
          <tbody>
            {(['enabled', 'enable_streaming', 'ak', 'sk', 'agent_id', 'api_id'] as const).map((field) =>
              renderXiaoyiField(xiaoyiDraft, field),
            )}
          </tbody>
        </table>
      </>
    );
  };
  const configErrorNotice = useMemo(() => {
    return Array.from(
      new Set(
        [
          saveError,
          xiaoyiSaveError,
          dingtalkSaveError,
          telegramSaveError,
          discordSaveError,
          slackSaveError,
          whatsappSaveError,
          wecomSaveError,
          wechatSaveError,
        ].filter((message): message is string => Boolean(message)),
      ),
    ).join(t('common.and'));
  }, [
    discordSaveError,
    dingtalkSaveError,
    saveError,
    slackSaveError,
    t,
    telegramSaveError,
    whatsappSaveError,
    wechatSaveError,
    wecomSaveError,
    xiaoyiSaveError,
  ]);
  useEffect(() => {
    if (!configErrorNotice) {
      return;
    }
    const timer = window.setTimeout(() => {
      setSaveError(null);
      setXiaoyiSaveError(null);
      setDingtalkSaveError(null);
      setTelegramSaveError(null);
      setDiscordSaveError(null);
      setSlackSaveError(null);
      setWhatsappSaveError(null);
      setWecomSaveError(null);
      setWechatSaveError(null);
    }, 2000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [configErrorNotice]);

  return (
    <div className="flex-1 min-h-0 relative">
      <WechatQrModal
        open={wechatQrModalOpen}
        onClose={() => setWechatQrModalOpen(false)}
        loginUi={wechatLoginUi}
      />
      <WechatUnbindConfirmModal
        open={wechatUnbindConfirmOpen}
        onClose={() => {
          if (!wechatUnbinding) {
            setWechatUnbindConfirmOpen(false);
          }
        }}
        onConfirm={() => void runWechatUnbind()}
        confirming={wechatUnbinding}
      />
      <div className="card w-full h-full flex flex-col">
        {configErrorNotice ? (
          <div className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 z-20">
            <div className="bg-danger text-text-inverse px-4 py-2 rounded-lg shadow-lg animate-rise text-sm">
              {configErrorNotice}
            </div>
          </div>
        ) : null}
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">{t('channels.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t('channels.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2" />
        </div>

        {error ? (
          <div className="border border-[var(--color-border-danger)] bg-danger-subtle rounded-lg p-4 text-sm text-danger flex items-center justify-between">
            <span>{t('channels.fetchFailed')}: {error}</span>
            <button onClick={() => void fetchChannels()} className="btn !px-3 !py-1.5">
              {t('channels.retry')}
            </button>
          </div>
        ) : (
          <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,3fr)_minmax(0,7fr)] gap-4">
            <section className="min-w-[260px] rounded-xl border border-border bg-card/70 backdrop-blur-sm shadow-sm flex flex-col min-h-0 overflow-hidden">
              <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <h3 className="text-sm font-medium text-text">{t('channels.listTitle')}</h3>
                    <p className="text-xs text-text-muted mt-1 mono">
                      {t('channels.listMeta', { status: statusText, time: formatTime(lastUpdatedAt, i18n.language) })}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => void fetchChannels()}
                    className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                    disabled={loadState === 'loading'}
                  >
                    {loadState === 'loading' ? t('common.refreshing') : t('common.refresh')}
                  </button>
                </div>
              </div>
              <div className="overflow-auto flex-1 min-h-0 p-3">
                {loadState === 'loading' ? (
                  <div className="space-y-2">
                    <div className="h-10 rounded-lg border border-border bg-secondary/40" />
                    <div className="h-10 rounded-lg border border-border bg-secondary/30" />
                  </div>
                ) : (
                  <div className="space-y-2">
                    {channels.map((channel, index) => {
                      const isAdapting = ADAPTING_CHANNEL_IDS.has(channel.channel_id);
                      const label = getChannelLabel(t, channel.channel_id);
                      return (
                        <button
                          type="button"
                          key={channel.channel_id}
                          onClick={() => handleSelectChannel(channel.channel_id)}
                          disabled={isAdapting}
                          className={`w-full rounded-xl border px-4 py-3.5 text-left  ${
                            isAdapting
                              ? 'channels-panel__channel-disabled border-border bg-card text-text-muted'
                              : activeChannelId === channel.channel_id
                                ? 'border-accent bg-accent-subtle text-text'
                                : 'border-border bg-card text-text hover:bg-bg-hover'
                          }`}
                        >
                          <div className="flex items-center justify-between gap-3 w-full">
                            <div className="flex items-center gap-3 min-w-0 flex-1">
                              <span className="text-xs px-2.5 py-1 rounded-full border border-border bg-secondary text-text-muted font-medium flex-shrink-0">
                                #{index + 1}
                              </span>
                              <ChannelLogo channel={channel} label={label} />
                              <span className="text-sm font-medium text-text flex-1 min-w-0 truncate">{label}</span>
                              <span className="mono text-xs px-2.5 py-1 rounded-md border border-border bg-secondary text-text-muted flex-shrink-0">
                                {channel.channel_id}
                              </span>
                            </div>
                            <span
                              className={`text-xs px-2.5 py-1 rounded-full border font-medium flex-shrink-0 ${
                                isAdapting
                                  ? 'text-text-muted border-border bg-secondary'
                                  : channel.enabled
                                    ? 'text-ok border-ok bg-ok-subtle'
                                    : 'text-text-muted border-border bg-secondary'
                              }`}
                            >
                              {isAdapting ? t('channels.status.adapting') : channel.enabled ? t('channels.status.enabled') : t('channels.status.disabled')}
                            </span>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            </section>

            <section className="min-h-0 flex">
                {activeChannelId === 'web' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center gap-3">
                        <ChannelHeaderLogo channelId="web" label={getChannelLabel(t, 'web')} />
                        <div>
                          <h4 className="text-sm font-medium text-text">{t('channels.config.webTitle')}</h4>
                          <p className="text-xs text-text-muted mt-1">{t('channels.config.webSubtitle')}</p>
                        </div>
                      </div>
                    </div>
                    <div className="p-4 text-sm text-text-muted flex-1 overflow-auto flex items-center justify-center text-center">
                      {t('channels.config.webEmpty')}
                    </div>
                  </div>
                ) : null}

                {activeChannelId === 'xiaoyi' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId="xiaoyi" label={getChannelLabel(t, 'xiaoyi')} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.xiaoyiTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.xiaoyiSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchXiaoyiConfig()}
                            disabled={xiaoyiSaving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {xiaoyiLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelXiaoyiConfig}
                            disabled={!hasXiaoyiConfigChanges || xiaoyiSaving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveXiaoyiConfig()}
                            disabled={!hasXiaoyiConfigChanges || xiaoyiSaving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {xiaoyiSaving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {xiaoyiSuccess ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {xiaoyiSuccess}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {xiaoyiLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.xiaoyi')}</div>
                      ) : (
                        renderXiaoyiConfigEditor()
                      )}
                    </div>
                  </div>
                ) : null}

                {activeChannelId === 'dingtalk' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId="dingtalk" label={getChannelLabel(t, 'dingtalk')} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.dingtalkTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.dingtalkSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchDingtalkConfig()}
                            disabled={dingtalkSaving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {dingtalkLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelDingtalkConfig}
                            disabled={!hasDingtalkConfigChanges || dingtalkSaving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveDingtalkConfig()}
                            disabled={!hasDingtalkConfigChanges || dingtalkSaving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {dingtalkSaving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {dingtalkSuccess ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {dingtalkSuccess}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {dingtalkLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.dingtalk')}</div>
                      ) : (
                        <table className="w-full text-sm">
                          <tbody>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enabled</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={dingtalkDraft.enabled}
                                  onClick={() => handleDingtalkFieldChange('enabled', !dingtalkDraft.enabled)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    dingtalkDraft.enabled ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      dingtalkDraft.enabled ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            {(['client_id', 'client_secret'] as const).map((field) => (
                              <tr key={field} className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{field}</td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <div className="relative">
                                    <input
                                      type={isSensitiveDingtalkField(field) && !dingtalkVisibleFields[field] ? 'password' : 'text'}
                                      value={dingtalkDraft[field]}
                                      onChange={(e) => handleDingtalkFieldChange(field, e.target.value)}
                                      placeholder={field === 'client_id' ? t('channels.placeholders.appId') : t('channels.placeholders.appSecret')}
                                      className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${
                                        isSensitiveDingtalkField(field) ? 'pr-10' : ''
                                      }`}
                                    />
                                    {isSensitiveDingtalkField(field) ? (
                                      <button
                                        type="button"
                                        onClick={() => toggleDingtalkFieldVisible(field)}
                                        className="channels-panel__visibility-toggle"
                                        aria-label={dingtalkVisibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                        title={dingtalkVisibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                      >
                                        <VisibilityIcon visible={Boolean(dingtalkVisibleFields[field])} />
                                      </button>
                                    ) : null}
                                  </div>
                                </td>
                              </tr>
                            ))}
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">allow_from</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <textarea
                                  value={dingtalkDraft.allow_from}
                                  onChange={(e) => handleDingtalkFieldChange('allow_from', e.target.value)}
                                  placeholder={t('channels.placeholders.employeeIds')}
                                  rows={4}
                                  className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent resize-y"
                                />
                              </td>
                            </tr>
                          </tbody>
                        </table>
                      )}
                    </div>
                  </div>
                ) : null}

                {activeChannelId === 'feishu' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId="feishu" label={getChannelLabel(t, 'feishu')} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.feishuTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.feishuSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchFeishuConfig()}
                            disabled={saving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {feishuLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelConfig}
                            disabled={!hasConfigChanges || saving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveConfig()}
                            disabled={!hasConfigChanges || saving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {saving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {success ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {success}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {feishuLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.feishu')}</div>
                      ) : (
                        renderFeishuAppsEditor()
                      )}
                    </div>
                  </div>
                ) : null}

                {false && activeChannelId === 'feishu' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId="feishu" label={getChannelLabel(t, 'feishu')} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.feishuTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.feishuSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchFeishuConfig()}
                            disabled={saving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {feishuLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelConfig}
                            disabled={!hasConfigChanges || saving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveConfig()}
                            disabled={!hasConfigChanges || saving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {saving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {success ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {success}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {feishuLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.feishu')}</div>
                      ) : (
                        <table className="w-full text-sm">
                          <tbody>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enabled</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={draft.enabled}
                                  onClick={() => handleFieldChange('enabled', !draft.enabled)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    draft.enabled ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      draft.enabled ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enable_streaming</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={draft.enable_streaming}
                                  onClick={() => handleFieldChange('enable_streaming', !draft.enable_streaming)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    draft.enable_streaming ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      draft.enable_streaming ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            {(['app_id', 'app_secret', 'encrypt_key', 'verification_token'] as const).map((field) => (
                              <tr key={field} className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{field}</td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <div className="relative">
                                    <input
                                      type={isSensitiveField(field) && !visibleFields[field] ? 'password' : 'text'}
                                      value={draft[field]}
                                      onChange={(e) => handleFieldChange(field, e.target.value)}
                                      placeholder={t('channels.placeholders.configValue')}
                                      className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${
                                        isSensitiveField(field) ? 'pr-10' : ''
                                      }`}
                                    />
                                    {isSensitiveField(field) ? (
                                      <button
                                        type="button"
                                        onClick={() => toggleFieldVisible(field)}
                                        className="channels-panel__visibility-toggle"
                                        aria-label={visibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                        title={visibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                      >
                                        <VisibilityIcon visible={Boolean(visibleFields[field])} />
                                      </button>
                                    ) : null}
                                  </div>
                                </td>
                              </tr>
                            ))}
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">group_digital_avatar</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={draft.group_digital_avatar}
                                  onClick={() => handleFieldChange('group_digital_avatar', !draft.group_digital_avatar)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    draft.group_digital_avatar ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      draft.group_digital_avatar ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            {draft.group_digital_avatar && (
                              <>
                                <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                  <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">my_user_id</td>
                                  <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                    <input
                                      type="text"
                                      value={draft.my_user_id}
                                      onChange={(e) => handleFieldChange('my_user_id', e.target.value)}
                                      placeholder={t('channels.placeholders.configValue')}
                                      className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                    />
                                  </td>
                                </tr>
                                <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                  <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">bot_name</td>
                                  <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                    <input
                                      type="text"
                                      value={draft.bot_name}
                                      onChange={(e) => handleFieldChange('bot_name', e.target.value)}
                                      placeholder={t('channels.placeholders.configValue')}
                                      className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                    />
                                  </td>
                                </tr>
                                <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                  <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enable_memory</td>
                                  <td className="px-4 py-2.5 align-middle">
                                    <button
                                      type="button"
                                      role="switch"
                                      aria-checked={draft.enable_memory}
                                      onClick={() => handleFieldChange('enable_memory', !draft.enable_memory)}
                                      className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                        draft.enable_memory ? 'bg-ok' : 'bg-secondary'
                                      }`}
                                    >
                                      <span
                                        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                          draft.enable_memory ? 'translate-x-4' : 'translate-x-0'
                                        }`}
                                      />
                                    </button>
                                  </td>
                                </tr>
                              </>
                            )}
                          </tbody>
                        </table>
                      )}

                      {/* 数字分身权限编辑器 — 放在 table 外部 */}
                      {draft.group_digital_avatar && (
                        <div className="mt-4 px-4 py-3 border-t border-border">
                          <h5 className="text-xs font-medium text-text-muted mb-2">{t("ownerScopes.toolPermissions")}</h5>
                          <AvatarPermEditor channelId="feishu" userId={draft.my_user_id} />
                        </div>
                      )}
                    </div>
                  </div>
                ) : null}

                {activeChannelId === 'telegram' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId="telegram" label={getChannelLabel(t, 'telegram')} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.telegramTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.telegramSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchTelegramConfig()}
                            disabled={telegramSaving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {telegramLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelTelegramConfig}
                            disabled={!hasTelegramConfigChanges || telegramSaving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveTelegramConfig()}
                            disabled={!hasTelegramConfigChanges || telegramSaving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {telegramSaving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {telegramSuccess ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {telegramSuccess}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {telegramLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.telegram')}</div>
                      ) : (
                        <table className="w-full text-sm">
                          <tbody>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enabled</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={telegramDraft.enabled}
                                  onClick={() => handleTelegramFieldChange('enabled', !telegramDraft.enabled)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    telegramDraft.enabled ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      telegramDraft.enabled ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">bot_token</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <div className="relative">
                                  <input
                                    type={telegramVisibleFields['bot_token'] ? 'text' : 'password'}
                                    value={telegramDraft.bot_token}
                                    onChange={(e) => handleTelegramFieldChange('bot_token', e.target.value)}
                                    placeholder={t('channels.placeholders.telegramBotToken')}
                                    className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent pr-10"
                                  />
                                  <button
                                    type="button"
                                    onClick={() => toggleTelegramFieldVisible('bot_token')}
                                    className="channels-panel__visibility-toggle"
                                    aria-label={telegramVisibleFields['bot_token'] ? t('channels.hideValue') : t('channels.showValue')}
                                    title={telegramVisibleFields['bot_token'] ? t('channels.hideValue') : t('channels.showValue')}
                                  >
                                    <VisibilityIcon visible={Boolean(telegramVisibleFields['bot_token'])} />
                                  </button>
                                </div>
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">allow_from</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <textarea
                                  value={telegramDraft.allow_from}
                                  onChange={(e) => handleTelegramFieldChange('allow_from', e.target.value)}
                                  placeholder={t('channels.placeholders.telegramUserIds')}
                                  rows={4}
                                  className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent resize-y"
                                />
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">parse_mode</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <select
                                  value={telegramDraft.parse_mode}
                                  onChange={(e) => handleTelegramFieldChange('parse_mode', e.target.value)}
                                  className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                >
                                  <option value="Markdown">Markdown</option>
                                  <option value="HTML">HTML</option>
                                  <option value="None">None</option>
                                </select>
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">group_chat_mode</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <select
                                  value={telegramDraft.group_chat_mode}
                                  onChange={(e) => handleTelegramFieldChange('group_chat_mode', e.target.value)}
                                  className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                >
                                  <option value="mention">Only respond to @mentions (mention)</option>
                                  <option value="reply">Only respond to replies (reply)</option>
                                  <option value="all">Respond to all messages (all)</option>
                                  <option value="off">Disable group chat (off)</option>
                                </select>
                              </td>
                            </tr>
                          </tbody>
                        </table>
                      )}
                    </div>
                  </div>
                ) : null}

                {activeChannelId === 'discord' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId="discord" label={getChannelLabel(t, 'discord')} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.discordTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.discordSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchDiscordConfig()}
                            disabled={discordSaving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {discordLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelDiscordConfig}
                            disabled={!hasDiscordConfigChanges || discordSaving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveDiscordConfig()}
                            disabled={!hasDiscordConfigChanges || discordSaving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {discordSaving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {discordSuccess ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {discordSuccess}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {discordLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.discord')}</div>
                      ) : (
                        <>
                          <div className="mb-3 rounded-md border border-border bg-secondary/20 px-3 py-2 text-xs text-text-muted">
                            {t('channels.config.discordHint')}
                          </div>
                          <table className="w-full text-sm">
                            <tbody>
                              <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enabled</td>
                                <td className="px-4 py-2.5 align-middle">
                                  <button
                                    type="button"
                                    role="switch"
                                    aria-checked={discordDraft.enabled}
                                    onClick={() => handleDiscordFieldChange('enabled', !discordDraft.enabled)}
                                    className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                      discordDraft.enabled ? 'bg-ok' : 'bg-secondary'
                                    }`}
                                  >
                                    <span
                                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                        discordDraft.enabled ? 'translate-x-4' : 'translate-x-0'
                                      }`}
                                    />
                                  </button>
                                </td>
                              </tr>
                              <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">block_dm</td>
                                <td className="px-4 py-2.5 align-middle">
                                  <button
                                    type="button"
                                    role="switch"
                                    aria-checked={discordDraft.block_dm}
                                    onClick={() => handleDiscordFieldChange('block_dm', !discordDraft.block_dm)}
                                    className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                      discordDraft.block_dm ? 'bg-ok' : 'bg-secondary'
                                    }`}
                                  >
                                    <span
                                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                        discordDraft.block_dm ? 'translate-x-4' : 'translate-x-0'
                                      }`}
                                    />
                                  </button>
                                </td>
                              </tr>
                              {(['bot_token', 'application_id', 'guild_id', 'channel_id'] as const).map((field) => (
                                <tr key={field} className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                  <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{field}</td>
                                  <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                    <div className="relative">
                                      <input
                                        type={isSensitiveDiscordField(field) && !discordVisibleFields[field] ? 'password' : 'text'}
                                        value={discordDraft[field]}
                                        onChange={(e) => handleDiscordFieldChange(field, e.target.value)}
                                        placeholder={
                                          field === 'guild_id'
                                            ? t('channels.placeholders.discordGuildId')
                                            : field === 'channel_id'
                                              ? t('channels.placeholders.discordChannelId')
                                              : t('channels.placeholders.configValue')
                                        }
                                        className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${
                                          isSensitiveDiscordField(field) ? 'pr-10' : ''
                                        }`}
                                      />
                                      {isSensitiveDiscordField(field) ? (
                                        <button
                                          type="button"
                                          onClick={() => toggleDiscordFieldVisible(field)}
                                          className="channels-panel__visibility-toggle"
                                          aria-label={discordVisibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                          title={discordVisibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                        >
                                          <VisibilityIcon visible={Boolean(discordVisibleFields[field])} />
                                        </button>
                                      ) : null}
                                    </div>
                                  </td>
                                </tr>
                              ))}
                              <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">allow_from</td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <textarea
                                    value={discordDraft.allow_from}
                                    onChange={(e) => handleDiscordFieldChange('allow_from', e.target.value)}
                                    placeholder={t('channels.placeholders.ids')}
                                    rows={4}
                                    className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent resize-y"
                                  />
                                </td>
                              </tr>
                            </tbody>
                          </table>
                        </>
                      )}
                    </div>
                  </div>
                ) : null}

                {activeChannelId === 'slack' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId="slack" label={getChannelLabel(t, 'slack')} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.slackTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.slackSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchSlackConfig()}
                            disabled={slackSaving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {slackLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelSlackConfig}
                            disabled={!hasSlackConfigChanges || slackSaving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveSlackConfig()}
                            disabled={!hasSlackConfigChanges || slackSaving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {slackSaving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {slackSuccess ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {slackSuccess}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {slackLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.slack')}</div>
                      ) : (
                        <>
                          <div className="mb-3 rounded-md border border-border bg-secondary/20 px-3 py-2 text-xs text-text-muted">
                            {t('channels.config.slackHint')}
                          </div>
                          <table className="w-full text-sm">
                            <tbody>
                              <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enabled</td>
                                <td className="px-4 py-2.5 align-middle">
                                  <button
                                    type="button"
                                    role="switch"
                                    aria-checked={slackDraft.enabled}
                                    onClick={() => handleSlackFieldChange('enabled', !slackDraft.enabled)}
                                    className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus:outline-none ${
                                      slackDraft.enabled ? 'bg-ok' : 'bg-secondary'
                                    }`}
                                  >
                                    <span
                                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${
                                        slackDraft.enabled ? 'translate-x-4' : 'translate-x-0'
                                      }`}
                                    />
                                  </button>
                                </td>
                              </tr>
                              <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">
                                  reply_in_thread
                                </td>
                                <td className="px-4 py-2.5 align-middle">
                                  <button
                                    type="button"
                                    role="switch"
                                    aria-checked={slackDraft.reply_in_thread}
                                    onClick={() =>
                                      handleSlackFieldChange('reply_in_thread', !slackDraft.reply_in_thread)
                                    }
                                    className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus:outline-none ${
                                      slackDraft.reply_in_thread ? 'bg-ok' : 'bg-secondary'
                                    }`}
                                  >
                                    <span
                                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${
                                        slackDraft.reply_in_thread ? 'translate-x-4' : 'translate-x-0'
                                      }`}
                                    />
                                  </button>
                                </td>
                              </tr>
                              {(['bot_token', 'app_token', 'default_channel_id'] as const).map((field) => (
                                <tr key={field} className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                  <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{field}</td>
                                  <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                    <div className="relative">
                                      <input
                                        type={isSensitiveSlackField(field) && !slackVisibleFields[field] ? 'password' : 'text'}
                                        value={slackDraft[field]}
                                        onChange={(e) => handleSlackFieldChange(field, e.target.value)}
                                        placeholder={
                                          field === 'bot_token'
                                            ? t('channels.placeholders.slackBotToken')
                                            : field === 'app_token'
                                              ? t('channels.placeholders.slackAppToken')
                                              : t('channels.placeholders.slackChannelId')
                                        }
                                        className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${
                                          isSensitiveSlackField(field) ? 'pr-10' : ''
                                        }`}
                                      />
                                      {isSensitiveSlackField(field) ? (
                                        <button
                                          type="button"
                                          onClick={() => toggleSlackFieldVisible(field)}
                                          className="channels-panel__visibility-toggle"
                                          aria-label={slackVisibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                          title={slackVisibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                        >
                                          <VisibilityIcon visible={Boolean(slackVisibleFields[field])} />
                                        </button>
                                      ) : null}
                                    </div>
                                  </td>
                                </tr>
                              ))}
                              <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">allow_from</td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <textarea
                                    value={slackDraft.allow_from}
                                    onChange={(e) => handleSlackFieldChange('allow_from', e.target.value)}
                                    placeholder={t('channels.placeholders.slackUserIds')}
                                    rows={4}
                                    className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent resize-y"
                                  />
                                </td>
                              </tr>
                              <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">
                                  allowed_channel_ids
                                </td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <textarea
                                    value={slackDraft.allowed_channel_ids}
                                    onChange={(e) => handleSlackFieldChange('allowed_channel_ids', e.target.value)}
                                    placeholder={t('channels.placeholders.slackChannelIds')}
                                    rows={4}
                                    className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent resize-y"
                                  />
                                </td>
                              </tr>
                            </tbody>
                          </table>
                        </>
                      )}
                    </div>
                  </div>
                ) : null}

                {activeChannelId === 'whatsapp' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId="whatsapp" label={getChannelLabel(t, 'whatsapp')} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.whatsappTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.whatsappSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchWhatsAppConfig()}
                            disabled={whatsappSaving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {whatsappLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelWhatsAppConfig}
                            disabled={!hasWhatsAppConfigChanges || whatsappSaving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveWhatsAppConfig()}
                            disabled={!hasWhatsAppConfigChanges || whatsappSaving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {whatsappSaving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {whatsappSuccess ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {whatsappSuccess}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {whatsappLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.whatsapp')}</div>
                      ) : (
                        <table className="w-full text-sm">
                          <tbody>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enabled</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={whatsappDraft.enabled}
                                  onClick={() => handleWhatsAppFieldChange('enabled', !whatsappDraft.enabled)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    whatsappDraft.enabled ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      whatsappDraft.enabled ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            {(['bridge_ws_url', 'default_jid', 'bridge_command', 'bridge_workdir'] as const).map((field) => (
                              <tr key={field} className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{field}</td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <input
                                    type="text"
                                    value={whatsappDraft[field]}
                                    onChange={(e) => handleWhatsAppFieldChange(field, e.target.value)}
                                    placeholder={t('channels.placeholders.configValue')}
                                    className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                  />
                                </td>
                              </tr>
                            ))}
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">allow_from</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <textarea
                                  value={whatsappDraft.allow_from}
                                  onChange={(e) => handleWhatsAppFieldChange('allow_from', e.target.value)}
                                  placeholder={t('channels.placeholders.whatsappJids')}
                                  rows={4}
                                  className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent resize-y"
                                />
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enable_streaming</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={whatsappDraft.enable_streaming}
                                  onClick={() => handleWhatsAppFieldChange('enable_streaming', !whatsappDraft.enable_streaming)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    whatsappDraft.enable_streaming ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      whatsappDraft.enable_streaming ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">auto_start_bridge</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={whatsappDraft.auto_start_bridge}
                                  onClick={() => handleWhatsAppFieldChange('auto_start_bridge', !whatsappDraft.auto_start_bridge)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    whatsappDraft.auto_start_bridge ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      whatsappDraft.auto_start_bridge ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                          </tbody>
                        </table>
                      )}
                    </div>
                  </div>
                ) : null}

                {(activeChannelId as string) === 'wechat' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId={'wechat' as SupportedChannelId} label={getChannelLabel(t, 'wechat' as SupportedChannelId)} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.wechatTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.wechatSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2 flex-wrap">
                          <button
                            type="button"
                            onClick={() => void fetchWechatConfig()}
                            disabled={wechatSaving || wechatUnbinding || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {wechatLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              if (!wechatConfig.enabled || wechatSaving || wechatUnbinding || wechatLoading) return;
                              setWechatUnbindConfirmOpen(true);
                            }}
                            disabled={!wechatConfig.enabled || wechatSaving || wechatUnbinding || wechatLoading}
                            className="btn !px-3 !py-1.5 border border-[var(--color-feedback-danger)] text-[var(--color-feedback-danger)] hover:bg-[var(--color-feedback-danger)]/10 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {wechatUnbinding ? t('channels.wechatUnbind.unbinding') : t('channels.wechatUnbind.button')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelWechatConfig}
                            disabled={!hasWechatConfigChanges || wechatSaving || wechatUnbinding}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveWechatConfig()}
                            disabled={!hasWechatConfigChanges || wechatSaving || wechatUnbinding || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {wechatSaving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {wechatSuccess ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {wechatSuccess}
                      </div>
                    ) : null}
                    {wechatDraft.enabled && !wechatDraft.bot_token.trim() ? (
                      <div className="mx-4 mt-4 rounded-md border border-border bg-secondary/30 px-3 py-2 text-sm text-text-muted">
                        {t('channels.notices.wechatAutoLogin')}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {wechatLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.wechat')}</div>
                      ) : (
                        <table className="w-full text-sm">
                          <tbody>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enabled</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={wechatDraft.enabled}
                                  onClick={() => handleWechatFieldChange('enabled', !wechatDraft.enabled)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    wechatDraft.enabled ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      wechatDraft.enabled ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>

                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">ilink_bot_id</td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <input
                                    type="text"
                                    value={String(wechatDraft.ilink_bot_id)}
                                    readOnly
                                    placeholder={t('channels.placeholders.configValue')}
                                    className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none cursor-not-allowed opacity-60"
                                  />
                                </td>
                              </tr>

                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">allow_from</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <textarea
                                  value={wechatDraft.allow_from}
                                  onChange={(e) => handleWechatFieldChange('allow_from', e.target.value)}
                                  placeholder={t('channels.placeholders.allowFrom')}
                                  className="w-full min-h-[86px] resize-y rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                />
                              </td>
                            </tr>

                            {(['auto_login', 'enable_streaming'] as const).map((field) => (
                              <tr key={field} className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{field}</td>
                                <td className="px-4 py-2.5 align-middle">
                                  <button
                                    type="button"
                                    role="switch"
                                    aria-checked={wechatDraft[field]}
                                    onClick={() => handleWechatFieldChange(field, !wechatDraft[field])}
                                    className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                      wechatDraft[field] ? 'bg-ok' : 'bg-secondary'
                                    }`}
                                  >
                                    <span
                                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                        wechatDraft[field] ? 'translate-x-4' : 'translate-x-0'
                                      }`}
                                    />
                                  </button>
                                </td>
                              </tr>
                            ))}

                            {(
                              [
                                'qrcode_poll_interval_sec',
                                'long_poll_timeout_sec',
                                'backoff_base_sec',
                                'backoff_max_sec',
                              ] as const
                            ).map((field) => (
                              <tr key={field} className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{field}</td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <input
                                    type="number"
                                    step={field === 'long_poll_timeout_sec' ? 1 : 0.1}
                                    min={WECHAT_NUMERIC_BOUNDS[field].min}
                                    max={WECHAT_NUMERIC_BOUNDS[field].max}
                                    value={wechatDraft[field]}
                                    onChange={(e) => handleWechatFieldChange(field, Number(e.target.value) || 0)}
                                    placeholder={t('channels.placeholders.configValue')}
                                    className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                  />
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </div>
                  </div>
                ) : null}

                {(activeChannelId as string) === 'wecom' ? (
                  <div className="w-full h-full rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col">
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border">
                      <div className="flex items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                          <ChannelHeaderLogo channelId={'wecom' as SupportedChannelId} label={getChannelLabel(t, 'wecom' as SupportedChannelId)} />
                          <div>
                            <h4 className="text-sm font-medium text-text">{t('channels.config.wecomTitle')}</h4>
                            <p className="text-xs text-text-muted mt-1">{t('channels.config.wecomSubtitle')}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void fetchWecomConfig()}
                            disabled={wecomSaving || isConfigRefreshing}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {wecomLoading ? t('common.refreshing') : t('common.refresh')}
                          </button>
                          <button
                            type="button"
                            onClick={handleCancelWecomConfig}
                            disabled={!hasWecomConfigChanges || wecomSaving}
                            className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {t('common.cancel')}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleSaveWecomConfig()}
                            disabled={!hasWecomConfigChanges || wecomSaving || !isConnected}
                            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {wecomSaving ? t('common.saving') : t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>

                    {wecomSuccess ? (
                      <div className="mx-4 mt-4 rounded-md border border-[var(--color-border-success)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                        {wecomSuccess}
                      </div>
                    ) : null}

                    <div className="p-4 pt-3 flex-1 overflow-auto">
                      {wecomLoading ? (
                        <div className="text-sm text-text-muted">{t('channels.loading.wecom')}</div>
                      ) : (
                        <table className="w-full text-sm">
                          <tbody>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enabled</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={wecomDraft.enabled}
                                  onClick={() => handleWecomFieldChange('enabled', !wecomDraft.enabled)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    wecomDraft.enabled ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      wecomDraft.enabled ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            {(['bot_id', 'secret'] as const).map((field) => (
                              <tr key={field} className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">{field}</td>
                                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                  <div className="relative">
                                    <input
                                      type={isSensitiveWecomField(field) && !wecomVisibleFields[field] ? 'password' : 'text'}
                                      value={wecomDraft[field]}
                                      onChange={(e) => handleWecomFieldChange(field, e.target.value)}
                                      placeholder={t('channels.placeholders.configValue')}
                                      className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${
                                        isSensitiveWecomField(field) ? 'pr-10' : ''
                                      }`}
                                    />
                                    {isSensitiveWecomField(field) ? (
                                      <button
                                        type="button"
                                        onClick={() => toggleWecomFieldVisible(field)}
                                        className="channels-panel__visibility-toggle"
                                        aria-label={wecomVisibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                        title={wecomVisibleFields[field] ? t('channels.hideValue') : t('channels.showValue')}
                                      >
                                        <VisibilityIcon visible={Boolean(wecomVisibleFields[field])} />
                                      </button>
                                    ) : null}
                                  </div>
                                </td>
                              </tr>
                            ))}
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">default_chat_id</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <input
                                  type="text"
                                  value={wecomDraft.default_chat_id}
                                  onChange={(e) => handleWecomFieldChange('default_chat_id', e.target.value)}
                                  placeholder={t('channels.placeholders.wecomDefaultChatId')}
                                  className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                />
                                <p className="mt-1 text-xs text-text-muted">{t('channels.placeholders.wecomDefaultChatIdHint')}</p>
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-top mono text-xs text-text-muted w-[32%]">allow_from</td>
                              <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                <textarea
                                  value={wecomDraft.allow_from}
                                  onChange={(e) => handleWecomFieldChange('allow_from', e.target.value)}
                                  placeholder={t('channels.placeholders.ids')}
                                  rows={4}
                                  className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent resize-y"
                                />
                              </td>
                            </tr>
                            <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                              <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">group_digital_avatar</td>
                              <td className="px-4 py-2.5 align-middle">
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={wecomDraft.group_digital_avatar}
                                  onClick={() => handleWecomFieldChange('group_digital_avatar', !wecomDraft.group_digital_avatar)}
                                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                    wecomDraft.group_digital_avatar ? 'bg-ok' : 'bg-secondary'
                                  }`}
                                >
                                  <span
                                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                      wecomDraft.group_digital_avatar ? 'translate-x-4' : 'translate-x-0'
                                    }`}
                                  />
                                </button>
                              </td>
                            </tr>
                            {wecomDraft.group_digital_avatar && (
                              <>
                                <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                  <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">my_user_id</td>
                                  <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                    <input
                                      type="text"
                                      value={wecomDraft.my_user_id}
                                      onChange={(e) => handleWecomFieldChange('my_user_id', e.target.value)}
                                      placeholder={t('channels.placeholders.configValue')}
                                      className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                    />
                                  </td>
                                </tr>
                                <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                  <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">bot_name</td>
                                  <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                                    <input
                                      type="text"
                                      value={wecomDraft.bot_name}
                                      onChange={(e) => handleWecomFieldChange('bot_name', e.target.value)}
                                      placeholder={t('channels.placeholders.configValue')}
                                      className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                                    />
                                  </td>
                                </tr>
                                <tr className="border-t border-border first:border-t-0 even:bg-secondary/10">
                                  <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]">enable_memory</td>
                                  <td className="px-4 py-2.5 align-middle">
                                    <button
                                      type="button"
                                      role="switch"
                                      aria-checked={wecomDraft.enable_memory}
                                      onClick={() => handleWecomFieldChange('enable_memory', !wecomDraft.enable_memory)}
                                      className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${
                                        wecomDraft.enable_memory ? 'bg-ok' : 'bg-secondary'
                                      }`}
                                    >
                                      <span
                                        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${
                                          wecomDraft.enable_memory ? 'translate-x-4' : 'translate-x-0'
                                        }`}
                                      />
                                    </button>
                                  </td>
                                </tr>
                                <tr className="border-t border-border first:border-t-0">
                                  <td colSpan={2} className="px-4 py-3">
                                    <p className="text-xs font-medium text-text-muted mb-2">{t('ownerScopes.toolPermissions')}</p>
                                    <AvatarPermEditor channelId="wecom" userId={wecomDraft.my_user_id} />
                                  </td>
                                </tr>
                              </>
                            )}
                          </tbody>
                        </table>
                      )}
                    </div>
                  </div>
                ) : null}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

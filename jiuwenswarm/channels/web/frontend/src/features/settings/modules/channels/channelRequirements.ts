import type { FormRule, FormRules, FormValues } from '../../../../components/form';
import type {
  DingtalkFormValues,
  DiscordFormValues,
  FeishuAppDraft,
  SlackFormValues,
  SettingsChannelId,
  TelegramFormValues,
  WhatsAppFormValues,
  XiaoyiFormValues,
} from './channelTypes';

type FieldRequirement = 'required' | 'optional' | 'defined';

type ChannelFormValuesById = {
  xiaoyi: XiaoyiFormValues;
  feishu: FeishuAppDraft;
  dingtalk: DingtalkFormValues;
  telegram: TelegramFormValues;
  discord: DiscordFormValues;
  slack: SlackFormValues;
  whatsapp: WhatsAppFormValues;
};

type ChannelFieldRequirements = {
  [K in SettingsChannelId]: Partial<Record<keyof ChannelFormValuesById[K], FieldRequirement>>;
};

/**
 * 频道字段业务规则的唯一事实源。
 *
 * required: 后端启动频道前明确校验为非空；
 * defined: 表单始终提供确定值的开关或枚举，不展示“可选”；
 * optional: 后端启动不依赖该字段，允许留空。
 */
export const CHANNEL_FIELD_REQUIREMENTS = {
  xiaoyi: {
    enable_streaming: 'defined',
    ak: 'required',
    sk: 'required',
    agent_id: 'required',
    api_id: 'optional',
  },
  feishu: {
    name: 'optional',
    enable_streaming: 'defined',
    app_id: 'required',
    app_secret: 'required',
    encrypt_key: 'optional',
    verification_token: 'optional',
    group_digital_avatar: 'defined',
  },
  dingtalk: {
    client_id: 'required',
    client_secret: 'required',
    allow_from: 'optional',
  },
  telegram: {
    bot_token: 'required',
    allow_from: 'optional',
    parse_mode: 'defined',
    group_chat_mode: 'defined',
  },
  discord: {
    block_dm: 'defined',
    bot_token: 'required',
    application_id: 'optional',
    guild_id: 'optional',
    channel_id: 'optional',
    allow_from: 'optional',
  },
  slack: {
    reply_in_thread: 'defined',
    bot_token: 'required',
    app_token: 'required',
    default_channel_id: 'optional',
    allow_from: 'optional',
    allowed_channel_ids: 'optional',
  },
  whatsapp: {
    bridge_ws_url: 'required',
    default_jid: 'optional',
    bridge_command: 'optional',
    bridge_workdir: 'optional',
    allow_from: 'optional',
    enable_streaming: 'defined',
    auto_start_bridge: 'defined',
  },
} as const satisfies ChannelFieldRequirements;

export function shouldConfirmXiaoyiEnable(enabled: boolean, apiId: string): boolean {
  return enabled && apiId.trim().length === 0;
}

function getFieldRequirement(channelId: SettingsChannelId, field: PropertyKey): FieldRequirement {
  const requirement = (CHANNEL_FIELD_REQUIREMENTS[channelId] as Record<PropertyKey, FieldRequirement | undefined>)[
    field
  ];
  if (!requirement) throw new Error(`Missing field requirement for ${channelId}.${String(field)}`);
  return requirement;
}

function getRequiredFields(channelId: SettingsChannelId): string[] {
  return Object.entries(CHANNEL_FIELD_REQUIREMENTS[channelId])
    .filter(([, requirement]) => requirement === 'required')
    .map(([field]) => field);
}

function toRecord(input: unknown): Record<string, unknown> {
  return input && typeof input === 'object' && !Array.isArray(input) ? (input as Record<string, unknown>) : {};
}

function hasRequiredFields(channelId: SettingsChannelId, input: unknown): boolean {
  const data = toRecord(input);
  return getRequiredFields(channelId).every((field) => {
    const value = data[field];
    return typeof value === 'string' && value.trim().length > 0;
  });
}

function hasConfiguredApp(channelId: 'xiaoyi' | 'feishu', input: unknown): boolean {
  const apps = toRecord(input).apps;
  return Array.isArray(apps) && apps.some((app) => hasRequiredFields(channelId, app));
}

export function isChannelFormFieldOptional(channelId: SettingsChannelId, field: PropertyKey): boolean {
  return getFieldRequirement(channelId, field) === 'optional';
}

export function isFeishuAppConfigured(input: unknown): boolean {
  return hasRequiredFields('feishu', input);
}

export const channelConfigurationChecks: Record<SettingsChannelId, (input: unknown) => boolean> = {
  xiaoyi: (input) => hasConfiguredApp('xiaoyi', input),
  feishu: (input) => hasConfiguredApp('feishu', input),
  dingtalk: (input) => hasRequiredFields('dingtalk', input),
  telegram: (input) => hasRequiredFields('telegram', input),
  discord: (input) => hasRequiredFields('discord', input),
  slack: (input) => hasRequiredFields('slack', input),
  whatsapp: (input) => hasRequiredFields('whatsapp', input),
};

function createRequiredTextRules<TValues extends FormValues>(
  fields: readonly (keyof TValues)[],
  requiredMessage: string,
): FormRules<TValues> {
  const rules: FormRules<TValues> = {};
  for (const field of fields) {
    const rule: FormRule<TValues, typeof field> = {
      trigger: 'blur',
      validator: (value) => (typeof value === 'string' && value.trim().length > 0 ? undefined : requiredMessage),
    };
    rules[field] = [rule] as FormRules<TValues>[typeof field];
  }
  return rules;
}

export function createChannelFormRules<TChannelId extends SettingsChannelId>(
  channelId: TChannelId,
  requiredMessage: string,
): FormRules<ChannelFormValuesById[TChannelId]> {
  return createRequiredTextRules<ChannelFormValuesById[TChannelId]>(
    getRequiredFields(channelId) as (keyof ChannelFormValuesById[TChannelId])[],
    requiredMessage,
  );
}

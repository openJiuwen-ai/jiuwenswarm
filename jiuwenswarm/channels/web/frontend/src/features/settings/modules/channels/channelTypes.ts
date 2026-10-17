export type SettingsChannelId = 'xiaoyi' | 'feishu' | 'dingtalk' | 'telegram' | 'discord' | 'slack' | 'whatsapp';

export type SingleSettingsChannelId = Exclude<SettingsChannelId, 'feishu'>;

export type ChannelItem = {
  channel_id: SettingsChannelId;
  logo_src: string;
  enabled: boolean;
};

export type FeishuConfig = {
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

export type FeishuAppConfig = FeishuConfig & {
  name: string;
  is_default: boolean;
};

export type FeishuAppDraft = Omit<FeishuAppConfig, 'allow_from'> & {
  allow_from: string;
};

export type FeishuFormValues = {
  apps: FeishuAppDraft[];
};

export type XiaoyiConfig = {
  enabled: boolean;
  ak: string;
  sk: string;
  agent_id: string;
  api_id: string;
  enable_streaming: boolean;
};

export type XiaoyiAppConfig = XiaoyiConfig & {
  name: string;
  is_default: boolean;
};

export type XiaoyiFormValues = XiaoyiAppConfig;

export type DingtalkFormValues = {
  enabled: boolean;
  client_id: string;
  client_secret: string;
  allow_from: string;
};

export type TelegramFormValues = {
  enabled: boolean;
  bot_token: string;
  allow_from: string;
  parse_mode: string;
  group_chat_mode: string;
};

export type DiscordFormValues = {
  enabled: boolean;
  bot_token: string;
  application_id: string;
  guild_id: string;
  channel_id: string;
  block_dm: boolean;
  allow_from: string;
};

export type SlackFormValues = {
  enabled: boolean;
  bot_token: string;
  app_token: string;
  allow_from: string;
  allowed_channel_ids: string;
  default_channel_id: string;
  reply_in_thread: boolean;
};

export type WhatsAppFormValues = {
  enabled: boolean;
  bridge_ws_url: string;
  default_jid: string;
  allow_from: string;
  enable_streaming: boolean;
  auto_start_bridge: boolean;
  bridge_command: string;
  bridge_workdir: string;
};

export type ChannelDialogTarget = {
  channelId: SettingsChannelId;
  feishuAppIndex?: number;
  addFeishuApp?: boolean;
};

export type PendingDiscardAction = { type: 'close' } | { type: 'open'; target: ChannelDialogTarget };

export type PendingChannelDeletion = {
  channelId: SettingsChannelId;
  accountIndex: number;
  accountName: string;
};

import type { TFunction } from 'i18next';
import type { FormItem, FormValues } from '../../../../components/form';
import { isChannelFormFieldOptional } from './channelRequirements';
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

function passwordLabels(t: TFunction): { show: string; hide: string } {
  return { show: t('channels.showValue'), hide: t('channels.hideValue') };
}

function switchLabel(t: TFunction, field: string): string {
  return t('channels.fieldToggleAria', { field });
}

function withFieldRequirements<TValues extends FormValues>(
  channelId: SettingsChannelId,
  items: FormItem<TValues>[],
): FormItem<TValues>[] {
  return items.map((item) => ({
    ...item,
    required: !isChannelFormFieldOptional(channelId, item.name),
  })) as FormItem<TValues>[];
}

export function createXiaoyiFormItems(t: TFunction): FormItem<XiaoyiFormValues>[] {
  return withFieldRequirements('xiaoyi', [
    {
      name: 'enable_streaming',
      label: 'enable_streaming',
      component: 'switch',
      switchLabel: switchLabel(t, 'enable_streaming'),
    },
    {
      name: 'ak',
      label: 'ak',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.configValue'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'sk',
      label: 'sk',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.configValue'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'agent_id',
      label: 'agent_id',
      component: 'input',
      placeholder: t('channels.placeholders.configValue'),
    },
    {
      name: 'api_id',
      label: 'api_id',
      component: 'input',
      placeholder: t('channels.placeholders.configValue'),
    },
  ]);
}

export function createFeishuAppFormItems(t: TFunction): FormItem<FeishuAppDraft>[] {
  return withFieldRequirements('feishu', [
    {
      name: 'name',
      label: t('channels.feishuApps.appNameLabel'),
      component: 'input',
      placeholder: t('channels.feishuApps.appNamePlaceholder'),
    },
    {
      name: 'enable_streaming',
      label: 'enable_streaming',
      component: 'switch',
      switchLabel: switchLabel(t, 'enable_streaming'),
    },
    {
      name: 'app_id',
      label: 'app_id',
      component: 'input',
      placeholder: t('channels.placeholders.configValue'),
    },
    {
      name: 'app_secret',
      label: 'app_secret',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.configValue'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'encrypt_key',
      label: 'encrypt_key',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.configValue'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'verification_token',
      label: 'verification_token',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.configValue'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'group_digital_avatar',
      label: 'group_digital_avatar',
      component: 'switch',
      switchLabel: switchLabel(t, 'group_digital_avatar'),
    },
  ]);
}

export function createDingtalkFormItems(t: TFunction): FormItem<DingtalkFormValues>[] {
  return withFieldRequirements('dingtalk', [
    {
      name: 'client_id',
      label: 'client_id',
      component: 'input',
      placeholder: t('channels.placeholders.appId'),
    },
    {
      name: 'client_secret',
      label: 'client_secret',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.appSecret'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'allow_from',
      label: 'allow_from',
      component: 'textarea',
      rows: 4,
      placeholder: t('channels.placeholders.employeeIds'),
    },
  ]);
}

export function createTelegramFormItems(t: TFunction): FormItem<TelegramFormValues>[] {
  return withFieldRequirements('telegram', [
    {
      name: 'bot_token',
      label: 'bot_token',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.telegramBotToken'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'allow_from',
      label: 'allow_from',
      component: 'textarea',
      rows: 4,
      placeholder: t('channels.placeholders.telegramUserIds'),
    },
    {
      name: 'parse_mode',
      label: 'parse_mode',
      component: 'select',
      options: [
        { value: 'Markdown', label: 'Markdown' },
        { value: 'HTML', label: 'HTML' },
        { value: 'None', label: 'None' },
      ],
    },
    {
      name: 'group_chat_mode',
      label: 'group_chat_mode',
      component: 'select',
      options: [
        { value: 'mention', label: t('channels.telegramGroupModes.mention') },
        { value: 'reply', label: t('channels.telegramGroupModes.reply') },
        { value: 'all', label: t('channels.telegramGroupModes.all') },
        { value: 'off', label: t('channels.telegramGroupModes.off') },
      ],
    },
  ]);
}

export function createDiscordFormItems(t: TFunction): FormItem<DiscordFormValues>[] {
  return withFieldRequirements('discord', [
    { name: 'block_dm', label: 'block_dm', component: 'switch', switchLabel: switchLabel(t, 'block_dm') },
    {
      name: 'bot_token',
      label: 'bot_token',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.configValue'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'application_id',
      label: 'application_id',
      component: 'input',
      placeholder: t('channels.placeholders.configValue'),
    },
    {
      name: 'guild_id',
      label: 'guild_id',
      component: 'input',
      placeholder: t('channels.placeholders.discordGuildId'),
    },
    {
      name: 'channel_id',
      label: 'channel_id',
      component: 'input',
      placeholder: t('channels.placeholders.discordChannelId'),
    },
    {
      name: 'allow_from',
      label: 'allow_from',
      component: 'textarea',
      rows: 4,
      placeholder: t('channels.placeholders.ids'),
    },
  ]);
}

export function createSlackFormItems(t: TFunction): FormItem<SlackFormValues>[] {
  return withFieldRequirements('slack', [
    {
      name: 'reply_in_thread',
      label: 'reply_in_thread',
      component: 'switch',
      switchLabel: switchLabel(t, 'reply_in_thread'),
    },
    {
      name: 'bot_token',
      label: 'bot_token',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.slackBotToken'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'app_token',
      label: 'app_token',
      component: 'input',
      type: 'password',
      placeholder: t('channels.placeholders.slackAppToken'),
      passwordVisibilityLabels: passwordLabels(t),
    },
    {
      name: 'default_channel_id',
      label: 'default_channel_id',
      component: 'input',
      placeholder: t('channels.placeholders.slackChannelId'),
    },
    {
      name: 'allow_from',
      label: 'allow_from',
      component: 'textarea',
      rows: 4,
      placeholder: t('channels.placeholders.slackUserIds'),
    },
    {
      name: 'allowed_channel_ids',
      label: 'allowed_channel_ids',
      component: 'textarea',
      rows: 4,
      placeholder: t('channels.placeholders.slackChannelIds'),
    },
  ]);
}

export function createWhatsAppFormItems(t: TFunction): FormItem<WhatsAppFormValues>[] {
  return withFieldRequirements('whatsapp', [
    {
      name: 'bridge_ws_url',
      label: 'bridge_ws_url',
      component: 'input',
      placeholder: t('channels.placeholders.configValue'),
    },
    {
      name: 'default_jid',
      label: 'default_jid',
      component: 'input',
      placeholder: t('channels.placeholders.configValue'),
    },
    {
      name: 'bridge_command',
      label: 'bridge_command',
      component: 'input',
      placeholder: t('channels.placeholders.configValue'),
    },
    {
      name: 'bridge_workdir',
      label: 'bridge_workdir',
      component: 'input',
      placeholder: t('channels.placeholders.configValue'),
    },
    {
      name: 'allow_from',
      label: 'allow_from',
      component: 'textarea',
      rows: 4,
      placeholder: t('channels.placeholders.whatsappJids'),
    },
    {
      name: 'enable_streaming',
      label: 'enable_streaming',
      component: 'switch',
      switchLabel: switchLabel(t, 'enable_streaming'),
    },
    {
      name: 'auto_start_bridge',
      label: 'auto_start_bridge',
      component: 'switch',
      switchLabel: switchLabel(t, 'auto_start_bridge'),
    },
  ]);
}

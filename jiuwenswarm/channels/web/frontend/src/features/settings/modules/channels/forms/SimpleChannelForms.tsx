import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import {
  createDingtalkFormItems,
  createDiscordFormItems,
  createSlackFormItems,
  createTelegramFormItems,
  createWhatsAppFormItems,
} from '../channelFormItems';
import { createChannelFormRules } from '../channelRequirements';
import type {
  DingtalkFormValues,
  DiscordFormValues,
  SlackFormValues,
  TelegramFormValues,
  WhatsAppFormValues,
} from '../channelTypes';
import type { ChannelFormController } from '../useChannelForm';
import { StandardChannelForm } from './StandardChannelForm';

export function DingtalkChannelForm({ controller }: { controller: ChannelFormController<DingtalkFormValues> }) {
  const { t } = useTranslation();
  const items = useMemo(() => createDingtalkFormItems(t), [t]);
  const rules = useMemo(() => createChannelFormRules('dingtalk', t('settingsPanel.validation.required')), [t]);
  return <StandardChannelForm controller={controller} items={items} rules={rules} />;
}

export function TelegramChannelForm({ controller }: { controller: ChannelFormController<TelegramFormValues> }) {
  const { t } = useTranslation();
  const items = useMemo(() => createTelegramFormItems(t), [t]);
  const rules = useMemo(() => createChannelFormRules('telegram', t('settingsPanel.validation.required')), [t]);
  return <StandardChannelForm controller={controller} items={items} rules={rules} />;
}

export function DiscordChannelForm({ controller }: { controller: ChannelFormController<DiscordFormValues> }) {
  const { t } = useTranslation();
  const items = useMemo(() => createDiscordFormItems(t), [t]);
  const rules = useMemo(() => createChannelFormRules('discord', t('settingsPanel.validation.required')), [t]);
  return (
    <StandardChannelForm controller={controller} items={items} rules={rules} hint={t('channels.config.discordHint')} />
  );
}

export function SlackChannelForm({ controller }: { controller: ChannelFormController<SlackFormValues> }) {
  const { t } = useTranslation();
  const items = useMemo(() => createSlackFormItems(t), [t]);
  const rules = useMemo(() => createChannelFormRules('slack', t('settingsPanel.validation.required')), [t]);
  return (
    <StandardChannelForm controller={controller} items={items} rules={rules} hint={t('channels.config.slackHint')} />
  );
}

export function WhatsAppChannelForm({ controller }: { controller: ChannelFormController<WhatsAppFormValues> }) {
  const { t } = useTranslation();
  const items = useMemo(() => createWhatsAppFormItems(t), [t]);
  const rules = useMemo(() => createChannelFormRules('whatsapp', t('settingsPanel.validation.required')), [t]);
  return <StandardChannelForm controller={controller} items={items} rules={rules} />;
}

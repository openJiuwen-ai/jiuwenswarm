import type { TFunction } from 'i18next';
import { settingsChannelLogos } from '../../../../assets/settings';
import type { ChannelItem, SettingsChannelId } from './channelTypes';

export const SETTINGS_CHANNEL_IDS: readonly SettingsChannelId[] = [
  'xiaoyi',
  'feishu',
  'dingtalk',
  'telegram',
  'discord',
  'slack',
  'whatsapp',
];

const CHANNEL_LOGOS: Record<SettingsChannelId, string> = {
  xiaoyi: settingsChannelLogos.xiaoyi,
  feishu: settingsChannelLogos.feishu,
  dingtalk: settingsChannelLogos.dingtalk,
  telegram: settingsChannelLogos.telegram,
  discord: settingsChannelLogos.discord,
  slack: settingsChannelLogos.slack,
  whatsapp: settingsChannelLogos.whatsapp,
};

function normalizeEnabledChannels(channels: unknown): Set<string> {
  if (!Array.isArray(channels)) return new Set();
  return new Set(
    channels.flatMap((item) => {
      if (!item || typeof item !== 'object') return [];
      const channelId = (item as { channel_id?: unknown }).channel_id;
      if (typeof channelId !== 'string' || !channelId.trim()) return [];
      return [channelId.trim().toLowerCase()];
    }),
  );
}

export function buildSettingsChannels(channels: unknown): ChannelItem[] {
  const enabledChannels = normalizeEnabledChannels(channels);
  return SETTINGS_CHANNEL_IDS.map((channelId) => ({
    channel_id: channelId,
    logo_src: CHANNEL_LOGOS[channelId],
    enabled: enabledChannels.has(channelId),
  }));
}

export function getSettingsChannelLogo(channelId: SettingsChannelId): string {
  return CHANNEL_LOGOS[channelId];
}

export function getSettingsChannelLabel(t: TFunction, channelId: SettingsChannelId): string {
  return t(`channels.labels.${channelId}`);
}

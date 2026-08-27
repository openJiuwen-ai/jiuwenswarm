import type { SettingsChannelId } from './channelTypes';

export type ChannelGuideLanguage = 'zh' | 'en';

const CHANNEL_GUIDE_DOCS_VERSION = '0.2.5';
const CHANNEL_GUIDE_BASE_URL = `https://gitcode.com/openJiuwen/jiuwenswarm/blob/${CHANNEL_GUIDE_DOCS_VERSION}/docs`;

const CHANNEL_GUIDE_PATHS: Record<ChannelGuideLanguage, Record<SettingsChannelId, string>> = {
  zh: {
    xiaoyi: 'zh/%E5%9B%BD%E5%86%85%E9%A2%91%E9%81%93.md#%E5%B0%8F%E8%89%BA',
    feishu: 'zh/%E5%9B%BD%E5%86%85%E9%A2%91%E9%81%93.md#%E9%A3%9E%E4%B9%A6',
    dingtalk: 'zh/%E5%9B%BD%E5%86%85%E9%A2%91%E9%81%93.md#%E9%92%89%E9%92%89',
    telegram: 'zh/%E6%B5%B7%E5%A4%96%E9%A2%91%E9%81%93.md#telegram',
    discord: 'zh/%E6%B5%B7%E5%A4%96%E9%A2%91%E9%81%93.md#discord',
    slack: 'zh/%E6%B5%B7%E5%A4%96%E9%A2%91%E9%81%93.md#slack',
    whatsapp: 'zh/%E6%B5%B7%E5%A4%96%E9%A2%91%E9%81%93.md#whatsapp',
  },
  en: {
    xiaoyi: 'en/ChinaChannels.md#xiaoyi',
    feishu: 'en/ChinaChannels.md#feishu-lark',
    dingtalk: 'en/ChinaChannels.md#dingtalk',
    telegram: 'en/InternationalChannels.md#telegram',
    discord: 'en/InternationalChannels.md#discord',
    slack: 'en/InternationalChannels.md#slack',
    whatsapp: 'en/InternationalChannels.md#whatsapp',
  },
};

export function getSettingsChannelGuideUrl(channelId: SettingsChannelId, language: ChannelGuideLanguage): string {
  return `${CHANNEL_GUIDE_BASE_URL}/${CHANNEL_GUIDE_PATHS[language][channelId]}`;
}

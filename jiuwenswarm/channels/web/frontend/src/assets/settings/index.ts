import type { FunctionComponent, SVGProps } from 'react';
import GeneralIcon from './navigation/general.svg?react';
import ModelsIcon from './navigation/models.svg?react';
import AgentIcon from './navigation/agent.svg?react';
import BrowserIcon from './navigation/browser.svg?react';
import ChannelsIcon from './navigation/channels.svg?react';
import ExperimentalIcon from './navigation/experimental.svg?react';
import RefreshIcon from './actions/refresh.svg?react';
import EditIcon from './actions/edit.svg?react';
import EnableIcon from './actions/enable.svg?react';
import DisableIcon from './actions/disable.svg?react';
import DeleteIcon from '../delete.svg?react';
import emptyBoxIllustration from './empty/empty-box.svg';
import customModelIcon from './models/custom.svg';
import xiaoyiLogo from './channels/xiaoyi.svg';
import feishuLogo from './channels/feishu.svg';
import dingtalkLogo from './channels/dingtalk.svg';
import telegramLogo from './channels/telegram.svg';
import discordLogo from './channels/discord.svg';
import slackLogo from './channels/slack.svg';
import whatsappLogo from './channels/whatsapp.svg';

export type SettingsNavigationIcon = FunctionComponent<SVGProps<SVGSVGElement>>;

export const settingsNavigationIcons = {
  general: GeneralIcon,
  models: ModelsIcon,
  agent: AgentIcon,
  browser: BrowserIcon,
  channels: ChannelsIcon,
  experimental: ExperimentalIcon,
} as const satisfies Record<string, SettingsNavigationIcon>;

export const settingsActionIcons = {
  refresh: RefreshIcon,
  edit: EditIcon,
  enable: EnableIcon,
  disable: DisableIcon,
  delete: DeleteIcon,
} as const;

export const settingsEmptyBoxIllustration = emptyBoxIllustration;
export const settingsCustomModelIcon = customModelIcon;

export const settingsChannelLogos = {
  xiaoyi: xiaoyiLogo,
  feishu: feishuLogo,
  dingtalk: dingtalkLogo,
  telegram: telegramLogo,
  discord: discordLogo,
  slack: slackLogo,
  whatsapp: whatsappLogo,
} as const;

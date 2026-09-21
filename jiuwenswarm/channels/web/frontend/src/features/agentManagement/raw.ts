import type { CatalogCacheMetadata } from '../catalogCache';
import type { AgentConnectionState } from './types';

export type RawLocalizedText = {
  zh?: string;
  en?: string;
};

export type RawAgentTemplateListItem = {
  id: string;
  packageName?: string;
  displayName?: RawLocalizedText;
  displayDescription?: RawLocalizedText;
  category?: string;
  source?: string;
  installed?: boolean;
  connection_state?: AgentConnectionState;
  enabled?: boolean;
  updateAvailable?: boolean;
  tags?: RawAgentTag[];
  avatar?: string;
  version?: string;
  teamCompatible?: {
    leader?: boolean;
    member?: boolean;
  };
};

export type RawAgentCapability = {
  id?: string;
  displayName?: RawLocalizedText;
  displayDescription?: RawLocalizedText;
  avatar?: string;
};

export type RawAgentTag = RawLocalizedText & {
  id?: string;
};

export type RawAgentTemplateDetail = RawAgentTemplateListItem & {
  version?: string;
  details?: string;
  prompt?: string;
  persona?: string;
  skills?: RawAgentCapability[];
  tools?: RawAgentCapability[];
  rails?: RawAgentCapability[];
  mcps?: RawAgentCapability[];
  quickInputs?: RawLocalizedText[];
  pending_connectors?: string[];
};

export type RawAgentGroupMember = {
  id?: string;
  agentTemplateId?: string;
  displayName?: RawLocalizedText | string;
  displayDescription?: RawLocalizedText | string;
  role?: string;
  avatar?: string;
};

export type RawAgentGroupCapabilities = {
  canUse?: boolean;
  canInstall?: boolean;
  canUninstall?: boolean;
  canPreviewFiles?: boolean;
  canEdit?: boolean;
  canPublish?: boolean;
};

export type RawAgentGroupListItem = {
  id: string;
  name?: string;
  displayName?: RawLocalizedText | string;
  displayDescription?: RawLocalizedText | string;
  category?: string;
  tags?: RawAgentTag[];
  source?: string;
  installed?: boolean;
  avatar?: string;
  memberCount?: number;
  members?: RawAgentGroupMember[];
  skills?: RawAgentCapability[];
  capabilities?: RawAgentGroupCapabilities;
  version?: string;
  updatedAt?: string;
  details?: string;
  persona?: string;
  leaderId?: string;
  quickInputs?: RawLocalizedText[];
};

export type RawAgentGroupDetail = RawAgentGroupListItem & {
  version?: string;
  updatedAt?: string;
  details?: string;
  persona?: string;
  leaderId?: string;
  quickInputs?: RawLocalizedText[];
};

export type RawAgentGroupListPayload = {
  agentGroups?: RawAgentGroupListItem[];
  cache?: import('../catalogCache').CatalogCacheMetadata;
};

export type RawAgentGroupDetailPayload = {
  group?: RawAgentGroupDetail;
};

export type RawAgentListPayload = {
  templates?: RawAgentTemplateListItem[];
};

export type RawAgentDetailPayload = {
  template?: RawAgentTemplateDetail;
};

export type RawAgentFileEntry = {
  path: string;
  type: 'file' | 'dir';
  visible?: boolean;
  size?: number;
  previewable?: boolean;
  children?: RawAgentFileEntry[];
};

export type RawAgentFileListPayload = {
  tree?: RawAgentFileEntry[];
};

export type RawAgentFileReadPayload = {
  path?: string;
  content?: string | null;
  download_url?: string | null;
};

export type RawSkillOption = {
  name?: string;
  display_name?: string;
  description?: string;
  source?: string;
  installed?: boolean;
  kind?: string;
  skill_type?: string;
  marketplace?: string;
  spec?: string;
  install_spec?: string;
};

export type RawSkillListPayload = {
  skills?: RawSkillOption[];
};

export type RawTeamSkillMarketplaceItem = {
  asset_id?: string;
  name?: string;
  display_name?: string;
  short_desc?: string;
  description?: string;
  plugin_type?: string;
};

export type RawTeamSkillMarketplacePayload = {
  success?: boolean;
  detail?: string;
  cache?: CatalogCacheMetadata;
  skills?: RawTeamSkillMarketplaceItem[];
  items?: RawTeamSkillMarketplaceItem[];
};

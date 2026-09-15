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
  skills?: RawAgentCapability[];
  tools?: RawAgentCapability[];
  rails?: RawAgentCapability[];
  mcps?: RawAgentCapability[];
  quickInputs?: RawLocalizedText[];
  pending_connectors?: string[];
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
  children?: RawAgentFileEntry[];
};

export type RawAgentFileListPayload = {
  tree?: RawAgentFileEntry[];
};

export type RawAgentFileReadPayload = {
  path?: string;
  content?: string;
};

export type RawSkillOption = {
  name?: string;
  display_name?: string;
  description?: string;
  source?: string;
  installed?: boolean;
};

export type RawSkillListPayload = {
  skills?: RawSkillOption[];
};

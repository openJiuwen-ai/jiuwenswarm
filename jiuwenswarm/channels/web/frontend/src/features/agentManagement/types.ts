export type AgentSource = 'builtin' | 'local' | 'hub';

export type AgentConnectionState = 'connected' | 'disconnected' | 'connecting';

export type AgentManagementSource = 'live';

export type RequestStatus = 'idle' | 'loading' | 'success' | 'error';

export type AgentCatalogItem = {
  id: string;
  runtimePackageName: string;
  hubAssetId?: string;
  displayName: string;
  description: string;
  category: string;
  source: AgentSource;
  installed: boolean;
  connectionState: AgentConnectionState;
  enabled?: boolean;
  updateAvailable?: boolean;
  tags: Array<{ id: string; label: string }>;
  avatarUrl: string | null;
  version?: string;
  teamCompatible?: {
    leader: boolean;
    member: boolean;
  };
};

export type AgentCapability = {
  id: string;
  name: string;
  description: string;
};

export type AgentDetail = AgentCatalogItem & {
  prompt: string;
  details: string;
  persona: string;
  skills: AgentCapability[];
  tools: AgentCapability[];
  rails: AgentCapability[];
  mcps: AgentCapability[];
  suggestedPrompts: string[];
  pendingConnectors: string[];
};

export type AgentGroupSource = 'builtin' | 'local';

export type AgentGroupMember = {
  id: string;
  agentTemplateId: string;
  displayName: string;
  description: string;
  role: 'leader' | 'member';
  avatarUrl: string | null;
};

export type AgentGroupCapabilities = {
  canUse: boolean;
  canInstall: boolean;
  canUninstall: boolean;
  canPreviewFiles: boolean;
  canEdit: boolean;
  canPublish: boolean;
};

export type AgentGroupCatalogItem = {
  id: string;
  name: string;
  displayName: string;
  description: string;
  category: string;
  source: AgentGroupSource;
  installed: boolean;
  memberCount: number;
  members: AgentGroupMember[];
  skills: AgentCapability[];
  tags: Array<{ id: string; label: string }>;
  avatarUrl: string | null;
  capabilities: AgentGroupCapabilities;
};

export type AgentGroupIdentity = Pick<AgentGroupCatalogItem, 'id' | 'displayName' | 'avatarUrl'>;

export type AgentGroupDetail = AgentGroupCatalogItem & {
  version: string;
  updatedAt: string;
  details: string;
  persona: string;
  leaderId: string;
  quickInputs: string[];
};

export type AgentGroupDraft = {
  id: string;
  name: string;
  description: string;
  persona: string;
  category: string;
  tagIds: string[];
  customTags: string[];
  leaderId: string;
  memberIds: string[];
  skillRefs: string[];
  suggestedPrompts: string[];
};

export type AgentGroupSelectionIntent =
  | { kind: 'keep' }
  | { kind: 'clear' }
  | { kind: 'select'; id: string };

export type DefinitionFileEntry = {
  relativePath: string;
  kind: 'file' | 'directory';
  visible?: boolean;
  size?: number;
  children?: DefinitionFileEntry[];
  previewable: boolean;
};

export type AgentFileContent = {
  relativePath: string;
  content: string;
};

export type SkillOption = {
  id: string;
  name: string;
  description: string;
};

export type McpOption = {
  id: string;
  name: string;
  description: string;
  category: string;
  integrationType: string;
  connectionState: string;
  source: string;
};

export type AgentDraft = {
  id: string;
  name: string;
  description: string;
  persona: string;
  tagIds: string[];
  customTags: string[];
  skillRefs: string[];
  mcpRefs: string[];
  suggestedPrompts: string[];
};

export type AgentSelectionIntent = { kind: 'keep' } | { kind: 'clear' } | { kind: 'select'; id: string };

export type AgentManagementErrorShape = {
  code: string;
  message: string;
  retriable: boolean;
  payload?: unknown;
};

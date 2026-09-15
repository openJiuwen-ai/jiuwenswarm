import type {
  AgentCatalogItem,
  AgentDetail,
  AgentDraft,
  AgentFileContent,
  AgentManagementErrorShape,
  AgentManagementSource,
  AgentSelectionIntent,
  AgentGroupCatalogItem,
  AgentGroupDetail,
  AgentGroupDraft,
  AgentGroupSelectionIntent,
  DefinitionFileEntry,
  McpOption,
  SkillOption,
} from './types';
import { resolveAgentTagPayload } from './tagOptions';

export type AgentInstallResult =
  | { kind: 'ok' }
  | {
      kind: 'auth_required';
      id: string;
      authId: string;
      mcpId: string;
      prompt: string;
      fields: Array<{ name: string; type: string; label: string }>;
    };

export class AgentManagementError extends Error implements AgentManagementErrorShape {
  code: string;
  retriable: boolean;
  payload?: unknown;

  constructor(message: string, code = 'agent_management_request_failed', retriable = true, payload?: unknown) {
    super(message);
    this.name = 'AgentManagementError';
    this.code = code;
    this.retriable = retriable;
    this.payload = payload;
  }
}

export class AgentInstallPendingError extends AgentManagementError {
  pendingConnectors: string[];

  constructor(message: string, pendingConnectors: string[]) {
    super(message, 'agent_install_pending', true);
    this.name = 'AgentInstallPendingError';
    this.pendingConnectors = pendingConnectors;
  }
}

export interface AgentCatalogListOptions {
  enrichTags?: boolean;
  filter?: 'builtin+hub' | 'mine';
  includeTeamCompatibility?: boolean;
}

export interface AgentManagementClient {
  readonly source: AgentManagementSource;
  listCatalog(options?: AgentCatalogListOptions): Promise<AgentCatalogItem[]>;
  getDefinition(id: string): Promise<AgentDetail>;
  getDefinitionFiles(id: string): Promise<DefinitionFileEntry[]>;
  getDefinitionFile(id: string, relativePath: string): Promise<AgentFileContent>;
  listSkillOptions(): Promise<SkillOption[]>;
  listMcpOptions(): Promise<McpOption[]>;
  createAgent(draft: AgentDraft): Promise<void>;
  importAgentTemplate(path: string): Promise<{ id: string }>;
  installDefinition(id: string): Promise<AgentInstallResult>;
  uninstallDefinition(id: string): Promise<{ notice?: string }>;
}

export interface AgentGroupListOptions {
  filter?: 'builtin' | 'local' | 'all';
}

export interface AgentGroupManagementClient {
  readonly source: AgentManagementSource;
  listGroups(options?: AgentGroupListOptions): Promise<AgentGroupCatalogItem[]>;
  getGroup(id: string): Promise<AgentGroupDetail>;
  getGroupFiles(id: string): Promise<DefinitionFileEntry[]>;
  getGroupFile(id: string, relativePath: string): Promise<AgentFileContent>;
  createGroup(draft: AgentGroupDraft): Promise<{ id: string }>;
  importGroup(path: string): Promise<{ id: string }>;
  installGroup(id: string): Promise<void>;
  uninstallGroup(id: string): Promise<{ notice?: string }>;
}

export function buildAgentGroupCreatePayload(draft: AgentGroupDraft): Record<string, unknown> {
  const id = draft.id.trim();
  const memberIds = Array.from(new Set(draft.memberIds.map(memberId => memberId.trim()).filter(Boolean)))
    .filter(memberId => memberId !== draft.leaderId.trim());
  const payload: Record<string, unknown> = {
    id,
    name: draft.name.trim(),
    description: draft.description.trim(),
    persona: draft.persona.trim(),
    leaderId: draft.leaderId.trim(),
    memberIds,
    skills: Array.from(new Set(draft.skillRefs.map(skill => skill.trim()).filter(Boolean))),
    quickInputs: draft.suggestedPrompts.map(prompt => prompt.trim()).filter(Boolean),
  };
  if (draft.category.trim()) payload.category = draft.category.trim();
  const tags = resolveAgentTagPayload(draft.tagIds, draft.customTags);
  if (tags.length > 0) payload.tags = tags;
  return payload;
}

export function resolveAgentGroupSelectionId(
  agent: Pick<AgentCatalogItem, 'id' | 'runtimePackageName'>,
): string {
  return agent.runtimePackageName.trim() || agent.id.trim();
}

export function isAgentGroupAgentSelectable(
  agent: Pick<AgentCatalogItem, 'source' | 'installed' | 'teamCompatible'>,
  mode: 'leader' | 'member',
): boolean {
  if (agent.source === 'hub' && (!agent.installed || !agent.teamCompatible)) return false;
  return mode === 'leader'
    ? agent.teamCompatible?.leader !== false
    : agent.teamCompatible?.member !== false;
}

export function buildAgentGroupSelectionPayloadForMode(
  mode: string | undefined,
  intent: AgentGroupSelectionIntent,
  boundGroupId?: string | null,
  allowUnboundSelection = true,
): Record<string, string> {
  if (
    mode !== 'team'
    || boundGroupId?.trim()
    || !allowUnboundSelection
    || intent.kind !== 'select'
    || !intent.id.trim()
  ) return {};
  return { agent_group_name: intent.id.trim() };
}

export function buildDefinitionSelectionPayload(intent: AgentSelectionIntent): Record<string, string> {
  if (intent.kind === 'select') {
    return { agent_template_name: intent.id };
  }
  if (intent.kind === 'clear') {
    return { agent_template_name: '' };
  }
  return {};
}

export function buildDefinitionSelectionPayloadForMode(
  mode: string | undefined,
  intent: AgentSelectionIntent,
): Record<string, string> {
  return mode === 'agent' ? buildDefinitionSelectionPayload(intent) : {};
}

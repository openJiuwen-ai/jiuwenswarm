import type {
  AgentCatalogItem,
  AgentDetail,
  AgentDraft,
  AgentFileContent,
  AgentManagementErrorShape,
  AgentManagementSource,
  AgentSelectionIntent,
  DefinitionFileEntry,
  McpOption,
  SkillOption,
} from './types';

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
  updateAgent(draft: AgentDraft): Promise<void>;
  deleteDefinition(id: string): Promise<void>;
  importAgentTemplate(path: string): Promise<{ id: string }>;
  installDefinition(id: string): Promise<AgentInstallResult>;
  uninstallDefinition(id: string): Promise<{ notice?: string }>;
}

type AgentSelectionIdentity = Pick<AgentCatalogItem, 'id' | 'runtimePackageName'>;

/** Chat selection is keyed by the runtime package; keep old asset-id selections visible. */
export function getAgentSelectionId(item: AgentSelectionIdentity): string {
  return item.runtimePackageName || item.id;
}

/** Prefer the current runtime identity before falling back to legacy asset ids. */
export function findAgentSelection<T extends AgentSelectionIdentity>(
  items: readonly T[],
  selectedId: string | null | undefined,
): T | null {
  if (!selectedId) return null;
  return items.find((item) => item.runtimePackageName === selectedId)
    ?? items.find((item) => item.id === selectedId)
    ?? null;
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

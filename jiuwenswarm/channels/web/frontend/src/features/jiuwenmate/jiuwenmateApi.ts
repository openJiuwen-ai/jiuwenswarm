import type {
  JiuwenMateCategory,
  JiuwenMateColleague,
  JiuwenMateColleagueDraft,
  JiuwenMateModelOption,
} from './types';

/** Server RPCs handled by the AgentServer JiuwenMate adapter; the server fixes the namespace. */
export const JIUWENMATE_METHODS = {
  ensureDefault: 'jiuwenmate.colleague.ensure_default',
  list: 'jiuwenmate.colleague.list',
  get: 'jiuwenmate.colleague.get',
  create: 'jiuwenmate.colleague.create',
  settingsGet: 'jiuwenmate.colleague.settings.get',
  settingsUpdate: 'jiuwenmate.colleague.settings.update',
  publish: 'jiuwenmate.colleague.publish',
  delete: 'jiuwenmate.colleague.delete',
  modelsList: 'jiuwenmate.models.list',
} as const;

const CATEGORIES: readonly JiuwenMateCategory[] = [
  'education',
  'development',
  'data',
  'document',
  'finance',
  'legal',
  'commerce',
  'content',
  'custom',
];

/** The server stores at most this many characters of an avatar URL; uploaded images are not stored. */
const MAX_AVATAR_URL = 2048;

export type JiuwenMateRequest = (method: string, params?: Record<string, unknown>) => Promise<unknown>;

export interface JiuwenMateWireCapabilities {
  model: string | null;
  skills: string[];
  connectors: string[];
  plugins: string[];
  knowledge: string[];
  starter_prompts: string[];
}

export interface JiuwenMateWireSettings {
  name: string;
  description: string;
  persona?: string;
  tags: string[];
  category: string;
  avatar_url: string;
  capabilities: JiuwenMateWireCapabilities;
}

export interface JiuwenMateModelCatalog {
  models: JiuwenMateModelOption[];
  defaultModelId: string;
  defaultModelLabel: string;
}

export interface JiuwenMatePublishedVersion {
  agentId: string;
  configVersion: string;
  publishedAt: string;
}

type Wire = Record<string, unknown>;

function record(value: unknown): Wire {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Wire) : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function filled(values: readonly string[]): string[] {
  return values.map((item) => item.trim()).filter(Boolean);
}

export function toColleague(value: unknown): JiuwenMateColleague {
  const wire = record(value);
  const capabilities = record(wire.capabilities);
  const category = text(wire.category) as JiuwenMateCategory;
  const avatarUrl = text(wire.avatar_url);
  return {
    id: text(wire.agent_id),
    name: text(wire.name),
    description: text(wire.description),
    tags: texts(wire.tags),
    category: CATEGORIES.includes(category) ? category : 'custom',
    ...(avatarUrl ? { avatarUrl } : {}),
    capabilities: {
      model: text(capabilities.model),
      skills: texts(capabilities.skills),
      connectors: texts(capabilities.connectors),
      plugins: texts(capabilities.plugins),
      knowledge: texts(capabilities.knowledge),
      starterPrompts: texts(capabilities.starter_prompts),
    },
    imConfigured: false,
    disabled: false,
    joinedAt: text(wire.created_at),
    isDefault: wire.is_default === true,
    currentVersion: typeof wire.current_version === 'string' ? wire.current_version : null,
    hasUnpublishedChanges: wire.has_unpublished_changes === true,
  };
}

/** Settings to create a colleague from a draft; a model missing from the list falls back to the server default. */
export function toWireSettings(
  draft: JiuwenMateColleagueDraft,
  models: readonly JiuwenMateModelOption[],
): JiuwenMateWireSettings {
  const { capabilities } = draft;
  const avatarUrl = draft.avatarUrl ?? '';
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    tags: filled(draft.tags),
    category: draft.category,
    avatar_url: avatarUrl.startsWith('data:') || avatarUrl.length > MAX_AVATAR_URL ? '' : avatarUrl,
    capabilities: {
      model: models.some((item) => item.id === capabilities.model) ? capabilities.model : null,
      skills: filled(capabilities.skills),
      connectors: filled(capabilities.connectors),
      plugins: filled(capabilities.plugins),
      knowledge: filled(capabilities.knowledge),
      starter_prompts: filled(capabilities.starterPrompts),
    },
  };
}

export function toModelCatalog(value: unknown): JiuwenMateModelCatalog {
  const wire = record(value);
  const models = (Array.isArray(wire.models) ? wire.models : []).flatMap((item) => {
    const model = record(item);
    const id = text(model.selection_key);
    if (!id) return [];
    return [{ id, label: text(model.display_name) || text(model.model_name) || id, provider: text(model.provider) }];
  });
  return {
    models,
    defaultModelId: text(wire.current_selection),
    defaultModelLabel: text(wire.current_display_name),
  };
}

export function findModel(catalog: JiuwenMateModelCatalog, id: string): JiuwenMateModelOption | null {
  return catalog.models.find((item) => item.id === (id || catalog.defaultModelId)) ?? null;
}

export function createJiuwenMateApi(request: JiuwenMateRequest) {
  const colleague = async (method: string, params?: Record<string, unknown>) =>
    toColleague(record(await request(method, params)).colleague);

  return {
    /** Ensures 小九 exists, then lists every colleague. */
    async loadColleagues(): Promise<JiuwenMateColleague[]> {
      await request(JIUWENMATE_METHODS.ensureDefault);
      const listed = record(await request(JIUWENMATE_METHODS.list));
      return (Array.isArray(listed.colleagues) ? listed.colleagues : []).map(toColleague);
    },
    getColleague: (agentId: string) => colleague(JIUWENMATE_METHODS.get, { agent_id: agentId }),
    createColleague: (draft: JiuwenMateColleagueDraft, models: readonly JiuwenMateModelOption[]) =>
      colleague(JIUWENMATE_METHODS.create, { settings: toWireSettings(draft, models) }),
    async readSettings(agentId: string): Promise<JiuwenMateWireSettings> {
      return record(await request(JIUWENMATE_METHODS.settingsGet, { agent_id: agentId })).settings as JiuwenMateWireSettings;
    },
    updateSettings: (agentId: string, settings: JiuwenMateWireSettings) =>
      colleague(JIUWENMATE_METHODS.settingsUpdate, { agent_id: agentId, settings }),
    async publish(agentId: string): Promise<JiuwenMatePublishedVersion> {
      const version = record(record(await request(JIUWENMATE_METHODS.publish, { agent_id: agentId })).version);
      return {
        agentId: text(version.agent_id),
        configVersion: text(version.config_version),
        publishedAt: text(version.published_at),
      };
    },
    async deleteColleague(agentId: string): Promise<void> {
      await request(JIUWENMATE_METHODS.delete, { agent_id: agentId });
    },
    async listModels(): Promise<JiuwenMateModelCatalog> {
      return toModelCatalog(await request(JIUWENMATE_METHODS.modelsList));
    },
  };
}

export type JiuwenMateApi = ReturnType<typeof createJiuwenMateApi>;

export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return typeof error === 'string' ? error : '';
}

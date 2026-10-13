/**
 * A2A duplex supervisor settings: flat config keys <-> editor values.
 * config.yaml `duplex_router` holds everything except the API key. openai/anthropic use
 * their own key in .env (DUPLEX_ROUTER_API_KEY). jev derives endpoint, model and key from
 * the url host (`jevHostDefaults`); on typesafe.ai / openrouter.ai hosts an empty key uses
 * the shared browser Jev key. config.get never returns a key, only whether it is set, and
 * supplies the Jev default models (from the backend provider table).
 */

export const DUPLEX_MODES = ['off', 'active'] as const;
export const DUPLEX_FORMATS = ['openai', 'anthropic', 'jev'] as const;
export const DUPLEX_JEV_PROVIDERS = ['typesafe', 'openrouter'] as const;
export type DuplexMode = (typeof DUPLEX_MODES)[number];
export type DuplexFormat = (typeof DUPLEX_FORMATS)[number];
export type DuplexJevProvider = (typeof DUPLEX_JEV_PROVIDERS)[number];

export type DuplexDraft = {
  enabled: boolean;
  mode: DuplexMode;
  format: DuplexFormat;
  api_base: string;
  endpoint: string;
  model: string;
  /** Empty keeps the stored key. */
  api_key: string;
  timeout_seconds: string;
};
export type DuplexField = keyof DuplexDraft;
export type DuplexSettings = Omit<DuplexDraft, 'api_key'> & {
  hasApiKey: boolean;
  /** Saved api_key_env: DUPLEX_ROUTER_API_KEY once the user typed an own Jev key. */
  apiKeyEnv: string;
  jev: Record<DuplexJevProvider, { model: string; keyConfigured: boolean }>;
};
export type DuplexFieldError = 'required' | 'timeout' | 'url' | 'endpoint' | 'jevKey';

export const DUPLEX_JEV_DEFAULT_BASE = 'https://api.typesafe.ai/v1';
const OWN_KEY_ENV = 'DUPLEX_ROUTER_API_KEY';

/** Lower-cased url host; '' when not a URL yet (matches the server's comparison). */
function hostOf(apiBase: string): string {
  try {
    return new URL(apiBase.trim()).hostname.toLowerCase();
  } catch {
    return '';
  }
}

/** Only TypeSafe has its own endpoint; every other JEV service uses OpenRouter's /decisions. */
export function jevHostDefaults(apiBase: string): { provider?: DuplexJevProvider; endpoint: string } {
  const host = hostOf(apiBase.trim() || DUPLEX_JEV_DEFAULT_BASE);
  const on = (domain: string) => host === domain || host.endsWith(`.${domain}`);
  if (on('typesafe.ai')) return { provider: 'typesafe', endpoint: '/systemone' };
  return { provider: on('openrouter.ai') ? 'openrouter' : undefined, endpoint: '/decisions' };
}

const MAX_TIMEOUT_SECONDS = 60;
const text = (values: Record<string, unknown>, key: string) => String(values[key] ?? '').trim();
const isTrue = (value: unknown) =>
  value === true ||
  String(value ?? '')
    .trim()
    .toLowerCase() === 'true';
const oneOf = <T extends string>(options: readonly T[], value: string, fallback: T): T =>
  (options as readonly string[]).includes(value) ? (value as T) : fallback;

export function readDuplexSettings(values: Record<string, unknown>): DuplexSettings {
  return {
    enabled: isTrue(values.duplex_router_enabled),
    mode: oneOf(DUPLEX_MODES, text(values, 'duplex_router_mode'), 'off'),
    format: oneOf(DUPLEX_FORMATS, text(values, 'duplex_router_format'), 'openai'),
    api_base: text(values, 'duplex_router_api_base'),
    endpoint: text(values, 'duplex_router_endpoint'),
    model: text(values, 'duplex_router_model'),
    timeout_seconds: text(values, 'duplex_router_timeout_seconds') || '2.0',
    hasApiKey: isTrue(values.duplex_router_api_key_configured),
    apiKeyEnv: text(values, 'duplex_router_api_key_env'),
    jev: Object.fromEntries(
      DUPLEX_JEV_PROVIDERS.map((provider) => [
        provider,
        {
          model: text(values, `duplex_router_jev_${provider}_model`),
          keyConfigured: isTrue(values[`duplex_router_jev_${provider}_key_configured`]),
        },
      ]),
    ) as DuplexSettings['jev'],
  };
}

export function createDuplexDraft(saved: DuplexSettings): DuplexDraft {
  const { enabled, mode, format, api_base, endpoint, model, timeout_seconds } = saved;
  const draft = { enabled, mode, format, api_base, endpoint, model, api_key: '', timeout_seconds };
  return format === 'jev' ? withJevDefaults(draft, saved, true) : draft;
}

/** Fill the Jev url, endpoint and model defaults from the url host. A typed model is kept unless it
 * is a known Jev default; a typed endpoint only with `keepEndpoint` (the url did not change). */
export function withJevDefaults(
  draft: DuplexDraft,
  saved: Pick<DuplexSettings, 'jev'>,
  keepEndpoint = false,
): DuplexDraft {
  const api_base = draft.api_base.trim() || DUPLEX_JEV_DEFAULT_BASE;
  const host = jevHostDefaults(api_base);
  const defaults: string[] = DUPLEX_JEV_PROVIDERS.map((name) => saved.jev[name].model);
  const model = draft.model.trim() && !defaults.includes(draft.model.trim()) ? draft.model : '';
  return {
    ...draft,
    api_base,
    endpoint: keepEndpoint && draft.endpoint.trim() ? draft.endpoint : host.endpoint,
    model: model || (host.provider ? saved.jev[host.provider].model : ''),
  };
}

/** Jev on a typesafe / openrouter host: the own key is optional (empty = shared browser key). */
export function jevSharedKey(draft: Pick<DuplexDraft, 'format' | 'api_base'>): DuplexJevProvider | undefined {
  return draft.format === 'jev' ? jevHostDefaults(draft.api_base).provider : undefined;
}

/** Validation for one field. Required fields only apply while the supervisor is on. */
export function duplexFieldError(
  field: DuplexField,
  draft: DuplexDraft,
  saved: Pick<DuplexSettings, 'hasApiKey' | 'apiKeyEnv' | 'jev' | 'format' | 'api_base'>,
): DuplexFieldError | undefined {
  const value = String(draft[field] ?? '').trim();
  if (field === 'timeout_seconds') {
    const seconds = Number(value);
    return value && Number.isFinite(seconds) && seconds > 0 && seconds <= MAX_TIMEOUT_SECONDS ? undefined : 'timeout';
  }
  if (field === 'api_base' && value && !/^https?:\/\/[^\s/@]+/i.test(value)) return 'url';
  if (field === 'endpoint' && value && !value.startsWith('/')) return 'endpoint';
  if (!draft.enabled) return undefined;
  const shared = jevSharedKey(draft);
  if (shared && field === 'api_key') {
    // The server drops a saved own key when the format or url host changes (see duplex_router_settings).
    const keptOwnKey =
      saved.apiKeyEnv === OWN_KEY_ENV &&
      saved.hasApiKey &&
      saved.format === draft.format &&
      hostOf(saved.api_base) === hostOf(draft.api_base);
    const ownKey = value || keptOwnKey;
    return ownKey || saved.jev[shared].keyConfigured ? undefined : 'jevKey';
  }
  if (draft.format === 'jev' && field === 'api_base') return undefined;
  // Jev on a typesafe / openrouter host falls back to that host's default model.
  if (shared && field === 'model') return undefined;
  if (field === 'api_base' || field === 'model') return value ? undefined : 'required';
  if (field === 'api_key') return value || saved.hasApiKey ? undefined : 'required';
  return undefined;
}

/** The enable switch drives both keys: on -> mode "active", off -> mode "off". */
export function buildDuplexUpdates(draft: DuplexDraft): Record<string, string> {
  const jev = draft.format === 'jev';
  const updates: Record<string, string> = {
    duplex_router_enabled: draft.enabled ? 'true' : 'false',
    duplex_router_mode: draft.enabled ? 'active' : 'off',
    duplex_router_format: draft.format,
    duplex_router_api_base: draft.api_base.trim(),
    duplex_router_endpoint: jev ? draft.endpoint.trim() : '',
    duplex_router_model: draft.model.trim(),
    duplex_router_timeout_seconds: String(draft.timeout_seconds).trim(),
  };
  if (draft.api_key.trim()) updates.duplex_router_api_key = draft.api_key.trim();
  return updates;
}

/** Turn the supervisor off and remove the url, model and its own key (not the shared Jev key). */
export function buildDuplexClearUpdates(): Record<string, string> {
  return {
    duplex_router_enabled: 'false',
    duplex_router_mode: 'off',
    duplex_router_api_base: '',
    duplex_router_endpoint: '',
    duplex_router_model: '',
    duplex_router_api_key: '',
  };
}

export function isDuplexConfigured(saved: DuplexSettings): boolean {
  return Boolean(saved.enabled || saved.api_base || saved.model || saved.hasApiKey);
}

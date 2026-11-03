/**
 * Group team rules (duplex supervisor) settings: flat config keys <-> editor values.
 * config.yaml `duplex_router` holds the switch, format, address and model; the key goes to
 * .env (DUPLEX_ROUTER_API_KEY). A jev supervisor with an empty key, address or model reuses
 * the Jev connection configured in the browser settings.
 */

export const DUPLEX_FORMATS = ['openai', 'anthropic', 'jev'] as const;
export type DuplexFormat = (typeof DUPLEX_FORMATS)[number];

export type DuplexDraft = {
  enabled: boolean;
  format: DuplexFormat;
  api_base: string;
  model: string;
  api_key: string;
};
export type DuplexField = keyof DuplexDraft;
export type DuplexSettings = DuplexDraft & {
  /** The browser Jev connection a jev supervisor falls back to. */
  jev: { apiBase: string; model: string; keyConfigured: boolean };
};
export type DuplexFieldError = 'required' | 'url' | 'jevKey';

const text = (values: Record<string, unknown>, key: string) => String(values[key] ?? '').trim();
const isTrue = (value: unknown) => value === true || String(value ?? '').trim().toLowerCase() === 'true';

export function readDuplexSettings(values: Record<string, unknown>): DuplexSettings {
  const format = text(values, 'duplex_router_format');
  return {
    enabled: isTrue(values.duplex_router_enabled),
    format: (DUPLEX_FORMATS as readonly string[]).includes(format) ? (format as DuplexFormat) : 'openai',
    api_base: text(values, 'duplex_router_api_base'),
    model: text(values, 'duplex_router_model'),
    api_key: text(values, 'duplex_router_api_key'),
    jev: {
      apiBase: text(values, 'duplex_router_jev_api_base'),
      model: text(values, 'duplex_router_jev_model'),
      keyConfigured: isTrue(values.duplex_router_jev_key_configured),
    },
  };
}

export function createDuplexDraft(saved: DuplexSettings): DuplexDraft {
  const { enabled, format, api_base, model, api_key } = saved;
  return { enabled, format, api_base, model, api_key };
}

/** Validation for one field. Required fields only apply while the supervisor is on. */
export function duplexFieldError(
  field: DuplexField,
  draft: DuplexDraft,
  saved: Pick<DuplexSettings, 'jev'>,
): DuplexFieldError | undefined {
  const value = String(draft[field] ?? '').trim();
  if (field === 'api_base' && value && !/^https?:\/\/[^\s/@]+/i.test(value)) return 'url';
  if (!draft.enabled) return undefined;
  if (draft.format === 'jev') {
    // Empty address / model / key: the browser Jev connection is used.
    return field === 'api_key' && !value && !saved.jev.keyConfigured ? 'jevKey' : undefined;
  }
  if (field === 'api_base' || field === 'model' || field === 'api_key') return value ? undefined : 'required';
  return undefined;
}

export function buildDuplexUpdates(draft: DuplexDraft): Record<string, string> {
  return {
    duplex_router_enabled: draft.enabled ? 'true' : 'false',
    duplex_router_format: draft.format,
    duplex_router_api_base: draft.api_base.trim(),
    duplex_router_model: draft.model.trim(),
    duplex_router_api_key: draft.api_key.trim(),
  };
}

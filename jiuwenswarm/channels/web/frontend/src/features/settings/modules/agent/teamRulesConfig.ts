/**
 * Group team rules settings: flat config keys <-> editor values.
 * config.yaml `team_rules` holds the switch, format, address and model; the key goes to
 * .env (TEAM_RULES_API_KEY). A jev supervisor with an empty key, address or model reuses
 * the Jev connection configured in the browser settings.
 */

export const TEAM_RULES_FORMATS = ['openai', 'anthropic', 'jev'] as const;
export type TeamRulesFormat = (typeof TEAM_RULES_FORMATS)[number];

export type TeamRulesDraft = {
  enabled: boolean;
  format: TeamRulesFormat;
  api_base: string;
  model: string;
  api_key: string;
};
export type TeamRulesField = keyof TeamRulesDraft;
export type TeamRulesSaved = TeamRulesDraft & {
  /** The browser Jev connection a jev supervisor falls back to. */
  jev: { apiBase: string; model: string; keyConfigured: boolean };
};
export type TeamRulesFieldError = 'required' | 'url' | 'jevKey';

const text = (values: Record<string, unknown>, key: string) => String(values[key] ?? '').trim();
const isTrue = (value: unknown) => value === true || String(value ?? '').trim().toLowerCase() === 'true';

export function readTeamRulesSettings(values: Record<string, unknown>): TeamRulesSaved {
  const format = text(values, 'team_rules_format');
  return {
    enabled: isTrue(values.team_rules_enabled),
    format: (TEAM_RULES_FORMATS as readonly string[]).includes(format) ? (format as TeamRulesFormat) : 'openai',
    api_base: text(values, 'team_rules_api_base'),
    model: text(values, 'team_rules_model'),
    api_key: text(values, 'team_rules_api_key'),
    jev: {
      apiBase: text(values, 'team_rules_jev_api_base'),
      model: text(values, 'team_rules_jev_model'),
      keyConfigured: isTrue(values.team_rules_jev_key_configured),
    },
  };
}

export function createTeamRulesDraft(saved: TeamRulesSaved): TeamRulesDraft {
  const { enabled, format, api_base, model, api_key } = saved;
  return { enabled, format, api_base, model, api_key };
}

/** Same rule as the server: http(s) with a host, no credentials, no fragment. */
function isServiceUrl(value: string): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  const allowed = ['http:', 'https:'].includes(url.protocol) && url.hostname !== '';
  return allowed && !url.username && !url.password && !url.hash;
}

/** Validation for one field. Required fields only apply while the supervisor is on. */
export function teamRulesFieldError(
  field: TeamRulesField,
  draft: TeamRulesDraft,
  saved: Pick<TeamRulesSaved, 'jev'>,
): TeamRulesFieldError | undefined {
  const value = String(draft[field] ?? '').trim();
  if (field === 'api_base' && value && !isServiceUrl(value)) return 'url';
  if (!draft.enabled) return undefined;
  if (draft.format === 'jev') {
    // Empty address / model / key: the browser Jev connection is used.
    return field === 'api_key' && !value && !saved.jev.keyConfigured ? 'jevKey' : undefined;
  }
  if (field === 'api_base' || field === 'model' || field === 'api_key') return value ? undefined : 'required';
  return undefined;
}

export function buildTeamRulesUpdates(draft: TeamRulesDraft): Record<string, string> {
  return {
    team_rules_enabled: draft.enabled ? 'true' : 'false',
    team_rules_format: draft.format,
    team_rules_api_base: draft.api_base.trim(),
    team_rules_model: draft.model.trim(),
    team_rules_api_key: draft.api_key.trim(),
  };
}

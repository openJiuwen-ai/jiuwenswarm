export type A2AOutboundAvailability = 'available' | 'unreachable' | 'incompatible' | 'review_required';

export interface A2AOutboundInterface {
  protocol_binding: string;
  protocol_version: string;
  url: string;
}

export interface A2AOutboundDiscovery {
  agent_card: Record<string, unknown>;
  discovery_id: string;
  expires_at: string;
  source_url: string;
  card_path: string;
  card_fingerprint: string;
  agent: {
    name: string;
    description: string;
    version: string;
    skills: Array<Record<string, unknown>>;
    compatible_interfaces: A2AOutboundInterface[];
  };
  security_requirements: Array<Record<string, unknown>>;
  warnings: string[];
}

export interface A2AOutboundAgent {
  agent_id: string;
  display_name: string;
  card_revision: number;
  agent_card: Record<string, unknown>;
  selected_interface: A2AOutboundInterface;
  enabled: boolean;
  manager_enabled: boolean;
  user_enabled: boolean;
  effective_enabled: boolean;
  availability: A2AOutboundAvailability;
  has_credential: boolean;
  connect_timeout_seconds: number;
  sync_wait_seconds: number;
  last_checked_at: string | null;
  last_error_summary: string | null;
  pending_revision: Record<string, unknown> | null;
}

export interface A2AOutboundSettings {
  allow_loopback_http: boolean;
}

const asString = (value: unknown): string => (typeof value === 'string' ? value : '');
const asNumber = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0);

function normalizeInterface(value: unknown): A2AOutboundInterface | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const url = asString(item.url);
  const protocolBinding = asString(item.protocol_binding);
  if (!url || !protocolBinding) return null;
  return { url, protocol_binding: protocolBinding, protocol_version: asString(item.protocol_version) };
}

export function normalizeA2AOutboundDiscovery(value: unknown): A2AOutboundDiscovery | null {
  if (!value || typeof value !== 'object') return null;
  const payload = value as Record<string, unknown>;
  const rawAgent = payload.agent;
  if (!rawAgent || typeof rawAgent !== 'object') return null;
  const agent = rawAgent as Record<string, unknown>;
  const interfaces = Array.isArray(agent.compatible_interfaces)
    ? agent.compatible_interfaces.map(normalizeInterface).filter((item): item is A2AOutboundInterface => item !== null)
    : [];
  const discoveryId = asString(payload.discovery_id);
  if (!discoveryId || !asString(agent.name) || interfaces.length === 0) return null;
  return {
    discovery_id: discoveryId,
    agent_card: payload.agent_card && typeof payload.agent_card === 'object' ? (payload.agent_card as Record<string, unknown>) : {},
    expires_at: asString(payload.expires_at),
    source_url: asString(payload.source_url),
    card_path: asString(payload.card_path),
    card_fingerprint: asString(payload.card_fingerprint),
    agent: {
      name: asString(agent.name),
      description: asString(agent.description),
      version: asString(agent.version),
      skills: Array.isArray(agent.skills) ? (agent.skills.filter(item => !!item && typeof item === 'object') as Array<Record<string, unknown>>) : [],
      compatible_interfaces: interfaces,
    },
    security_requirements: Array.isArray(payload.security_requirements) ? (payload.security_requirements as Array<Record<string, unknown>>) : [],
    warnings: Array.isArray(payload.warnings) ? payload.warnings.map(asString).filter(Boolean) : [],
  };
}

export function normalizeA2AOutboundAgent(value: unknown): A2AOutboundAgent | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const availability = asString(item.availability) as A2AOutboundAvailability;
  const selectedInterface = normalizeInterface(item.selected_interface);
  if (!asString(item.agent_id) || !selectedInterface || !['available', 'unreachable', 'incompatible', 'review_required'].includes(availability)) return null;
  const enabled = item.enabled === true;
  return {
    agent_id: asString(item.agent_id),
    display_name: asString(item.display_name),
    card_revision: asNumber(item.card_revision),
    agent_card: item.agent_card && typeof item.agent_card === 'object' ? (item.agent_card as Record<string, unknown>) : {},
    selected_interface: selectedInterface,
    enabled,
    manager_enabled: typeof item.manager_enabled === 'boolean' ? item.manager_enabled : enabled,
    user_enabled: typeof item.user_enabled === 'boolean' ? item.user_enabled : enabled,
    effective_enabled: typeof item.effective_enabled === 'boolean' ? item.effective_enabled : enabled,
    availability,
    has_credential: item.has_credential === true,
    connect_timeout_seconds: asNumber(item.connect_timeout_seconds),
    sync_wait_seconds: asNumber(item.sync_wait_seconds),
    last_checked_at: typeof item.last_checked_at === 'string' ? item.last_checked_at : null,
    last_error_summary: typeof item.last_error_summary === 'string' ? item.last_error_summary : null,
    pending_revision: item.pending_revision && typeof item.pending_revision === 'object' ? (item.pending_revision as Record<string, unknown>) : null,
  };
}

export function normalizeA2AOutboundList(value: unknown): A2AOutboundAgent[] | null {
  if (!value || typeof value !== 'object' || !Array.isArray((value as Record<string, unknown>).items)) return null;
  const result: A2AOutboundAgent[] = [];
  for (const raw of (value as { items: unknown[] }).items) {
    const item = normalizeA2AOutboundAgent(raw);
    if (!item) return null;
    result.push(item);
  }
  return result;
}

export function normalizeA2AOutboundSettings(value: unknown): A2AOutboundSettings | null {
  if (!value || typeof value !== 'object') return null;
  const enabled = (value as Record<string, unknown>).allow_loopback_http;
  return typeof enabled === 'boolean' ? { allow_loopback_http: enabled } : null;
}

export const shouldAcceptA2AOutboundResponse = (generation: number, current: number): boolean => generation === current;

export function createA2AOutboundRequestScope() {
  let generation = 0;
  return {
    next: () => ++generation,
    accepts: (responseGeneration: number) => responseGeneration === generation,
  };
}

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

export function describeA2AOutboundAuthentication(card: Record<string, unknown>, t: (key: string) => string): string {
  const requirements = Array.isArray(card.securityRequirements) ? card.securityRequirements : [];
  const schemes = asRecord(card.securitySchemes);
  const alternatives = requirements.map(requirement => {
    const raw = asRecord(requirement);
    if (!('schemes' in raw) && Object.keys(raw).length) return t('a2aIngress.outbound.credentialHelp.unknown');
    const names = Object.keys(asRecord(raw.schemes));
    if (!names.length) return t('a2aIngress.security.types.none');
    return names
      .map(name => {
        const scheme = asRecord(schemes[name]);
        const apiKey = asRecord(scheme.apiKeySecurityScheme);
        const httpAuth = asRecord(scheme.httpAuthSecurityScheme);
        if (typeof apiKey.name === 'string') {
          const location = typeof apiKey.location === 'string' ? apiKey.location : '';
          const label = ['header', 'query', 'cookie'].includes(location)
            ? t(`a2aIngress.outbound.credentialHelp.${location}`)
            : t('a2aIngress.outbound.credentialHelp.unknown');
          return `API Key (${label}: ${apiKey.name})`;
        }
        if (httpAuth.scheme === 'bearer') return 'Bearer Token';
        if (typeof httpAuth.scheme === 'string') return `HTTP ${httpAuth.scheme}`;
        if (scheme.oauth2SecurityScheme) return 'OAuth 2.0 (Bearer Token)';
        if (scheme.openIdConnectSecurityScheme) return 'OpenID Connect (Bearer Token)';
        return t('a2aIngress.outbound.credentialHelp.unknown');
      })
      .join(' + ');
  });
  return alternatives.length ? alternatives.join(t('a2aIngress.outbound.credentialHelp.or')) : t('a2aIngress.outbound.credentialHelp.undeclared');
}

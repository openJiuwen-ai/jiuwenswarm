const SAFE_FAILURE_KEYS = {
  INVALID_PLUGIN_STRUCTURE: 'invalidPluginStructure',
  PLUGIN_NOT_FOUND: 'pluginNotFound',
  SESSION_EXCHANGE_FAILED: 'sessionExchangeFailed',
  AUTH_REQUIRED: 'authRequired',
  RESOURCE_NOT_FOUND: 'resourceNotFound',
  VERSION_CONFLICT: 'versionConflict',
} as const;

export type PublishFailureKey = (typeof SAFE_FAILURE_KEYS)[keyof typeof SAFE_FAILURE_KEYS] | 'requestFailed';

function errorCode(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return '';
  const candidate = value as { code?: unknown; payload?: unknown };
  if (typeof candidate.code === 'string') return candidate.code;
  if (candidate.payload && typeof candidate.payload === 'object') {
    const payload = candidate.payload as { code?: unknown; error?: { code?: unknown } };
    if (typeof payload.code === 'string') return payload.code;
    if (typeof payload.error?.code === 'string') return payload.error.code;
  }
  return '';
}

export function publishIssueKey(value: unknown): PublishFailureKey {
  const normalized = errorCode(value).trim().replaceAll('-', '_').toUpperCase();
  return SAFE_FAILURE_KEYS[normalized as keyof typeof SAFE_FAILURE_KEYS] || 'requestFailed';
}

export function publishFailureKey(value: unknown): PublishFailureKey {
  return publishIssueKey(value);
}

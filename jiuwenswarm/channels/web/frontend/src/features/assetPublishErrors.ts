const SAFE_FAILURE_KEYS = {
  INVALID_PLUGIN_STRUCTURE: 'invalidPluginStructure',
  PLUGIN_NOT_FOUND: 'pluginNotFound',
  SESSION_EXCHANGE_FAILED: 'sessionExchangeFailed',
  AUTH_REQUIRED: 'authRequired',
  RESOURCE_NOT_FOUND: 'resourceNotFound',
  VERSION_CONFLICT: 'versionConflict',
  HUB_REQUEST_FAILED: 'hubRequestFailed',
  UNDEFINED_PLACEHOLDER: 'undefinedPlaceholder',
  INVALID_AGENT_MCP: 'invalidAgentMcp',
  INVALID_PACKAGE: 'invalidPackage',
  PUBLISH_UNAVAILABLE: 'publishUnavailable',
  UPLOAD_OUTCOME_UNKNOWN: 'uploadOutcomeUnknown',
  SKILLPACK_DISABLED: 'packUnavailable',
  SKILLPACK_MARKET_GATE_REQUIRED: 'packUnavailable',
  SKILLPACK_CONTENT_CHANGED: 'packChanged',
  SKILLPACK_MEMBER_LOCALLY_MODIFIED: 'packChanged',
  SKILLPACK_MEMBER_TYPE_INVALID: 'packMemberInvalid',
  SKILLPACK_MEMBER_HUB_CONFLICT: 'packMemberInvalid',
  SKILLPACK_MEMBER_IDENTITY_CONFLICT: 'packMemberInvalid',
  SKILLPACK_MEMBERS_BLOCKED: 'packMemberInvalid',
  SKILLPACK_NAME_CONFLICT: 'packConflict',
  SKILLPACK_MEMBER_NAME_CONFLICT: 'packConflict',
  SKILLPACK_TARGET_CONFLICT: 'packConflict',
  SKILLPACK_TARGET_REQUIRES_LOCAL_RECORD: 'packConflict',
  SKILLPACK_VERSION_CONFLICT: 'packConflict',
  SKILLPACK_TRANSPORT_CHANGED: 'packConflict',
  SKILLPACK_REVIEW_REJECTED: 'packRejected',
  SKILLPACK_HUB_CHECK_FAILED: 'packCheckFailed',
  SKILLPACK_UPLOAD_UNCONFIRMED: 'packStageReconcile',
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

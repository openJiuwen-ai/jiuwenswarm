export type KVCacheAffinityStatus = {
  enabled: boolean;
  requested_enabled: boolean;
  can_enable: boolean;
  scope: 'configured' | 'agentos' | 'default';
  models: { name: string; capable: boolean }[];
};

export function parseKVCacheAffinityStatus(value: unknown): KVCacheAffinityStatus | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const status = value as KVCacheAffinityStatus;
  if (
    typeof status.enabled !== 'boolean' || typeof status.requested_enabled !== 'boolean' ||
    typeof status.can_enable !== 'boolean' || !['configured', 'agentos', 'default'].includes(status.scope) ||
    !Array.isArray(status.models) || !status.models.every(
      (model) => model && typeof model.name === 'string' && typeof model.capable === 'boolean',
    )
  ) return undefined;
  return status;
}

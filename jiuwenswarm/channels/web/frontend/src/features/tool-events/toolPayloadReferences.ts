import type { ToolPayloadReferences } from '../../types/message';

export function readToolPayloadReferences(value: unknown): ToolPayloadReferences | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const result: ToolPayloadReferences = {};
  for (const key of ['rendered_result', 'result', 'raw_output'] as const) {
    const item = (value as Record<string, unknown>)[key];
    if (!item || typeof item !== 'object') continue;
    const ref = item as Record<string, unknown>;
    if (typeof ref.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(ref.sha256)) continue;
    if (typeof ref.bytes !== 'number' || !Number.isSafeInteger(ref.bytes) || ref.bytes < 0) continue;
    result[key] = {
      sha256: ref.sha256,
      bytes: ref.bytes,
      media_type: typeof ref.media_type === 'string' ? ref.media_type : 'text/plain',
    };
  }
  return Object.keys(result).length ? result : undefined;
}

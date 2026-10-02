import type { PublishMetadata, PublishRecord } from '../types/assetPublish';

export function canShowAssetPublish(installed: boolean | undefined, capability = true): boolean {
  return installed === true && capability;
}

export function publishOutcome(record: PublishRecord): string {
  if (record.execution_status === 'queued' || record.execution_status === 'uploading') return record.execution_status;
  if (record.execution_status === 'failed') return 'failed';
  const result = record.result?.publish_result;
  if (result === 'pending_moderation') return 'pending_moderation';
  if (result === 'published' || result === 'publish_success') return 'published';
  if (result === 'publish_failed') return 'failed';
  return 'unknown';
}
export function validateMetadata(metadata: PublishMetadata): string[] {
  const errors: string[] = [];
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(metadata.asset_name)) errors.push('asset_name');
  if (!/^(?:\d+\.\d+\.\d+|[0-9a-f]{7})$/.test(metadata.version)) errors.push('version');
  if (!metadata.display_name.trim() || metadata.display_name.length > 128) errors.push('display_name');
  return errors;
}
/** Own one id for the entire attempt, including retries after a lost RPC response. */
export function createCommitAttempt<T>(requestId: string, send: (requestId: string) => Promise<T>) {
  let pending: Promise<T> | null = null;
  let completed: { value: T } | null = null;
  return {
    run(): Promise<T> {
      if (completed) return Promise.resolve(completed.value);
      if (!pending)
        pending = send(requestId)
          .then((value) => {
            completed = { value };
            return value;
          })
          .finally(() => {
            pending = null;
          });
      return pending;
    },
  };
}
export function publishTimestamp(value: string | number | undefined): number {
  if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
  return value ? Date.parse(value) : NaN;
}
/** Only explicit pre-submission rejections may discard an idempotency key. */
export function definitiveCommitRejection(error: unknown): 'expired' | 'rejected' | null {
  const failure = error as { code?: unknown; message?: unknown } | null;
  const values = [failure?.code, failure?.message].filter((value): value is string => typeof value === 'string');
  if (values.some((value) => /\bdraft_expired\b/i.test(value))) return 'expired';
  if (
    values.some((value) =>
      /\b(?:invalid_draft|draft_not_found|publish_auth_required|auth_required|invalid_identifier|queue_full|target_conflict)\b/i.test(
        value,
      ),
    )
  )
    return 'rejected';
  return null;
}

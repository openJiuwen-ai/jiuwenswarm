import { webRequest } from './webClient';
import { getStoredOAuthProvider, getStoredOAuthToken } from '../utils/gitcodeOAuth';
import type {
  AssetReference,
  PublishDescription,
  PublishDraft,
  PublishMetadata,
  PublishRecord,
} from '../types/assetPublish';
function request<T>(method: string, params: Record<string, unknown>) {
  return webRequest<T>(
    `assets.publish.${method}`,
    {
      ...params,
      auth: { access_token: getStoredOAuthToken(), oauth_provider: getStoredOAuthProvider() },
    },
    { timeoutMs: 75_000 },
  );
}
export const assetPublishApi = {
  localStatus: (ref: AssetReference) =>
    webRequest<{ state: import('../features/assetPublication').PublicationState }>(
      'assets.publish.local_status',
      { ...ref },
      { timeoutMs: 15_000 },
    ),
  describe: (ref: AssetReference) => request<PublishDescription>('describe', ref),
  prepare: (ref: AssetReference, metadata: PublishMetadata, targetAssetId: string, force: boolean) =>
    request<PublishDraft>('prepare', {
      ...ref,
      metadata: { ...metadata, tags: metadata.tags.filter(Boolean) },
      ...(targetAssetId.trim() ? { target_asset_id: targetAssetId.trim() } : {}),
      force,
    }),
  commit: (draftId: string, requestId: string) =>
    request<PublishRecord>('commit', { draft_id: draftId, request_id: requestId }),
  status: (operationId: string) => request<PublishRecord>('status', { operation_id: operationId }),
  records: (ref: AssetReference) => request<{ records: PublishRecord[] }>('records', ref),
};
export { openAssetPublish } from '../features/assetPublishEvents';

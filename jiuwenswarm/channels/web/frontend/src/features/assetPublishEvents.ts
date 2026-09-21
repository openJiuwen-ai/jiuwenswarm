import type { AssetPublishOpenRequest } from '../types/assetPublish';
export function openAssetPublish(reference: AssetPublishOpenRequest) {
  window.dispatchEvent(new CustomEvent('asset-publish-open', { detail: reference }));
}

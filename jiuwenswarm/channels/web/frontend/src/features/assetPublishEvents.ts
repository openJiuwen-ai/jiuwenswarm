import type { AssetReference } from '../types/assetPublish';
export function openAssetPublish(reference: AssetReference) {
  window.dispatchEvent(new CustomEvent('asset-publish-open', { detail: reference }));
}

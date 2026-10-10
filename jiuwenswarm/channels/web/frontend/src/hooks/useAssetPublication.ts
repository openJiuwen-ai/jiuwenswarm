import { useEffect, useState } from 'react';
import { assetPublishApi } from '../services/assetPublishApi';
import { type PublicationState } from '../features/assetPublication';
import type { AssetReference } from '../types/assetPublish';
export const publicationKey = (ref: AssetReference) => JSON.stringify([ref.kind, ref.local_id]);
export function useAssetPublication(references: AssetReference[]) {
  const signature = JSON.stringify([...new Set(references.map(publicationKey))].sort());
  const [revision, setRevision] = useState(0);
  const [snapshot, setSnapshot] = useState<Record<string, PublicationState>>({});
  useEffect(() => {
    const refresh = () => setRevision((v) => v + 1);
    window.addEventListener('oauth-callback-complete', refresh);
    window.addEventListener('storage', refresh);
    window.addEventListener('asset-publication-changed', refresh);
    return () => {
      window.removeEventListener('oauth-callback-complete', refresh);
      window.removeEventListener('storage', refresh);
      window.removeEventListener('asset-publication-changed', refresh);
    };
  }, []);
  useEffect(() => {
    let cancelled = false;
    const keys: string[] = JSON.parse(signature);
    const valid = () => !cancelled;
    // Local read-only RPCs, bounded concurrency; never block rendering catalog cards.
    let cursor = 0;
    const worker = async () => {
      while (cursor < keys.length && valid()) {
        const key = keys[cursor++];
        const [kind, local_id] = JSON.parse(key);
        let value: PublicationState;
        try {
          value = (await assetPublishApi.localStatus({ kind, local_id })).state;
        } catch {
          if (valid()) setSnapshot((previous) => ({ ...previous, [key]: previous[key] || 'unknown' }));
          continue;
        }
        if (valid()) setSnapshot((previous) => ({ ...previous, [key]: value }));
      }
    };
    void Promise.all(Array.from({ length: Math.min(4, keys.length) }, worker));
    return () => {
      cancelled = true;
    };
  }, [signature, revision]);
  return (ref: AssetReference): PublicationState => snapshot[publicationKey(ref)] || 'loading';
}

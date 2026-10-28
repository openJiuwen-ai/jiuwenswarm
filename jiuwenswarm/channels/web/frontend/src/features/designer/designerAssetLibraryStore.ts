import { create } from 'zustand';
import { generateUuidV4 } from '../../utils/uuid';

export type DesignerAssetKind = 'image' | 'video' | 'audio' | 'other';

export type DesignerAssetSource = 'uploaded' | 'generated';

export type DesignerLibraryAsset = {
  id: string;
  filename: string;
  mime_type: string;
  kind: DesignerAssetKind;
  source: DesignerAssetSource;
  /** Session-local blob URL for preview / node output (uploads). */
  objectUrl: string;
  size: number;
  created_at: number;
  /** Set when the asset is tied to a graph node output. */
  nodeId?: string;
};

type DesignerAssetLibraryStore = {
  assets: DesignerLibraryAsset[];
  addFromFile: (file: File) => DesignerLibraryAsset | null;
  removeAsset: (assetId: string) => void;
  getById: (assetId: string) => DesignerLibraryAsset | undefined;
  clear: () => void;
};

function kindFromMime(mime: string): DesignerAssetKind {
  if (mime.startsWith('image/')) return 'image';
  if (mime.startsWith('video/')) return 'video';
  if (mime.startsWith('audio/')) return 'audio';
  return 'other';
}

function isAllowedMediaFile(file: File): boolean {
  return (
    file.type.startsWith('image/') ||
    file.type.startsWith('video/') ||
    file.type.startsWith('audio/')
  );
}

function newAssetId(): string {
  return `asset_${generateUuidV4().replace(/-/g, '').slice(0, 12)}`;
}

export const useDesignerAssetLibraryStore = create<DesignerAssetLibraryStore>((set, get) => ({
  assets: [],

  addFromFile: (file) => {
    if (!isAllowedMediaFile(file)) return null;
    const mime = file.type || 'application/octet-stream';
    const asset: DesignerLibraryAsset = {
      id: newAssetId(),
      filename: file.name,
      mime_type: mime,
      kind: kindFromMime(mime),
      source: 'uploaded',
      objectUrl: URL.createObjectURL(file),
      size: file.size,
      created_at: Date.now(),
    };
    set({ assets: [asset, ...get().assets] });
    return asset;
  },

  removeAsset: (assetId) => {
    const existing = get().assets.find((asset) => asset.id === assetId);
    if (!existing) return;
    if (existing.objectUrl.startsWith('blob:')) {
      URL.revokeObjectURL(existing.objectUrl);
    }
    set({ assets: get().assets.filter((asset) => asset.id !== assetId) });
  },

  getById: (assetId) => get().assets.find((asset) => asset.id === assetId),

  clear: () => {
    for (const asset of get().assets) {
      if (asset.objectUrl.startsWith('blob:')) {
        URL.revokeObjectURL(asset.objectUrl);
      }
    }
    set({ assets: [] });
  },
}));

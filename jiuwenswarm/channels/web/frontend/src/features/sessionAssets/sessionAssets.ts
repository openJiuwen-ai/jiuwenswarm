// 任务（会话）素材：文件（上传的、agent 生成的）可以起名，并在聊天框里用 @名称 引用。
// 数据放在后端、按会话隔离（session.assets.* RPC，见 server/runtime/session_assets.py），
// 与导演项目无关；这里只是前端的 API + 状态 + @ 引用解析。
import { create } from 'zustand';
import { webRequest } from '../../services/webClient';

export type SessionAssetKind = 'image' | 'video' | 'audio' | 'document';

export interface SessionAsset {
  asset_id: string;
  name: string;
  kind: SessionAssetKind;
  path: string;
  source: 'upload' | 'generated';
  created_at: number;
}

export interface SessionAssetInput {
  path: string;
  source?: 'upload' | 'generated';
  name?: string;
}

type AssetsResponse = { assets?: SessionAsset[] };

interface SessionAssetsState {
  bySession: Record<string, SessionAsset[]>;
  /** 用户在附件卡片上改的名字（按原文件名），会话建好、文件落盘登记时再用上。 */
  pendingNames: Record<string, string>;
  setPendingName: (filename: string, name: string) => void;
  /** 登记已经落盘的上传（media.persist 之后的 media_items），带上用户起的名字。 */
  registerUploads: (sessionId: string, items: ReadonlyArray<Record<string, unknown>>) => Promise<void>;
  refresh: (sessionId: string) => Promise<void>;
  register: (sessionId: string, items: SessionAssetInput[]) => Promise<void>;
  rename: (sessionId: string, assetId: string, name: string) => Promise<void>;
}

const EMPTY: SessionAsset[] = [];

export function selectSessionAssets(sessionId: string | null | undefined) {
  return (state: SessionAssetsState): SessionAsset[] => state.bySession[sessionId ?? ''] ?? EMPTY;
}

export const useSessionAssetsStore = create<SessionAssetsState>((set) => {
  const apply = (sessionId: string, response: AssetsResponse) =>
    set((state) => ({ bySession: { ...state.bySession, [sessionId]: response.assets ?? [] } }));
  return {
    bySession: {},
    pendingNames: {},
    setPendingName: (filename, name) =>
      set((state) => ({ pendingNames: { ...state.pendingNames, [filename]: name } })),
    registerUploads: async (sessionId, items) => {
      const names = useSessionAssetsStore.getState().pendingNames;
      const inputs: SessionAssetInput[] = [];
      for (const item of items) {
        const path = typeof item.path === 'string' ? item.path.trim() : '';
        if (!path) continue;
        const filename = typeof item.filename === 'string' ? item.filename : '';
        inputs.push({ path, source: 'upload', ...(names[filename] ? { name: names[filename] } : {}) });
      }
      await useSessionAssetsStore.getState().register(sessionId, inputs);
    },
    refresh: async (sessionId) => {
      apply(sessionId, await webRequest<AssetsResponse>('session.assets.list', { session_id: sessionId }));
    },
    register: async (sessionId, items) => {
      if (items.length === 0) return;
      apply(sessionId, await webRequest<AssetsResponse>('session.assets.register', { session_id: sessionId, items }));
    },
    rename: async (sessionId, assetId, name) => {
      apply(
        sessionId,
        await webRequest<AssetsResponse>('session.assets.rename', { session_id: sessionId, asset_id: assetId, name }),
      );
    },
  };
});

export {
  assetKindFromMime,
  findReferencedAssets,
  normalizePath,
  resolveAssetDisplayNames,
  stemFilename,
  withAssetReferenceNote,
  type NamedAsset,
} from './assetReferences';

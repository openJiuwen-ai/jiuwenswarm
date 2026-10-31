// 任务（会话）素材：文件（上传的、agent 生成的）可以起名，并在聊天框里用 @名称 引用。
// 数据放在后端、按会话隔离（session.assets.* RPC，见 server/runtime/session_assets.py），
// 与导演项目无关；这里只是前端的 API + 状态 + @ 引用解析。
import { create } from 'zustand';
import { createAssetOpQueue } from './assetOpQueue';
import { samePath } from './assetReferences';
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
  remove: (sessionId: string, assetId: string) => Promise<void>;
  /**
   * 按路径移除一条上传素材（点 x 取消附件用）。必须在队列里跑：取消时该文件的
   * 自动登记（session.assets.register，上传一完成就发）可能还在往返中，等它落地
   * 后再查路径才能查到，否则会留下一个指向已删文件的幽灵素材（#5956）。
   */
  removeUploadByPath: (sessionId: string, path: string) => Promise<void>;
}

const EMPTY: SessionAsset[] = [];

export function selectSessionAssets(sessionId: string | null | undefined) {
  return (state: SessionAssetsState): SessionAsset[] => state.bySession[sessionId ?? ''] ?? EMPTY;
}

export const useSessionAssetsStore = create<SessionAssetsState>((set) => {
  const apply = (sessionId: string, response: AssetsResponse) =>
    set((state) => ({ bySession: { ...state.bySession, [sessionId]: response.assets ?? [] } }));
  // 每个会话一条串行链：响应按请求顺序落地，旧快照不可能覆盖新快照（#5956）。
  const queue = createAssetOpQueue();
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
    refresh: async (sessionId) =>
      queue.run(sessionId, async () => {
        apply(sessionId, await webRequest<AssetsResponse>('session.assets.list', { session_id: sessionId }));
      }),
    register: async (sessionId, items) => {
      if (items.length === 0) return;
      await queue.run(sessionId, async () => {
        apply(sessionId, await webRequest<AssetsResponse>('session.assets.register', { session_id: sessionId, items }));
      });
    },
    rename: async (sessionId, assetId, name) =>
      queue.run(sessionId, async () => {
        apply(
          sessionId,
          await webRequest<AssetsResponse>('session.assets.rename', { session_id: sessionId, asset_id: assetId, name }),
        );
      }),
    remove: async (sessionId, assetId) =>
      queue.run(sessionId, async () => {
        apply(
          sessionId,
          await webRequest<AssetsResponse>('session.assets.remove', { session_id: sessionId, asset_id: assetId }),
        );
      }),
    removeUploadByPath: async (sessionId, path) =>
      queue.run(sessionId, async () => {
        const assets = useSessionAssetsStore.getState().bySession[sessionId] ?? [];
        const asset = assets.find((item) => item.source === 'upload' && samePath(item.path, path));
        if (!asset) return;
        apply(
          sessionId,
          await webRequest<AssetsResponse>('session.assets.remove', {
            session_id: sessionId,
            asset_id: asset.asset_id,
          }),
        );
      }),
  };
});

export {
  assetKindFromMime,
  displayFileName,
  findReferencedAssets,
  normalizePath,
  resolveAssetDisplayNames,
  stemFilename,
  withAssetReferenceNote,
  type NamedAsset,
} from './assetReferences';

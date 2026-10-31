// 任务（会话）素材：文件（上传的、agent 生成的）可以起名，并在聊天框里用 @名称 引用。
// 数据放在后端、按会话隔离（session.assets.* RPC，见 server/runtime/session_assets.py），
// 与导演项目无关；这里只是前端的 API + 状态 + @ 引用解析。
import { create } from 'zustand';
import { webRequest } from '../../services/webClient';
import { normalizePath } from './assetReferences';

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

export type AssetsResponse = { assets?: SessionAsset[]; created?: Array<SessionAsset | null> };

/** Paths whose registration InputArea's live effect has already dispatched this
 *  app session, keyed by session id -- so registerUploads (the post-send path,
 *  which exists to bootstrap the welcome page's first-ever message, but used to
 *  run unconditionally on every send) does not also register them a second
 *  time. Registration is no longer idempotent per path on the backend (a
 *  deliberate re-attach of the same file must get its own new asset, the way
 *  re-attaching an image already does), so this is the only thing standing
 *  between a normal send and a duplicate asset for every attachment. Plain
 *  module state, not store state: nothing renders off it, and "dispatched"
 *  (request sent) is set synchronously, before the response the store's
 *  bySession would otherwise need to arrive -- the one thing that actually
 *  closes the race against a user hitting send within that round trip. */
const dispatchedUploadPaths = new Map<string, Set<string>>();

export function markUploadDispatched(sessionId: string, path: string): void {
  const key = normalizePath(path);
  let dispatched = dispatchedUploadPaths.get(sessionId);
  if (!dispatched) {
    dispatched = new Set();
    dispatchedUploadPaths.set(sessionId, dispatched);
  }
  dispatched.add(key);
}

interface SessionAssetsState {
  bySession: Record<string, SessionAsset[]>;
  /** 用户在附件卡片上改的名字，会话建好、文件落盘登记时再用上——按
   *  normalizePath(path) 键，不能按原文件名键：两个不同文件可以同名
   *  （比如两份不同目录下的 1.json），文件名键会让后改的名字串到不相干
   *  的文件上。 */
  pendingNames: Record<string, string>;
  setPendingName: (path: string, name: string) => void;
  /** 登记已经落盘的上传（media.persist 之后的 media_items），带上用户起的名字；
   *  跳过 InputArea 实时注册 effect 已经处理过的路径（见 dispatchedUploadPaths）。 */
  registerUploads: (sessionId: string, items: ReadonlyArray<Record<string, unknown>>) => Promise<void>;
  refresh: (sessionId: string) => Promise<void>;
  register: (sessionId: string, items: SessionAssetInput[]) => Promise<AssetsResponse>;
  rename: (sessionId: string, assetId: string, name: string) => Promise<void>;
  remove: (sessionId: string, assetId: string) => Promise<void>;
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
    setPendingName: (path, name) =>
      set((state) => ({ pendingNames: { ...state.pendingNames, [normalizePath(path)]: name } })),
    registerUploads: async (sessionId, items) => {
      const names = useSessionAssetsStore.getState().pendingNames;
      const dispatched = dispatchedUploadPaths.get(sessionId);
      const inputs: SessionAssetInput[] = [];
      for (const item of items) {
        const path = typeof item.path === 'string' ? item.path.trim() : '';
        if (!path) continue;
        const key = normalizePath(path);
        if (dispatched?.has(key)) continue;
        const pendingName = names[key];
        inputs.push({ path, source: 'upload', ...(pendingName ? { name: pendingName } : {}) });
      }
      if (inputs.length === 0) return;
      await useSessionAssetsStore.getState().register(sessionId, inputs);
    },
    refresh: async (sessionId) => {
      apply(sessionId, await webRequest<AssetsResponse>('session.assets.list', { session_id: sessionId }));
    },
    register: async (sessionId, items): Promise<AssetsResponse> => {
      if (items.length === 0) return { assets: [], created: [] };
      const response = await webRequest<AssetsResponse>('session.assets.register', { session_id: sessionId, items });
      apply(sessionId, response);
      return response;
    },
    rename: async (sessionId, assetId, name) => {
      apply(
        sessionId,
        await webRequest<AssetsResponse>('session.assets.rename', { session_id: sessionId, asset_id: assetId, name }),
      );
    },
    remove: async (sessionId, assetId) => {
      apply(
        sessionId,
        await webRequest<AssetsResponse>('session.assets.remove', { session_id: sessionId, asset_id: assetId }),
      );
    },
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

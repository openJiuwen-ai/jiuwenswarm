import { create } from 'zustand';
import type { AgentCatalogItem } from '../features/agentManagement/types';

type AgentCatalogStatus = 'idle' | 'loading' | 'success' | 'error';

interface AgentCatalogState {
  catalog: AgentCatalogItem[] | null;
  status: AgentCatalogStatus;
  revision: number;
}

let catalogGeneration = 0;
let pendingLoad: { generation: number; promise: Promise<AgentCatalogItem[]> } | null = null;

export const useAgentCatalogStore = create<AgentCatalogState>(() => ({
  catalog: null,
  status: 'idle',
  revision: 0,
}));

function publishAgentCatalog(catalog: AgentCatalogItem[]): void {
  useAgentCatalogStore.setState({ catalog, status: 'success' });
}

/**
 * 用调用方已加载的目录回填共享缓存（不递增 revision，不触发依赖方重新拉取）。
 * 供 AgentManagementPanel 等已经持有完整目录的入口写入，让 ChatPanel 输入区的
 * 专家 tag 首帧就能解析出 displayName/头像，而不是先显示原始 id 再跳变。
 */
export function seedAgentCatalog(catalog: AgentCatalogItem[]): void {
  if (catalog.length === 0) return;
  publishAgentCatalog(catalog);
}

export function invalidateAgentCatalog(): void {
  catalogGeneration += 1;
  useAgentCatalogStore.setState({
    catalog: null,
    status: 'idle',
    revision: catalogGeneration,
  });
}

export function ensureAgentCatalog(
  loader: () => Promise<AgentCatalogItem[]>,
): Promise<AgentCatalogItem[]> {
  const current = useAgentCatalogStore.getState();
  if (current.catalog) return Promise.resolve(current.catalog);
  if (pendingLoad?.generation === catalogGeneration) return pendingLoad.promise;

  const generation = catalogGeneration;
  useAgentCatalogStore.setState({ status: 'loading' });
  const promise = loader().then(
    (catalog) => {
      if (generation === catalogGeneration) {
        publishAgentCatalog(catalog);
      }
      return catalog;
    },
    (error: unknown) => {
      if (generation === catalogGeneration) {
        useAgentCatalogStore.setState((state) => ({
          status: state.catalog ? 'success' : 'error',
        }));
      }
      throw error;
    },
  );
  pendingLoad = { generation, promise };
  promise.then(
    () => {
      if (pendingLoad?.promise === promise) pendingLoad = null;
    },
    () => {
      if (pendingLoad?.promise === promise) pendingLoad = null;
    },
  );
  return promise;
}

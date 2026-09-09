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

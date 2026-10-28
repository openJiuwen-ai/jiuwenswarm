import { useChatStore } from '../../stores/chatStore';
import { useDesignerRunStore } from './designerRunStore';
import { isDesignerGraphEditingLocked } from './designerGraphLock';
import { useDesignerStore } from './designerStore';

export function useDesignerGraphEditingLocked(): boolean {
  const bootstrapInProgress = useDesignerStore((state) => state.bootstrapInProgress);
  const graph = useDesignerStore((state) => state.domainGraph);
  const nodeStates = useDesignerRunStore((state) => state.nodeStates);
  const runStatus = useDesignerRunStore((state) => state.run?.status ?? null);
  const sessionId = String(graph?.metadata?.session_id ?? '').trim();
  const leaderBusy = useChatStore((state) =>
    sessionId ? state.runtimes[sessionId]?.isProcessing ?? false : false,
  );
  return isDesignerGraphEditingLocked({
    bootstrapInProgress,
    leaderBusy,
    graph,
    nodeStates,
    runStatus,
  });
}

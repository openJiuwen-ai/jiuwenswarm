import { create } from 'zustand';
import { webClient } from '../../services/webClient';
import { isDesignerPreviewGraph } from './designerBootstrapGraph';
import { designerGraphClient } from './designerGraphClient';
import { useDesignerStore } from './designerStore';
import { derivePrimaryAction, type DesignerRunPrimaryAction } from './designerLayerRun';
import { findDesignRunProblem } from './designModelsReadiness';
import { isActiveDesignerRun, designerRunFailureMessage } from './designerRunView';
import {
  DESIGNER_LEADER_NODE_ID,
  DESIGNER_NODE_STATUS_COMPLETED,
  DESIGNER_NODE_STATUS_FAILED,
  DESIGNER_NODE_STATUS_PENDING,
  DESIGNER_RUN_STATUS_RUNNING,
  type AssetRef,
  type DesignerExecutionGraph,
  type DesignerExecutionRun,
  type DesignerNodeActivity,
  type DesignerNodeState,
} from './executionGraphTypes';

/** Session that owns the graph; run triggers must name it so run pushes land
 * in the same session as the canvas (regenerate issue #5326). */
function graphSessionId(graph: DesignerExecutionGraph | null | undefined): string | undefined {
  const raw = graph?.metadata?.session_id;
  const id = typeof raw === 'string' ? raw.trim() : '';
  return id || undefined;
}

type DesignerRunStore = {
  run: DesignerExecutionRun | null;
  nodeStates: Record<string, DesignerNodeState>;
  currentLayerNodeIds: string[];
  isRunning: boolean;
  primaryAction: DesignerRunPrimaryAction;
  boundGraphId: string | null;
  runError: string | null;
  runWarning: string | null;
  leaderActivity: DesignerNodeActivity | null;
  leaderActivityTail: string[];
  applyRun: (run: DesignerExecutionRun | null) => void;
  applyLeaderActivity: (activity: DesignerNodeActivity | null) => void;
  resetForGraph: (graph: DesignerExecutionGraph | null) => void;
  getPrimaryAction: (graph: DesignerExecutionGraph | null) => DesignerRunPrimaryAction;
  advance: (graph: DesignerExecutionGraph) => Promise<void>;
  rerunNode: (graph: DesignerExecutionGraph, nodeId: string) => Promise<void>;
  restart: (graph: DesignerExecutionGraph) => Promise<void>;
  cancel: (graph: DesignerExecutionGraph | null) => Promise<void>;
  patchNodeOutput: (nodeId: string, outputRef: AssetRef) => void;
  /** Local upload preview: mark node completed with the uploaded asset. */
  applyUploadedOutput: (nodeId: string, outputRef: AssetRef) => void;
  /** Clear upload-produced preview on a node. */
  clearUploadedOutput: (nodeId: string) => void;
  /**
   * Re-read the run from the server. Events sent while the page was hidden or
   * the socket was reconnecting are lost, which used to leave the canvas stuck
   * on "Running…" until a full reload (#31).
   */
  reconcile: (graph: DesignerExecutionGraph | null) => Promise<void>;
};

let pollTimer: ReturnType<typeof setInterval> | null = null;
let runtimeBound = false;
let unbindRuntime: (() => void) | null = null;

function clearPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function primaryFrom(
  graph: DesignerExecutionGraph | null,
  nodeStates: Record<string, DesignerNodeState>,
  currentLayerNodeIds: string[],
  isRunning: boolean,
): DesignerRunPrimaryAction {
  return derivePrimaryAction({
    graph,
    nodeStates,
    currentLayerNodeIds,
    isRunning,
  });
}

function applySnapshot(
  run: DesignerExecutionRun | null,
  graph: DesignerExecutionGraph | null,
): Pick<
  DesignerRunStore,
  'run' | 'nodeStates' | 'currentLayerNodeIds' | 'isRunning' | 'primaryAction' | 'boundGraphId'
> {
  const nodeStates = run?.node_states ?? {};
  const currentLayerNodeIds = run?.current_node_ids ?? [];
  const isRunning = run?.status === DESIGNER_RUN_STATUS_RUNNING;
  return {
    run,
    nodeStates,
    currentLayerNodeIds,
    isRunning,
    primaryAction: primaryFrom(graph, nodeStates, currentLayerNodeIds, isRunning),
    boundGraphId: run?.graph_id ?? graph?.graph_id ?? null,
  };
}

async function persistBeforeRun() {
  try {
    await useDesignerStore.getState().flushSave();
  } catch {
    // Keep going; the backend still has the last saved graph.
  }
}

export const useDesignerRunStore = create<DesignerRunStore>((set, get) => ({
  run: null,
  nodeStates: {},
  currentLayerNodeIds: [],
  isRunning: false,
  primaryAction: 'execute',
  boundGraphId: null,
  runError: null,
  runWarning: null,
  leaderActivity: null,
  leaderActivityTail: [],

  applyRun: (run) => {
    const graph = useDesignerStore.getState().domainGraph;
    if (run && graph && run.graph_id !== graph.graph_id) {
      return;
    }
    const prior = get().run;
    const leader = run?.node_states?.[DESIGNER_LEADER_NODE_ID];
    const failureMessage = designerRunFailureMessage(run);
    set({
      ...applySnapshot(run, graph),
      // Async LLM/API failures arrive via designer.run.updated after start
      // succeeds — lift them into runError so DesignerPage can toast.
      runError: failureMessage,
      ...(leader?.activity
        ? {
            leaderActivity: leader.activity,
            leaderActivityTail: leader.activity_tail || [],
          }
        : {}),
    });
    clearPoll();
    if (run && isActiveDesignerRun(run.status)) {
      const runId = run.run_id;
      pollTimer = setInterval(() => {
        void designerGraphClient
          .getRun({ runId })
          .then((result) => get().applyRun(result.run))
          .catch(() => undefined);
      }, 400);
    } else if (
      run &&
      graph &&
      graph.graph_id === run.graph_id &&
      (isActiveDesignerRun(prior?.status) ||
        (prior != null && prior.run_id !== run.run_id))
    ) {
      // The run just reached a terminal state and the server stamped the node
      // configs (last_wan_prompt / regenerate_packet / generate.prompt). Pull
      // the fresh graph so node toolbars show the prompts this run produced
      // instead of pre-run fallbacks (they would otherwise stay stale until a
      // full page reload).
      void designerGraphClient
        .get(run.graph_id)
        .then(({ graph: fresh }) => {
          if (useDesignerStore.getState().domainGraph?.graph_id !== fresh.graph_id) {
            return;
          }
          useDesignerStore.getState().applyGraph(fresh);
        })
        .catch(() => undefined);
    }
  },

  applyLeaderActivity: (activity) => {
    if (!activity) {
      set({ leaderActivity: null, leaderActivityTail: [] });
      return;
    }
    const line = [activity.tool, activity.text].filter(Boolean).join(' · ') || activity.text;
    set((state) => {
      const tail = [...state.leaderActivityTail];
      if (line && tail[tail.length - 1] !== line) tail.push(line);
      return {
        leaderActivity: activity,
        leaderActivityTail: tail.slice(-8),
      };
    });
  },

  resetForGraph: (graph) => {
    clearPoll();
    if (!graph || graph.nodes.length === 0 || isDesignerPreviewGraph(graph)) {
      set({
        ...applySnapshot(null, graph),
        boundGraphId: graph?.graph_id ?? null,
        runError: null,
        runWarning: null,
        leaderActivity: null,
        leaderActivityTail: [],
      });
      return;
    }
    if (get().boundGraphId === graph.graph_id && get().run) {
      set(applySnapshot(get().run, graph));
      return;
    }
    set({
      ...applySnapshot(null, graph),
      boundGraphId: graph.graph_id,
      runError: null,
      runWarning: null,
      leaderActivity: null,
      leaderActivityTail: [],
    });
    void designerGraphClient
      .getRun({ graphId: graph.graph_id })
      .then((result) => {
        if (useDesignerStore.getState().domainGraph?.graph_id !== graph.graph_id) {
          return;
        }
        get().applyRun(result.run);
      })
      .catch(() => undefined);
  },

  getPrimaryAction: (graph) => {
    const state = get();
    return primaryFrom(graph, state.nodeStates, state.currentLayerNodeIds, state.isRunning);
  },

  advance: async (graph) => {
    const state = get();
    if (state.isRunning || graph.nodes.length === 0) return;
    const run = state.run?.graph_id === graph.graph_id ? state.run : null;
    const nodeStates = run ? state.nodeStates : {};
    const currentLayerNodeIds = run ? state.currentLayerNodeIds : [];
    const primary = primaryFrom(graph, nodeStates, currentLayerNodeIds, false);
    // A generation slot the remaining nodes actually use being off or incomplete
    // can only fail the round: report it on the canvas instead of dispatching
    // (#5266). Continue / Retry skip completed nodes, so only the rest count.
    const resumes = primary === 'continue' || primary === 'retry_failed';
    const pending = resumes
      ? graph.nodes.filter((node) => nodeStates[node.id]?.status !== DESIGNER_NODE_STATUS_COMPLETED)
      : graph.nodes;
    const problem = await findDesignRunProblem(pending, graph);
    if (problem) {
      set({ runError: problem, runWarning: null });
      return;
    }
    await persistBeforeRun();
    set({ runError: null, runWarning: null });
    try {
      let result;
      // Always drive the full remaining pipeline in one start — never require
      // repeated Continue clicks between layers / asset approvals.
      if (primary === 'retry_failed') {
        const failedId =
          currentLayerNodeIds.find((nodeId) => nodeStates[nodeId]?.status === DESIGNER_NODE_STATUS_FAILED) ??
          Object.keys(nodeStates).find((nodeId) => nodeStates[nodeId]?.status === DESIGNER_NODE_STATUS_FAILED);
        result = failedId
          ? await designerGraphClient.startRun({
              graphId: graph.graph_id,
              runId: run?.run_id,
              nodeId: failedId,
              sessionId: graphSessionId(graph),
            })
          : await designerGraphClient.startRun({
              graphId: graph.graph_id,
              sessionId: graphSessionId(graph),
            });
      } else if (run?.run_id && primary === 'continue') {
        result = await designerGraphClient.startRun({
          runId: run.run_id,
          sessionId: graphSessionId(graph),
        });
      } else {
        result = await designerGraphClient.startRun({
          graphId: graph.graph_id,
          sessionId: graphSessionId(graph),
        });
      }
      const warning = String(result.warning || result.run?.warning || result.warnings?.[0] || '').trim() || null;
      get().applyRun(result.run);
      if (warning) {
        set({ runWarning: warning });
      }
    } catch (error) {
      set({ runError: error instanceof Error ? error.message : String(error) });
    }
  },

  rerunNode: async (graph, nodeId) => {
    if (get().isRunning || !nodeId) return;
    // No model, no run: regenerating an image / video node with its Settings
    // slot off or incomplete can only fail, so report the same reason the
    // landing page uses instead of dispatching a doomed job (#5266).
    const node = graph.nodes.find((item) => item.id === nodeId);
    const problem = await findDesignRunProblem([node], graph);
    if (problem) {
      set({ runError: problem, runWarning: null });
      return;
    }
    await persistBeforeRun();
    set({ runError: null, runWarning: null });
    try {
      const run = get().run?.graph_id === graph.graph_id ? get().run : null;
      const result = await designerGraphClient.startRun({
        graphId: graph.graph_id,
        runId: run?.run_id,
        nodeId,
        sessionId: graphSessionId(graph),
      });
      const warning = String(result.warning || result.run?.warning || result.warnings?.[0] || '').trim() || null;
      get().applyRun(result.run);
      if (warning) {
        set({ runWarning: warning });
      }
    } catch (error) {
      set({ runError: error instanceof Error ? error.message : String(error) });
    }
  },

  restart: async (graph) => {
    if (get().isRunning || graph.nodes.length === 0) return;
    // Rerun workflow starts over from the first node, so it needs the same
    // models a fresh run does (#5266).
    const problem = await findDesignRunProblem(graph.nodes, graph);
    if (problem) {
      set({ runError: problem, runWarning: null });
      return;
    }
    await persistBeforeRun();
    set({ runError: null, runWarning: null });
    try {
      const result = await designerGraphClient.startRun({
        graphId: graph.graph_id,
        sessionId: graphSessionId(graph),
      });
      const warning = String(result.warning || result.run?.warning || result.warnings?.[0] || '').trim() || null;
      get().applyRun(result.run);
      if (warning) {
        set({ runWarning: warning });
      }
    } catch (error) {
      set({ runError: error instanceof Error ? error.message : String(error) });
    }
  },

  cancel: async (graph) => {
    const runId = get().run?.run_id;
    if (!runId) {
      get().resetForGraph(graph);
      return;
    }
    try {
      const result = await designerGraphClient.cancelRun(runId);
      get().applyRun(result.run);
    } catch (error) {
      set({ runError: error instanceof Error ? error.message : String(error) });
    }
  },

  patchNodeOutput: (nodeId, outputRef) => {
    get().applyUploadedOutput(nodeId, outputRef);
  },

  reconcile: async (graph) => {
    const current = get().run;
    const graphId = graph?.graph_id;
    if (!graphId) return;
    const runId = current?.run_id;
    try {
      const result = await designerGraphClient.getRun(
        runId ? { runId } : { graphId },
      );
      const fresh = result.run;
      if (!fresh) return;
      if (fresh.graph_id !== graphId) return;
      // Nothing changed: keep the current snapshot so polling is untouched.
      if (current && fresh.run_id === current.run_id && fresh.status === current.status) {
        return;
      }
      get().applyRun(fresh);
    } catch {
      // A failed reconcile must never surface as a run error to the user.
    }
  },

  applyUploadedOutput: (nodeId, outputRef) => {
    const state = get();
    const current = state.nodeStates[nodeId] ?? { status: DESIGNER_NODE_STATUS_PENDING };
    const nodeStates = {
      ...state.nodeStates,
      [nodeId]: {
        ...current,
        status: DESIGNER_NODE_STATUS_COMPLETED,
        output_ref: outputRef,
        error: null,
        completed_at: Date.now(),
      },
    };
    const run = state.run
      ? {
          ...state.run,
          node_states: nodeStates,
          updated_at: Date.now(),
        }
      : null;
    set({
      run,
      nodeStates,
    });
  },

  clearUploadedOutput: (nodeId) => {
    const state = get();
    if (!state.nodeStates[nodeId]) return;
    const nodeStates = {
      ...state.nodeStates,
      [nodeId]: {
        ...state.nodeStates[nodeId],
        status: DESIGNER_NODE_STATUS_PENDING,
        output_ref: null,
        error: null,
        completed_at: null,
      },
    };
    const run = state.run
      ? {
          ...state.run,
          node_states: nodeStates,
          updated_at: Date.now(),
        }
      : null;
    set({
      run,
      nodeStates,
    });
  },
}));

export function bindDesignerRuntime(): () => void {
  if (runtimeBound && unbindRuntime) {
    return unbindRuntime;
  }
  runtimeBound = true;
  const matches = (run?: DesignerExecutionRun) => {
    const current = useDesignerRunStore.getState().run;
    const graphId = useDesignerStore.getState().domainGraph?.graph_id;
    return Boolean(run?.run_id && (run.run_id === current?.run_id || (graphId && run.graph_id === graphId)));
  };
  const onVisible = () => {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    void useDesignerRunStore
      .getState()
      .reconcile(useDesignerStore.getState().domainGraph);
  };
  const offRun = webClient.on('designer.run.updated', ({ payload }) => {
    const run = (payload as { run?: DesignerExecutionRun }).run ?? (payload as DesignerExecutionRun);
    if (matches(run)) useDesignerRunStore.getState().applyRun(run);
  });
  const offNode = webClient.on('designer.node.updated', ({ payload }) => {
    const run = (payload as { run?: DesignerExecutionRun }).run;
    if (matches(run)) useDesignerRunStore.getState().applyRun(run as DesignerExecutionRun);
  });
  const offLeader = webClient.on('designer.leader.activity', ({ payload }) => {
    const activity = (payload as { activity?: DesignerNodeActivity }).activity ?? (payload as DesignerNodeActivity);
    if (activity && typeof activity === 'object') {
      useDesignerRunStore.getState().applyLeaderActivity({
        kind: String(activity.kind || 'stage'),
        text: String(activity.text || ''),
        tool: activity.tool ? String(activity.tool) : undefined,
        at: typeof activity.at === 'number' ? activity.at : Date.now(),
      });
    }
  });
  const offGraph = webClient.on('designer.graph.updated', ({ payload }) => {
    const graph = (payload as { graph?: DesignerExecutionGraph }).graph;
    const current = useDesignerStore.getState().domainGraph;
    if (!graph?.graph_id || !current || graph.graph_id !== current.graph_id) return;
    useDesignerStore.getState().applyGraph(graph);
  });
  unbindRuntime = () => {
    offRun();
    offNode();
    offLeader();
    offGraph();
    clearPoll();
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisible);
    }
    runtimeBound = false;
    unbindRuntime = null;
  };
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisible);
  }
  // A reload repairs a missed terminal event, so the same read on bind repairs
  // a page that was restored from bfcache or reconnected silently.
  void useDesignerRunStore
    .getState()
    .reconcile(useDesignerStore.getState().domainGraph);
  return unbindRuntime;
}

export function selectLeaderPeek(state: {
  nodeStates: Record<string, DesignerNodeState>;
  leaderActivity: DesignerNodeActivity | null;
  leaderActivityTail: string[];
}): Pick<DesignerNodeState, 'activity' | 'activity_tail'> | null {
  const fromRun = state.nodeStates[DESIGNER_LEADER_NODE_ID];
  if (fromRun?.activity || (fromRun?.activity_tail && fromRun.activity_tail.length > 0)) {
    return { activity: fromRun.activity, activity_tail: fromRun.activity_tail };
  }
  if (state.leaderActivity || state.leaderActivityTail.length > 0) {
    return { activity: state.leaderActivity || undefined, activity_tail: state.leaderActivityTail };
  }
  return null;
}

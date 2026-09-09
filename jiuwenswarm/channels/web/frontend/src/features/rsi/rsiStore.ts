// RSI 实验状态管理（Zustand）。
// 集中管理：实验列表 / 当前选中实验 / 运行态 KPI / 演进树 / 节点选中。
// 推送事件（P1/P2/P3）的归并也落在本 store，由 rsiEvents hook 驱动。

import { create } from 'zustand';
import type {
  RsiTreeNode,
  RsiTaskListItem,
  RsiTaskGetResult,
  RsiTreeGetResult,
  RsiReportGetResult,
  RsiUsageGetResult,
  RsiTaskStatus,
  RsiTrainingStatusChangedPayload,
  RsiTrainingProgressPayload,
  RsiTrainingTreeDeltaPayload,
} from './types';

interface RsiDetailState {
  task: RsiTaskGetResult | null;
  report: RsiReportGetResult | null;
  usage: RsiUsageGetResult | null;
  tree: RsiTreeGetResult | null;
  selectedNodeId: string | null;
  // 运行态进度（来自 P2 推送，覆盖 task.progress 展示最新）
  liveProgress: {
    iteration: number;
    total: number;
    score: number | null;
    baseline: number | null;
    usageCost: number | null;
  } | null;
  // P3 可能先于首次 tree.get 到达；先缓存，避免丢掉实时节点。
  pendingTreeNodes: RsiTrainingTreeDeltaPayload['nodes'];
}

interface RsiState {
  list: RsiTaskListItem[];
  listLoading: boolean;
  listError: string | null;

  selectedTaskId: string | null;
  detail: Record<string, RsiDetailState>;
  detailLoading: boolean;
  // 后端 RSI 状态没有 INSTALLED；安装成功后先在前端会话内标记，刷新后以后端状态为准。
  installedTaskIds: Record<string, boolean>;

  // 列表
  loadList: () => Promise<void>;
  // 选中实验；详情由 RsiDetail 挂载/切换时统一拉取
  selectTask: (taskId: string | null) => void;
  // 刷新单个实验详情（task.get / report.get / usage.get / tree.get）
  refreshDetail: (taskId: string) => Promise<void>;

  // 节点选中
  setSelectedNode: (nodeId: string | null) => void;

  // 本地状态变更（创建后插入列表、删除后移除、状态切换后更新）
  upsertListItem: (item: RsiTaskListItem) => void;
  removeListItem: (taskId: string) => void;
  patchTaskStatus: (taskId: string, status: RsiTaskStatus) => void;
  markTaskInstalled: (taskId: string) => void;

  // 推送事件归并
  applyStatusChanged: (payload: RsiTrainingStatusChangedPayload) => void;
  applyProgress: (payload: RsiTrainingProgressPayload) => void;
  applyTreeDelta: (payload: RsiTrainingTreeDeltaPayload) => void;

  reset: () => void;
}

function emptyDetail(): RsiDetailState {
  return {
    task: null,
    report: null,
    usage: null,
    tree: null,
    selectedNodeId: null,
    liveProgress: null,
    pendingTreeNodes: [],
  };
}

// Polling and push-triggered refreshes share one in-flight request per task.
const detailRequests = new Map<string, Promise<void>>();

export const useRsiStore = create<RsiState>((set, get) => ({
  list: [],
  listLoading: false,
  listError: null,
  selectedTaskId: null,
  detail: {},
  detailLoading: false,
  installedTaskIds: {},

  loadList: async () => {
    set({ listLoading: true, listError: null });
    try {
      const { rsiTaskList } = await import('./rsiApi');
      const list = await rsiTaskList();
      set({ list, listLoading: false });
    } catch (e) {
      set({
        listLoading: false,
        listError: e instanceof Error ? e.message : String(e),
      });
    }
  },

  selectTask: (taskId) => {
    set({ selectedTaskId: taskId });
  },

  refreshDetail: (taskId) => {
    const pending = detailRequests.get(taskId);
    if (pending) return pending;
    const request = Promise.resolve()
      .then(async () => {
        set({ detailLoading: true });
        try {
          const [{ rsiTaskGet, rsiReportGet, rsiUsageGet, rsiTreeGet }] = await Promise.all([import('./rsiApi')]);
          const [taskResult, reportResult, usageResult, treeResult] = await Promise.allSettled([
            rsiTaskGet(taskId),
            rsiReportGet(taskId),
            rsiUsageGet(taskId),
            rsiTreeGet(taskId),
          ]);
          if (taskResult.status === 'rejected') throw taskResult.reason;
          const task = taskResult.value;
          const report = reportResult.status === 'fulfilled' ? reportResult.value : null;
          const usage = usageResult.status === 'fulfilled' ? usageResult.value : null;
          const tree = treeResult.status === 'fulfilled' ? treeResult.value : null;
          set((state) => ({
            detail: {
              ...state.detail,
              [taskId]: {
                ...(state.detail[taskId] ?? emptyDetail()),
                task,
                report,
                usage,
                tree: tree ? mergeTree(tree, state.detail[taskId]?.pendingTreeNodes ?? []) : null,
                pendingTreeNodes: tree ? [] : (state.detail[taskId]?.pendingTreeNodes ?? []),
              },
            },
            detailLoading: false,
          }));
        } catch (e) {
          set({ detailLoading: false });
          console.error('[rsi] refreshDetail failed', e);
        }
      })
      .finally(() => {
        detailRequests.delete(taskId);
      });
    detailRequests.set(taskId, request);
    return request;
  },

  setSelectedNode: (nodeId) => {
    const tid = get().selectedTaskId;
    if (!tid) return;
    set((state) => {
      const cur = state.detail[tid] ?? emptyDetail();
      return {
        detail: { ...state.detail, [tid]: { ...cur, selectedNodeId: nodeId } },
      };
    });
  },

  upsertListItem: (item) => {
    set((state) => {
      const idx = state.list.findIndex((t) => t.task_id === item.task_id);
      const list = [...state.list];
      if (idx >= 0) list[idx] = item;
      else list.unshift(item);
      return { list };
    });
  },

  removeListItem: (taskId) => {
    const installedTaskIds = { ...get().installedTaskIds };
    delete installedTaskIds[taskId];
    set((state) => ({
      list: state.list.filter((t) => t.task_id !== taskId),
      detail: Object.fromEntries(Object.entries(state.detail).filter(([id]) => id !== taskId)),
      selectedTaskId: state.selectedTaskId === taskId ? null : state.selectedTaskId,
      installedTaskIds,
    }));
  },

  patchTaskStatus: (taskId, status) => {
    set((state) => {
      const list = state.list.map((t) => (t.task_id === taskId ? { ...t, status, running: status === 'RUNNING' } : t));
      const cur = state.detail[taskId];
      const detail = cur
        ? {
            ...state.detail,
            [taskId]: {
              ...cur,
              task: cur.task ? { ...cur.task, status } : cur.task,
            },
          }
        : state.detail;
      return { list, detail };
    });
  },

  applyStatusChanged: (payload) => {
    get().patchTaskStatus(payload.task_id, payload.status);
    if (
      get().selectedTaskId === payload.task_id &&
      (payload.status === 'COMPLETED' || payload.status === 'FAILED' || payload.status === 'TERMINATED')
    ) {
      void get().refreshDetail(payload.task_id);
    }
  },

  markTaskInstalled: (taskId) => {
    set((state) => ({
      installedTaskIds: { ...state.installedTaskIds, [taskId]: true },
    }));
  },

  applyProgress: (payload) => {
    const tid = payload.task_id;
    set((state) => {
      const cur = state.detail[tid] ?? emptyDetail();
      const list = state.list.map((t) =>
        t.task_id === tid
          ? {
              ...t,
              iter: { current: payload.iteration, total: payload.total_iterations },
              score: payload.score,
              base: payload.baseline ?? t.base ?? null,
            }
          : t,
      );
      return {
        list,
        detail: {
          ...state.detail,
          [tid]: {
            ...cur,
            liveProgress: {
              iteration: payload.iteration,
              total: payload.total_iterations,
              score: payload.score,
              baseline: payload.baseline,
              usageCost: payload.usage?.cost_estimate ?? cur.liveProgress?.usageCost ?? null,
            },
          },
        },
      };
    });
  },

  applyTreeDelta: (payload) => {
    const tid = payload.task_id;
    set((state) => {
      const cur = state.detail[tid] ?? emptyDetail();
      if (!cur.tree) {
        const map = new Map(cur.pendingTreeNodes.map((node) => [node.node_id, node]));
        for (const node of payload.nodes) map.set(node.node_id, node);
        return {
          detail: {
            ...state.detail,
            [tid]: { ...cur, pendingTreeNodes: [...map.values()] },
          },
        };
      }
      const tree = mergeTree(cur.tree, payload.nodes);
      return {
        detail: { ...state.detail, [tid]: { ...cur, tree, pendingTreeNodes: [] } },
      };
    });
  },

  reset: () => {
    set({
      list: [],
      listLoading: false,
      listError: null,
      selectedTaskId: null,
      detail: {},
      detailLoading: false,
      installedTaskIds: {},
    });
  },
}));

function mergeTree(base: RsiTreeGetResult, deltas: RsiTrainingTreeDeltaPayload['nodes']): RsiTreeGetResult {
  if (deltas.length === 0) return base;
  // 增量节点与全量节点按 node_id 去重合并（同源投影，覆盖更新）。
  const map = new Map(base.nodes.map((node) => [node.node_id, node]));
  for (const delta of deltas) {
    map.set(delta.node_id, { ...map.get(delta.node_id), ...delta });
  }
  const nodes = [...map.values()].sort((left, right) => left.iteration - right.iteration);
  const depthOf = (node: RsiTreeNode): number => {
    const seen = new Set<string>([node.node_id]);
    let depth = 0;
    let parent = node.parent_id;
    while (parent && map.has(parent) && !seen.has(parent)) {
      seen.add(parent);
      depth += 1;
      parent = map.get(parent)!.parent_id;
    }
    return depth;
  };
  return {
    nodes,
    depth: Math.max(0, ...nodes.map(depthOf)),
    iteration: nodes.some((node) => node.extra?.program != null)
      ? nodes.filter((node) => node.type === 'ADOPTED' || node.type === 'REJECTED').length
      : Math.max(base.iteration, ...nodes.filter((node) => node.type !== 'PROVISIONAL').map((node) => node.iteration)),
  };
}

/**
 * PersonalContext (主动上下文) 前端状态 store。
 *
 * 集中保存：配置、运行态、图谱、采集服务列表、授权状态、加载态。
 * 面板组件订阅切片，避免 prop drilling；写操作做乐观更新 + 失败回滚。
 */

import { create } from 'zustand';
import {
  type AuthorizationResult,
  type ContextGraph,
  type FetchServiceConfig,
  type FetchServicePatch,
  type FetchProvider,
  type FetchRunRecord,
  type PersonalContextConfig,
  type PersonalContextStatus,
  pcApi,
} from '../services/personalContextApi';

export type InfoTab = 'graph' | 'services';

/** 未配置时的统一投影（与后端 _unconfigured_projection 字段对齐）。 */
const UNCONFIGURED: PersonalContextConfig = {
  configured: false,
  master_enabled: false,
  collection_enabled: false,
  agent_use_enabled: false,
  strategy_profile: 'agent',
  model_index: null,
  model_id: null,
  fetch_services: [],
};

interface PersonalContextState {
  // 数据
  config: PersonalContextConfig;
  status: PersonalContextStatus | null;
  graph: ContextGraph | null;
  authByProvider: Record<string, AuthorizationResult>;
  /** 各服务采集运行历史（由 batchRefresh 一并覆盖，与 config/status 同步写入）。 */
  runHistories: Record<string, FetchRunRecord[]>;

  // UI
  infoTab: InfoTab;
  loadingConfig: boolean;
  loadingStatus: boolean;
  loadingGraph: boolean;
  loadingServices: boolean;
  configNeedsReconciliation: boolean;
  switchWriteGeneration: number;
  /** 按字段记录正在提交中的写操作，用于禁用对应控件。 */
  pendingWrites: Record<string, boolean>;

  // Actions
  setInfoTab: (tab: InfoTab) => void;
  loadConfig: () => Promise<void>;
  loadStatus: () => Promise<void>;
  loadServices: () => Promise<void>;
  loadGraph: () => Promise<void>;
  loadAll: () => Promise<void>;
  reconcileConfig: () => void;

  /** 一次 RTT 拉齐 services + status + runHistories，合并成单次 set()。
   *  由 5s 轮询的刷新任务调用，避免三次独立 set() 触发三次级联渲染。 */
  batchRefresh: () => Promise<void>;

  setEnabled: (enabled: boolean) => Promise<void>;
  setAgentUseEnabled: (enabled: boolean) => Promise<void>;
  /** 总开关（独立持久化）：开启=两个子开关都开，关闭=两个子开关都关；子开关切换不影响它。 */
  setMasterEnabled: (enabled: boolean) => Promise<void>;
  setStrategyProfile: (profile: PersonalContextConfig['strategy_profile']) => Promise<void>;
  selectModel: (modelIndex: number) => Promise<void>;

  createService: (service: FetchServiceConfig) => Promise<void>;
  /** 保存（编辑）已有采集任务：只更新参数，名称/来源不可改。 */
  updateService: (serviceId: string, patch: FetchServicePatch) => Promise<void>;
  deleteService: (serviceId: string) => Promise<void>;
  setServiceEnabled: (serviceId: string, enabled: boolean) => Promise<void>;
  runOne: (serviceId: string) => Promise<void>;
  /** 停止单次采集任务（不改 enabled 自动调度开关）。 */
  stopRun: (serviceId: string) => Promise<void>;

  loadAuthStatus: (provider: string) => Promise<void>;
  /** 授权（飞书 OAuth 设备流不带 credentials；github/gitcode 传 {token}/{pat}）。
   *  reauthorize=true 时强制重新发起授权（飞书已授权后再次授权需传）。 */
  authorizeProvider: (
    provider: string,
    credentials?: Record<string, string>,
    reauthorize?: boolean,
  ) => Promise<AuthorizationResult>;
  /** 派生：provider 是否已授权（飞书/github/gitcode 走 authByProvider 真实态，其余无需授权）。 */
  isProviderAuthorized: (provider: FetchProvider) => boolean;
}

export const usePersonalContextStore = create<PersonalContextState>((set, get) => ({
  config: UNCONFIGURED,
  status: null,
  graph: null,
  authByProvider: {},
  runHistories: {},

  infoTab: 'graph',
  loadingConfig: false,
  loadingStatus: false,
  loadingGraph: false,
  loadingServices: false,
  configNeedsReconciliation: false,
  switchWriteGeneration: 0,
  pendingWrites: {},

  setInfoTab: (tab) => set({ infoTab: tab }),

  loadConfig: async () => {
    const generationAtStart = get().switchWriteGeneration;
    set({ loadingConfig: true });
    try {
      const config = await pcApi.getConfig();
      set((state) =>
        state.switchWriteGeneration === generationAtStart &&
        !state.pendingWrites.collection_enabled &&
        !state.pendingWrites.agent_use_enabled
          ? { config, configNeedsReconciliation: false }
          : state,
      );
    } finally {
      set({ loadingConfig: false });
    }
  },

  reconcileConfig: () => {
    if (get().configNeedsReconciliation) return;
    set({ configNeedsReconciliation: true });
    const retry = async (): Promise<void> => {
      if (!get().configNeedsReconciliation) return;
      try {
        await get().loadConfig();
      } catch {
        // 写入结果不明时保留请求态，下次继续核对 Host 配置。
      }
      if (get().configNeedsReconciliation) {
        globalThis.setTimeout(() => {
          void retry();
        }, 5000);
      }
    };
    void retry();
  },

  loadStatus: async () => {
    set({ loadingStatus: true });
    try {
      const status = await pcApi.getStatus();
      set({ status });
    } finally {
      set({ loadingStatus: false });
    }
  },

  loadServices: async () => {
    set({ loadingServices: true });
    try {
      const { services } = await pcApi.listServices();
      // 合并运行态进 config.fetch_services
      const prev = get().config;
      set({
        config: { ...prev, fetch_services: services },
      });
    } finally {
      set({ loadingServices: false });
    }
  },

  loadGraph: async () => {
    // 防重入：getGraph 走全局流式事件订阅，并发调用会互相收到对方的 nodes/edges 帧，
    // 导致数据串扰；上一轮未结束时跳过本轮，由进行中的那次完成后统一 set。
    if (get().loadingGraph) return;
    set({ loadingGraph: true });
    try {
      const graph = await pcApi.getGraph();
      set({ graph });
    } finally {
      set({ loadingGraph: false });
    }
  },

  loadAll: async () => {
    await Promise.all([get().loadConfig(), get().loadStatus(), get().loadGraph()]);
  },

  batchRefresh: async () => {
    // 开关请求结果不明时额外读取 Host 配置；正常轮询不增加配置请求。
    const generationAtStart = get().switchWriteGeneration;
    const reconcile = get().configNeedsReconciliation;
    const [services, status, runHistories, config] = await Promise.all([
      pcApi.listServices().catch(() => null),
      pcApi.getStatus().catch(() => null),
      pcApi.getRunStatus().catch(() => null),
      reconcile ? pcApi.getConfig().catch(() => null) : Promise.resolve(null),
    ]);
    set((state) => ({
      ...(config &&
      state.configNeedsReconciliation &&
      state.switchWriteGeneration === generationAtStart &&
      !state.pendingWrites.collection_enabled &&
      !state.pendingWrites.agent_use_enabled
        ? {
            config: { ...config, fetch_services: services?.services ?? config.fetch_services },
            configNeedsReconciliation: false,
          }
        : services
          ? { config: { ...state.config, fetch_services: services.services } }
          : {}),
      ...(status ? { status } : {}),
      ...(runHistories
        ? {
            runHistories: Object.fromEntries(
              runHistories.services.map((item) => [item.service_id, item.runs]),
            ),
          }
        : {}),
    }));
  },

  setEnabled: async (enabled) => {
    set((state) => ({
      switchWriteGeneration: state.switchWriteGeneration + 1,
      pendingWrites: { ...state.pendingWrites, collection_enabled: true },
    }));
    const prev = get().config;
    set({ config: { ...prev, collection_enabled: enabled } });
    try {
      const next = enabled ? await pcApi.startRuntime() : await pcApi.stopRuntime();
      set({
        config: next,
        configNeedsReconciliation: false,
        status: await pcApi.getStatus().catch(() => get().status),
      });
    } catch (e) {
      get().reconcileConfig();
      throw e;
    } finally {
      set({ pendingWrites: { ...get().pendingWrites, collection_enabled: false } });
    }
  },

  setMasterEnabled: async (enabled) => {
    // 总开关联动两个子开关，由后端一次 RPC 原子持久化，避免中间状态落盘不一致。前端仍做
    // 乐观更新 + pendingWrites（对齐 setEnabled/setAgentUseEnabled）：切换期间主/子开关都进入
    // 禁用态，失败回滚乐观翻转。
    set((state) => ({
      switchWriteGeneration: state.switchWriteGeneration + 1,
      pendingWrites: {
        ...state.pendingWrites,
        collection_enabled: true,
        agent_use_enabled: true,
      },
    }));
    const prev = get().config;
    set({
      config: {
        ...prev,
        master_enabled: enabled,
        collection_enabled: enabled,
        agent_use_enabled: enabled,
      },
    });
    try {
      const next = await pcApi.setMasterEnabled(enabled);
      set({
        config: next,
        configNeedsReconciliation: false,
        status: await pcApi.getStatus().catch(() => get().status),
      });
    } catch (e) {
      get().reconcileConfig();
      throw e;
    } finally {
      set({
        pendingWrites: {
          ...get().pendingWrites,
          collection_enabled: false,
          agent_use_enabled: false,
        },
      });
    }
  },

  setAgentUseEnabled: async (enabled) => {
    set({ pendingWrites: { ...get().pendingWrites, agent_use_enabled: true } });
    const prev = get().config;
    set({ config: { ...prev, agent_use_enabled: enabled } });
    try {
      const next = enabled ? await pcApi.startAgentUse() : await pcApi.stopAgentUse();
      set({ config: next, status: await pcApi.getStatus().catch(() => get().status) });
    } catch (e) {
      set({ config: prev });
      throw e;
    } finally {
      set({ pendingWrites: { ...get().pendingWrites, agent_use_enabled: false } });
    }
  },

  setStrategyProfile: async (profile) => {
    set({ pendingWrites: { ...get().pendingWrites, strategy_profile: true } });
    const prev = get().config;
    set({ config: { ...prev, strategy_profile: profile } });
    try {
      const next = await pcApi.patchConfig({ strategy_profile: profile });
      set({ config: next });
    } catch (e) {
      set({ config: prev });
      throw e;
    } finally {
      set({ pendingWrites: { ...get().pendingWrites, strategy_profile: false } });
    }
  },

  selectModel: async (modelIndex) => {
    set({ pendingWrites: { ...get().pendingWrites, model_index: true } });
    const prev = get().config;
    set({ config: { ...prev, model_index: modelIndex } });
    try {
      const next = await pcApi.selectModel(modelIndex);
      set({ config: next });
    } catch (e) {
      set({ config: prev });
      throw e;
    } finally {
      set({ pendingWrites: { ...get().pendingWrites, model_index: false } });
    }
  },

  createService: async (service) => {
    set({ pendingWrites: { ...get().pendingWrites, create_service: true } });
    try {
      await pcApi.createService(service);
      await get().loadServices();
    } catch (e) {
      const requestTimedOut = (e as { code?: unknown })?.code === 'REQUEST_TIMEOUT';
      if (requestTimedOut) {
        try {
          await get().loadServices();
        } catch {
          // 保留原始超时错误；刷新失败不应掩盖更关键的事实。
        }
        if (get().config.fetch_services.some((item) => item.service_id === service.service_id)) {
          return;
        }
      }
      throw e;
    } finally {
      set({ pendingWrites: { ...get().pendingWrites, create_service: false } });
    }
  },

  updateService: async (serviceId, patch) => {
    set({ pendingWrites: { ...get().pendingWrites, [`patch:${serviceId}`]: true } });
    try {
      await pcApi.patchService(serviceId, patch);
      await get().loadServices();
    } finally {
      const next = { ...get().pendingWrites };
      delete next[`patch:${serviceId}`];
      set({ pendingWrites: next });
    }
  },

  deleteService: async (serviceId) => {
    set({ pendingWrites: { ...get().pendingWrites, [`del:${serviceId}`]: true } });
    try {
      await pcApi.deleteService(serviceId);
      await get().loadServices();
    } finally {
      const next = { ...get().pendingWrites };
      delete next[`del:${serviceId}`];
      set({ pendingWrites: next });
    }
  },

  setServiceEnabled: async (serviceId, enabled) => {
    set({ pendingWrites: { ...get().pendingWrites, [`svc:${serviceId}`]: true } });
    const prev = get().config;
    // 乐观翻转单个服务 enabled
    set({
      config: {
        ...prev,
        fetch_services: prev.fetch_services.map((s) =>
          s.service_id === serviceId ? { ...s, enabled } : s,
        ),
      },
    });
    try {
      if (enabled) await pcApi.startService(serviceId);
      else await pcApi.stopService(serviceId);
      await get().loadServices();
    } catch (e) {
      set({ config: prev });
      throw e;
    } finally {
      const next = { ...get().pendingWrites };
      delete next[`svc:${serviceId}`];
      set({ pendingWrites: next });
    }
  },

  runOne: async (serviceId) => {
    set({ pendingWrites: { ...get().pendingWrites, [`run:${serviceId}`]: true } });
    try {
      await pcApi.runOne(serviceId);
      await get().batchRefresh();
    } finally {
      const next = { ...get().pendingWrites };
      delete next[`run:${serviceId}`];
      set({ pendingWrites: next });
    }
  },

  stopRun: async (serviceId) => {
    set({ pendingWrites: { ...get().pendingWrites, [`stop:${serviceId}`]: true } });
    try {
      await pcApi.stopRun(serviceId);
      await Promise.all([get().loadServices(), get().loadStatus()]);
    } finally {
      const next = { ...get().pendingWrites };
      delete next[`stop:${serviceId}`];
      set({ pendingWrites: next });
    }
  },

  loadAuthStatus: async (provider) => {
    try {
      const result = await pcApi.getAuthStatus(provider);
      set({ authByProvider: { ...get().authByProvider, [provider]: result } });
    } catch {
      // 静默；授权状态读取失败不阻塞主流程
    }
  },

  authorizeProvider: async (provider, credentials, reauthorize) => {
    set({ pendingWrites: { ...get().pendingWrites, [`auth:${provider}`]: true } });
    try {
      const result = await pcApi.authorizeProvider(provider, credentials, reauthorize);
      set({ authByProvider: { ...get().authByProvider, [provider]: result } });
      return result;
    } finally {
      const next = { ...get().pendingWrites };
      delete next[`auth:${provider}`];
      set({ pendingWrites: next });
    }
  },

  isProviderAuthorized: (provider) => {
    if (provider === 'feishu' || provider === 'github' || provider === 'gitcode') {
      return get().authByProvider[provider]?.state === 'authorized';
    }
    return true;
  },
}));

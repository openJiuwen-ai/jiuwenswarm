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
  type PersonalContextConfig,
  type PersonalContextStatus,
  pcApi,
} from '../services/personalContextApi';

export type InfoTab = 'graph' | 'services';

/** 未配置时的统一投影（与后端 _unconfigured_projection 字段对齐）。 */
const UNCONFIGURED: PersonalContextConfig = {
  configured: false,
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

  // UI
  infoTab: InfoTab;
  loadingConfig: boolean;
  loadingStatus: boolean;
  loadingGraph: boolean;
  loadingServices: boolean;
  /** 按字段记录正在提交中的写操作，用于禁用对应控件。 */
  pendingWrites: Record<string, boolean>;

  // Actions
  setInfoTab: (tab: InfoTab) => void;
  loadConfig: () => Promise<void>;
  loadStatus: () => Promise<void>;
  loadServices: () => Promise<void>;
  loadGraph: () => Promise<void>;
  loadAll: () => Promise<void>;

  setEnabled: (enabled: boolean) => Promise<void>;
  setAgentUseEnabled: (enabled: boolean) => Promise<void>;
  /** 总开关：无独立持久化状态，仅联动两个子开关——开启=两者开，关闭=两者关。 */
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
  /** 授权（飞书 OAuth 设备流不带 credentials；github/gitcode 传 {token}/{pat}）。 */
  authorizeProvider: (
    provider: string,
    credentials?: Record<string, string>,
  ) => Promise<AuthorizationResult>;
  /** 派生：provider 是否已授权（飞书/github/gitcode 走 authByProvider 真实态，其余无需授权）。 */
  isProviderAuthorized: (provider: FetchProvider) => boolean;
}

export const usePersonalContextStore = create<PersonalContextState>((set, get) => ({
  config: UNCONFIGURED,
  status: null,
  graph: null,
  authByProvider: {},

  infoTab: 'graph',
  loadingConfig: false,
  loadingStatus: false,
  loadingGraph: false,
  loadingServices: false,
  pendingWrites: {},

  setInfoTab: (tab) => set({ infoTab: tab }),

  loadConfig: async () => {
    set({ loadingConfig: true });
    try {
      const config = await pcApi.getConfig();
      set({ config });
    } finally {
      set({ loadingConfig: false });
    }
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

  setEnabled: async (enabled) => {
    set({ pendingWrites: { ...get().pendingWrites, collection_enabled: true } });
    const prev = get().config;
    set({ config: { ...prev, collection_enabled: enabled } });
    try {
      const next = enabled ? await pcApi.startRuntime() : await pcApi.stopRuntime();
      set({ config: next, status: await pcApi.getStatus().catch(() => get().status) });
    } catch (e) {
      set({ config: prev });
      throw e;
    } finally {
      set({ pendingWrites: { ...get().pendingWrites, collection_enabled: false } });
    }
  },

  setMasterEnabled: async (enabled) => {
    // 总开关为派生状态，本身不落库：开=两个子开关都开，关=两个子开关都关。
    if (enabled) {
      // 开启顺序：先采集（可能触发后端首次初始化 config），再 agent 使用
      await get().setEnabled(true);
      await get().setAgentUseEnabled(true);
    } else {
      await get().setEnabled(false);
      await get().setAgentUseEnabled(false);
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
      await Promise.all([get().loadServices(), get().loadStatus()]);
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

  authorizeProvider: async (provider, credentials) => {
    set({ pendingWrites: { ...get().pendingWrites, [`auth:${provider}`]: true } });
    try {
      const result = await pcApi.authorizeProvider(provider, credentials);
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

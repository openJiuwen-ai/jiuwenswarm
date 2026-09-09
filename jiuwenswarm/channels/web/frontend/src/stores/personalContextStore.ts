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
  type FetchProvider,
  type PersonalContextConfig,
  type PersonalContextStatus,
  clearGithubToken,
  getGithubToken,
  pcApi,
  setGithubToken,
} from '../services/personalContextApi';

export type InfoTab = 'graph' | 'services';

/** 未配置时的统一投影（与后端 _unconfigured_projection 字段对齐）。 */
const UNCONFIGURED: PersonalContextConfig = {
  configured: false,
  collection_enabled: false,
  agent_use_enabled: true,
  strategy_profile: 'rules',
  model_index: null,
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
  /** GitHub PAT 是否已存（localStorage mock；飞书态在 authByProvider.feishu）。 */
  githubAuthorized: boolean;
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
  setStrategyProfile: (profile: PersonalContextConfig['strategy_profile']) => Promise<void>;
  selectModel: (modelIndex: number) => Promise<void>;

  createService: (service: Omit<FetchServiceConfig, 'state' | 'last_error'>) => Promise<void>;
  deleteService: (serviceId: string) => Promise<void>;
  setServiceEnabled: (serviceId: string, enabled: boolean) => Promise<void>;
  runOne: (serviceId: string) => Promise<void>;
  /** 停止单次采集任务（不改 enabled 自动调度开关）。 */
  stopRun: (serviceId: string) => Promise<void>;

  loadAuthStatus: (provider: string) => Promise<void>;
  authorizeProvider: (provider: string) => Promise<AuthorizationResult>;
  /** 保存 GitHub PAT 到 localStorage（后端无 GitHub 授权接口，前端 mock）。 */
  saveGithubAuth: (token: string) => void;
  /** 清除 GitHub PAT。 */
  clearGithubAuth: () => void;
  /** 派生：provider 是否已授权（飞书真态 / github localStorage / 其余 true）。 */
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
  githubAuthorized: !!getGithubToken(),
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
      // 同步 agent_use_enabled：开启上下文时一并开启 agent 使用，关闭时一并关闭
      const synced = { ...next, agent_use_enabled: enabled };
      set({ config: synced, status: await pcApi.getStatus().catch(() => get().status) });
      // 持久化 agent_use_enabled 到后端（与开关值一致）
      void get().setAgentUseEnabled(enabled).catch(() => {});
    } catch (e) {
      set({ config: prev });
      throw e;
    } finally {
      set({ pendingWrites: { ...get().pendingWrites, collection_enabled: false } });
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
    } finally {
      set({ pendingWrites: { ...get().pendingWrites, create_service: false } });
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

  authorizeProvider: async (provider) => {
    set({ pendingWrites: { ...get().pendingWrites, [`auth:${provider}`]: true } });
    try {
      const result = await pcApi.authorizeProvider(provider);
      set({ authByProvider: { ...get().authByProvider, [provider]: result } });
      return result;
    } finally {
      const next = { ...get().pendingWrites };
      delete next[`auth:${provider}`];
      set({ pendingWrites: next });
    }
  },

  saveGithubAuth: (token) => {
    setGithubToken(token);
    set({ githubAuthorized: true });
  },

  clearGithubAuth: () => {
    clearGithubToken();
    set({ githubAuthorized: false });
  },

  isProviderAuthorized: (provider) => {
    if (provider === 'feishu') {
      return get().authByProvider.feishu?.state === 'authorized';
    }
    if (provider === 'github') {
      return get().githubAuthorized;
    }
    return true;
  },
}));

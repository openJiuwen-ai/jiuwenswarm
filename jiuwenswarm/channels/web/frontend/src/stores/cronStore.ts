import { create } from 'zustand';
import {
  buildCronJobFingerprintMap,
  detectCronJobRunUpdates,
  type CronJobRunFingerprint,
} from '../features/cron/cronJobSync';
import { projectRegistryClient } from '../features/workspace/projectRegistryClient';
import { webRequest } from '../services/webClient';
import type { Session } from '../types';
import { useChatStore } from './chatStore';

export interface SidebarCronJob {
  id: string;
  name: string;
  enabled: boolean;
  expired?: boolean;
  cron_expr: string;
  project_id: string;
  session_id?: string;
  // 推送频道 ID,字符串或逗号分隔的多个(对齐后端 CronJob.to_dict().targets);空串按后端默认 web
  targets?: string;
  created_at: number | string | null;
  updated_at: number | string | null;
  /** 最近一次执行完成时间（epoch 秒），来自 cron.job.list */
  last_run_at?: number | null;
  /** 最近一次执行会话 ID，来自 cron.job.list */
  last_session_id?: string | null;
}

/** 判断 cron job 的 targets 是否含 "web"(空串按后端 normalize 默认 web 处理) */
export function isWebChannelJob(targets?: string): boolean {
  const s = (targets ?? '').trim();
  if (!s) return true;
  return s.split(',').some((p) => p.trim().toLowerCase() === 'web');
}

interface CronState {
  jobs: SidebarCronJob[];
  isLoading: boolean;
  expandedCronGroups: Record<string, boolean>;
  // cron_id → 触发会话列表
  cronSessions: Record<string, Session[]>;
  // cron_id → 加载中状态
  cronSessionsLoading: Record<string, boolean>;
  // job_id → 最近"立即执行"返回的 session_id，用于广播消息路由
  lastRunSessionId: Record<string, string>;
  setLastRunSessionId: (jobId: string, sessionId: string) => void;
  // 轮询检测到「当前正打开会话 == 某 job 新 last_session_id」时，投递一次历史刷新请求。
  // 企业版 HTTP 无 cron 结果 push，立即执行跳转后 skipHistoryLoad，消息两头落空；
  // 这里用 30s 轮询作为兜底：命中就把 session_id 交给 App.tsx 触发 startHistoryRestore。
  pendingActiveHistoryRefresh: string | null;
  requestActiveHistoryRefresh: (sessionId: string) => void;
  consumeActiveHistoryRefresh: () => string | null;
  // 定时任务未读状态（is_placeholder=false 的广播到达时标记，点击后清除）
  unreadCronJobs: Record<string, boolean>;
  markCronJobUnread: (jobId: string) => void;
  clearCronJobUnread: (jobId: string) => void;
  loadJobs: () => Promise<void>;
  /** HTTP Pull transport：静默同步执行状态，发现新 run 时标记未读 */
  syncJobRuns: () => Promise<void>;
  /** cron.job.list 运行态指纹，供 Pull sync diff */
  runSyncFingerprints: Record<string, CronJobRunFingerprint>;
  reload: () => Promise<void>;
  toggleCronGroup: (groupId: string) => void;
  loadCronSessions: (projectId: string, cronId: string) => Promise<void>;
  isCronGroupExpanded: (groupId: string) => boolean;
  // projectId → 该 project 下全部 cron 会话（cron_id 非空），会话组按此聚合（不再依赖 job 列表）
  projectCronSessions: Record<string, Session[]>;
  projectCronSessionsLoading: Record<string, boolean>;
  loadProjectCronSessions: (projectId: string) => Promise<void>;
}

// 将未读状态持久化到 localStorage，用 queueMicrotask 延迟到当前同步热路径之后执行，
// 避免阻塞 WebSocket 消息处理；try/catch 防止配额满/隐私模式导致异常影响 store 状态
function persistCronUnread(state: Record<string, boolean>) {
  queueMicrotask(() => {
    try { localStorage.setItem('jiuwenswarm_cron_unread', JSON.stringify(state)); } catch { /* ignore */ }
  });
}

async function fetchWebCronJobs(): Promise<SidebarCronJob[]> {
  const payload = await webRequest<{ jobs: SidebarCronJob[] }>('cron.job.list');
  return (payload.jobs || []).filter((job) => isWebChannelJob(job.targets));
}

export const useCronStore = create<CronState>((set, get) => ({
  jobs: [],
  isLoading: false,
  runSyncFingerprints: {},
  expandedCronGroups: {},
  cronSessions: {},
  cronSessionsLoading: {},
  lastRunSessionId: {},
  projectCronSessions: {},
  projectCronSessionsLoading: {},
  setLastRunSessionId: (jobId, sessionId) =>
    set((s) => ({ lastRunSessionId: { ...s.lastRunSessionId, [jobId]: sessionId } })),
  pendingActiveHistoryRefresh: null,
  requestActiveHistoryRefresh: (sessionId) => set({ pendingActiveHistoryRefresh: sessionId }),
  consumeActiveHistoryRefresh: () => {
    const pending = get().pendingActiveHistoryRefresh;
    if (pending) set({ pendingActiveHistoryRefresh: null });
    return pending;
  },
  unreadCronJobs: (() => {
    try {
      const value = JSON.parse(localStorage.getItem('jiuwenswarm_cron_unread') || '{}');
      return typeof value === 'object' && value !== null ? value as Record<string, boolean> : {};
    } catch {
      return {};
    }
  })(),
  markCronJobUnread: (jobId) => {
    set((s) => {
      if (s.unreadCronJobs[jobId]) return s;
      const next = { ...s.unreadCronJobs, [jobId]: true };
      persistCronUnread(next);
      return { unreadCronJobs: next };
    });
  },
  clearCronJobUnread: (jobId) => {
    set((s) => {
      if (!s.unreadCronJobs[jobId]) return s;
      const next = { ...s.unreadCronJobs };
      delete next[jobId];
      persistCronUnread(next);
      return { unreadCronJobs: next };
    });
  },

  loadJobs: async () => {
    set({ isLoading: true });
    try {
      const webJobs = await fetchWebCronJobs();
      set({
        jobs: webJobs,
        isLoading: false,
        runSyncFingerprints: buildCronJobFingerprintMap(webJobs),
      });
    } catch {
      set({ jobs: [], isLoading: false, runSyncFingerprints: {} });
    }
  },

  syncJobRuns: async () => {
    try {
      const webJobs = await fetchWebCronJobs();
      const previous = get().runSyncFingerprints;
      const { updatedJobIds, nextFingerprints } = detectCronJobRunUpdates(previous, webJobs);

      set({
        jobs: webJobs,
        runSyncFingerprints: nextFingerprints,
      });

      const activeSessionId = useChatStore.getState().activeSessionId;

      for (const jobId of updatedJobIds) {
        const job = webJobs.find((item) => item.id === jobId);
        const lastSid = (job?.last_session_id ?? '').trim();
        // 当前正打开的会话恰好是这次新产生的 cron 会话：企业版 HTTP 无 push，
        // 立即执行跳转后 skipHistoryLoad，消息两头落空。这里用轮询兜底——
        // 投递一次历史刷新请求，并直接清掉蓝点（消息即将实打实显示出来）。
        if (lastSid && activeSessionId && lastSid === activeSessionId) {
          get().clearCronJobUnread(jobId);
          get().requestActiveHistoryRefresh(lastSid);
          if (job) {
            void get().loadCronSessions(job.project_id || 'default', jobId);
          }
          continue;
        }
        get().markCronJobUnread(jobId);
        if (job) {
          void get().loadCronSessions(job.project_id || 'default', jobId);
        }
      }
    } catch {
      // Pull sync is best-effort; avoid surfacing transient HTTP errors in the sidebar.
    }
  },

  reload: async () => {
    await get().loadJobs();
  },

  toggleCronGroup: (groupId: string) => {
    set((state) => ({
      expandedCronGroups: {
        ...state.expandedCronGroups,
        [groupId]: !state.expandedCronGroups[groupId],
      },
    }));
  },

  isCronGroupExpanded: (groupId: string) => {
    return get().expandedCronGroups[groupId] ?? false;
  },

  loadCronSessions: async (projectId: string, cronId: string) => {
    set((state) => ({
      cronSessionsLoading: { ...state.cronSessionsLoading, [cronId]: true },
    }));
    try {
      const payload = await projectRegistryClient.getCronSessions(projectId, cronId);
      set((state) => ({
        cronSessions: {
          ...state.cronSessions,
          [cronId]: payload.sessions || [],
        },
        cronSessionsLoading: { ...state.cronSessionsLoading, [cronId]: false },
      }));
    } catch {
      set((state) => ({
        cronSessionsLoading: { ...state.cronSessionsLoading, [cronId]: false },
      }));
    }
  },

  loadProjectCronSessions: async (projectId: string) => {
    set((state) => ({
      projectCronSessionsLoading: { ...state.projectCronSessionsLoading, [projectId]: true },
    }));
    try {
      // 不带 cron_id：拉该 project 下全部 cron 会话（cron_id 非空），供会话组聚合。
      const payload = await projectRegistryClient.getCronSessions(projectId);
      set((state) => ({
        projectCronSessions: {
          ...state.projectCronSessions,
          [projectId]: payload.sessions || [],
        },
        projectCronSessionsLoading: { ...state.projectCronSessionsLoading, [projectId]: false },
      }));
    } catch {
      set((state) => ({
        projectCronSessionsLoading: { ...state.projectCronSessionsLoading, [projectId]: false },
      }));
    }
  },

}));

const DEFAULT_PROJECT_ID = 'default';

// 系统自动维护的 cron job id（与后端 proactive_cron_sync.PROACTIVE_JOB_ID、
// CronPanel/index.tsx 的 PROACTIVE_AUTO_JOB_ID 一致）。这类 job 由配置开关自动创建/删除，
// 不创建会话、不给推送的会话打 cron_id——所以"触发的会话"列表恒空，在会话侧栏是个空壳。
// 从会话侧栏隐藏它（Cron 面板里仍可见可编辑 cron 表达式/时区），避免空壳 item 碍眼。
const SYSTEM_AUTO_JOB_IDS = new Set(['proactive-tick-auto']);

export function isDefaultProjectId(projectId: string): boolean {
  return !projectId || projectId === DEFAULT_PROJECT_ID;
}

/** 按项目过滤定时任务（默认项目返回 project_id 为空的）；系统自动维护 job 不进会话侧栏。 */
export function filterJobsForProject(jobs: SidebarCronJob[], projectId: string): SidebarCronJob[] {
  const filtered = isDefaultProjectId(projectId)
    ? jobs.filter((job) => isDefaultProjectId(job.project_id) && !SYSTEM_AUTO_JOB_IDS.has(job.id))
    : jobs.filter((job) => job.project_id === projectId && !SYSTEM_AUTO_JOB_IDS.has(job.id));
  return filtered.sort((a, b) => {
    const au = typeof a.updated_at === 'number' ? a.updated_at : 0;
    const bu = typeof b.updated_at === 'number' ? b.updated_at : 0;
    return bu - au;
  });
}

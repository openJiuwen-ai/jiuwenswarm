import { create } from 'zustand';
import {
  buildCronJobFingerprintMap,
  detectCronJobRunUpdates,
  resolveOpenCronHistoryRefresh,
  type CronJobRunFingerprint,
} from '../features/cron/cronJobSync';
import { settleCronSessionRuntime } from '../features/cron/cronLiveWatch';
import { projectRegistryClient } from '../features/workspace/projectRegistryClient';
import { webRequest } from '../services/webClient';
import type { Session } from '../types';
import { useChatStore } from './chatStore';
import { useSessionStore } from './sessionStore';

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
  // 企业版 HTTP 无 cron 结果 push。30s 轮询发现任务跑完后，若当前窗口属于这次任务
  // （含连续执行时已被下一次盖住的上一个 cron_*），投递一次历史刷新。
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

function cronIdOfSession(sessionId: string): string {
  const sid = sessionId.trim();
  if (!sid) {
    return '';
  }
  const sessionState = useSessionStore.getState();
  const current = sessionState.currentSession;
  if (current?.session_id === sid) {
    return (current.cron_id || '').trim();
  }
  const listed = sessionState.sessions.find((item) => item.session_id === sid);
  return (listed?.cron_id || '').trim();
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
      // CronPanel 约 5s 静默刷新会走这里；比 30s syncJobRuns 更快收口漏掉的 processing。
      const activeSid = (useChatStore.getState().activeSessionId || '').trim();
      for (const job of webJobs) {
        const lastSid = (job.last_session_id ?? '').trim();
        if (!lastSid) continue;
        if (settleCronSessionRuntime(lastSid) && lastSid === activeSid) {
          get().requestActiveHistoryRefresh(lastSid);
        }
      }
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

      const activeSessionId = (useChatStore.getState().activeSessionId || '').trim();
      const projectIdsToRefresh = new Set<string>();

      // last_session_id 在 agent stream 返回后回写 = 该趟已结束/进审批。
      // 连点立即执行切走时 live 可能漏收尾；这里收口仍标 processing 的 UI
      // （pendingQuestion 时 settle 内部会跳过）。
      for (const job of webJobs) {
        const lastSid = (job.last_session_id ?? '').trim();
        if (!lastSid) continue;
        if (settleCronSessionRuntime(lastSid) && lastSid === activeSessionId) {
          get().requestActiveHistoryRefresh(lastSid);
        }
      }

      for (const jobId of updatedJobIds) {
        const job = webJobs.find((item) => item.id === jobId);
        const lastSid = (job?.last_session_id ?? '').trim();
        const projectId = job?.project_id || 'default';
        const prevLastSid = (previous[jobId]?.lastSessionId || '').trim();
        const rememberedSid = (get().lastRunSessionId[jobId] || '').trim();
        // 上一趟会话也可能卡在 processing（指纹已从旧 sid 跳到新 sid）。
        if (prevLastSid && prevLastSid !== lastSid) {
          settleCronSessionRuntime(prevLastSid);
        }
        if (rememberedSid && rememberedSid !== lastSid) {
          settleCronSessionRuntime(rememberedSid);
        }
        // 侧栏会话组读 projectCronSessions。本函数只挂在企业版 HTTP 的 30s 轮询上，
        // 个人版 WS 推送不走这里。
        projectIdsToRefresh.add(projectId);
        // 正看着的 cron_* 属于这个任务时再读一次历史。最新会话被下一次执行盖住时，
        // 仍刷新当前窗口，避免必须手动刷新才看得到已落盘的回复。
        const refreshSid = resolveOpenCronHistoryRefresh({
          jobId,
          lastSessionId: lastSid,
          activeSessionId,
          activeCronId: cronIdOfSession(activeSessionId),
          rememberedRunSessionId: rememberedSid,
        });
        const viewingLatest = Boolean(lastSid && activeSessionId && lastSid === activeSessionId);
        if (viewingLatest) {
          get().clearCronJobUnread(jobId);
        } else {
          get().markCronJobUnread(jobId);
        }
        // cron 进行中 restore 会 keep-live，可安全补任务内容 user 消息。
        if (refreshSid) {
          get().requestActiveHistoryRefresh(refreshSid);
        }
        if (job) {
          void get().loadCronSessions(projectId, jobId);
        }
      }

      for (const projectId of projectIdsToRefresh) {
        void get().loadProjectCronSessions(projectId);
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

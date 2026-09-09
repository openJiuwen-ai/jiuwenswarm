import { create } from 'zustand';
import i18n from '../i18n';
import { projectRegistryClient } from '../features/workspace/projectRegistryClient';
import { persistWorkMode, readStoredWorkMode } from '../features/workspace/workModeStorage';
import type { ProjectInfo, Session, WorkMode } from '../types';
import { useChatStore } from './chatStore';
import { useSessionStore } from './sessionStore';

export const PROJECT_SESSION_PAGE_SIZE = 10;
const DEFAULT_PROJECT_ID = 'default';
const DEFAULT_CODE_PROJECT_ID = 'default_code';

function normalizeProject(project: ProjectInfo, fallbackWorkMode: WorkMode): ProjectInfo {
  return {
    ...project,
    work_mode: project.work_mode === 'code' || project.work_mode === 'work'
      ? project.work_mode
      : fallbackWorkMode,
    git: project.git ?? {
      enabled: false,
      repo_root: '',
      initialized_by_jiuwenswarm: false,
      detected_at: 0,
      status: fallbackWorkMode === 'code' ? 'unknown' : 'disabled',
      branch: '',
      error: '',
      is_dirty: false,
    },
  };
}

interface UpsertSessionOptions {
  isNew?: boolean;
}

interface WorkspaceState {
  workMode: WorkMode;
  projects: ProjectInfo[];
  projectSessions: Record<string, Session[]>;
  projectSessionTotals: Record<string, number>;
  sessionVisibility: Record<string, { visibleCount: number }>;
  pinnedSessions: Session[];
  selectedProject: ProjectInfo | null;
  expandedProjectIds: Record<string, boolean>;
  isLoadingProjects: boolean;
  error: string | null;
  setWorkMode: (workMode: WorkMode) => Promise<void>;
  loadProjects: () => Promise<void>;
  loadProjectSessions: (projectId: string, limit?: number) => Promise<void>;
  showMoreSessions: (projectId: string) => Promise<void>;
  collapseSessions: (projectId: string) => Promise<void>;
  loadPinnedSessions: () => Promise<void>;
  setSelectedProject: (project: ProjectInfo | null) => void;
  toggleProjectExpanded: (projectId: string) => void;
  createProject: (name: string, projectDir: string) => Promise<ProjectInfo>;
  renameProject: (projectId: string, name: string) => Promise<void>;
  pinProject: (projectId: string, pinned: boolean) => Promise<void>;
  removeProject: (projectId: string) => Promise<void>;
  upsertSession: (session: Session, options?: UpsertSessionOptions) => void;
  pinSession: (sessionId: string, pinned: boolean) => Promise<void>;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  patchSession: (sessionId: string, patch: Partial<Session>) => void;
  refreshSessionWorkspace: (session: Pick<Session, 'project_id' | 'pinned'> | null | undefined) => Promise<void>;
}

function findProject(projects: ProjectInfo[], projectId: string): ProjectInfo | null {
  return projects.find((project) => project.project_id === projectId) ?? null;
}

function isDefaultProject(project: ProjectInfo): boolean {
  return project.is_default
    || project.project_id === DEFAULT_PROJECT_ID
    || project.project_id === DEFAULT_CODE_PROJECT_ID;
}

// 默认项目节点由后端按固定中文文案实时生成、不入库也不可重命名，
// 因此按当前 UI 语言在展示层替换即可，不存在覆盖用户自定义名称的风险。
export function getProjectDisplayName(project: ProjectInfo): string {
  return isDefaultProject(project) ? i18n.t('multiSession.project.defaultProjectName') : project.name;
}

function findDefaultProjectId(projects: ProjectInfo[]): string {
  return projects.find(isDefaultProject)?.project_id ?? DEFAULT_PROJECT_ID;
}

function findProjectIdForSession(projects: ProjectInfo[], session: Pick<Session, 'project_id'>): string {
  const projectId = session.project_id?.trim();
  if (!projectId || projectId === DEFAULT_PROJECT_ID) {
    return findDefaultProjectId(projects);
  }
  if (projects.length === 0) {
    return projectId;
  }
  const project = findProject(projects, projectId);
  return project && !project.hidden ? project.project_id : findDefaultProjectId(projects);
}

function patchSessionLists(
  lists: Record<string, Session[]>,
  sessionId: string,
  patch: Partial<Session>,
  options: { removeFromProjectLists?: boolean } = {},
): Record<string, Session[]> {
  let changed = false;
  const next: Record<string, Session[]> = {};
  for (const [projectId, sessions] of Object.entries(lists)) {
    const patched = sessions
      .map((session) => {
        if (session.session_id !== sessionId) return session;
        changed = true;
        return { ...session, ...patch };
      })
      .filter((session) => !(options.removeFromProjectLists && session.session_id === sessionId));
    next[projectId] = patched;
  }
  return changed ? next : lists;
}

function getVisibleCount(state: WorkspaceState, projectId: string): number {
  return state.sessionVisibility[projectId]?.visibleCount ?? PROJECT_SESSION_PAGE_SIZE;
}

function normalizeActivityTime(value: number | undefined): number | null {
  if (typeof value !== 'number') return null;
  return value < 1e11 ? value * 1000 : value;
}

function getSessionActivityAt(session: Pick<Session, 'last_user_message_at' | 'last_message_at' | 'updated_at' | 'created_at'>): number {
  const numericActivity = normalizeActivityTime(session.last_user_message_at)
    ?? normalizeActivityTime(session.last_message_at);
  if (numericActivity !== null) return numericActivity;
  const updatedAt = Date.parse(session.updated_at);
  if (!Number.isNaN(updatedAt)) return updatedAt;
  const createdAt = Date.parse(session.created_at);
  return Number.isNaN(createdAt) ? 0 : createdAt;
}

function upsertSessionByActivity(sessions: Session[], nextSession: Session): Session[] {
  const withoutExisting = sessions.filter((session) => session.session_id !== nextSession.session_id);
  const next = [nextSession, ...withoutExisting];
  return next.sort((left, right) => getSessionActivityAt(right) - getSessionActivityAt(left));
}

function getSessionTitle(session: Pick<Session, 'display_title' | 'title'> | undefined): string {
  return session?.display_title?.trim() || session?.title?.trim() || '';
}

function getLocalSessionsById(): Map<string, Session> {
  const sessionState = useSessionStore.getState();
  const sessions = new Map(sessionState.sessions.map((session) => [session.session_id, session]));
  if (sessionState.currentSession) {
    sessions.set(sessionState.currentSession.session_id, sessionState.currentSession);
  }
  return sessions;
}

function mergeLocalTitle(serverSession: Session, localSession: Session | undefined): Session {
  if (getSessionTitle(serverSession) || !localSession) return serverSession;
  const localTitle = getSessionTitle(localSession);
  if (!localTitle) return serverSession;
  return {
    ...serverSession,
    title: localSession.title || localTitle,
    display_title: localSession.display_title || localTitle,
  };
}

function mergeLocalSessionTitles(serverSessions: Session[]): Session[] {
  const localSessions = getLocalSessionsById();
  return serverSessions.map((session) => mergeLocalTitle(session, localSessions.get(session.session_id)));
}

function findWorkspaceSession(state: WorkspaceState, sessionId: string): Session | undefined {
  const pinnedSession = state.pinnedSessions.find((session) => session.session_id === sessionId);
  if (pinnedSession) return pinnedSession;
  for (const sessions of Object.values(state.projectSessions)) {
    const projectSession = sessions.find((session) => session.session_id === sessionId);
    if (projectSession) return projectSession;
  }
  const sessionState = useSessionStore.getState();
  if (sessionState.currentSession?.session_id === sessionId) return sessionState.currentSession;
  return sessionState.sessions.find((session) => session.session_id === sessionId);
}

function mergeSessionProjectContext(session: Session, previousSession: Session | undefined): Session {
  if (!previousSession) return session;
  return {
    ...session,
    project_id: session.project_id || previousSession.project_id || '',
    project_dir: session.project_dir || previousSession.project_dir || '',
  };
}

function mergeVisibleSessionTitles(serverSessions: Session[], visibleSessions: Session[]): Session[] {
  const localSessions = getLocalSessionsById();
  const visibleSessionsById = new Map(visibleSessions.map((session) => [session.session_id, session]));
  return serverSessions.map((session) => mergeLocalTitle(
    session,
    localSessions.get(session.session_id) ?? visibleSessionsById.get(session.session_id),
  ));
}

function shouldKeepPendingLocalSession(session: Session): boolean {
  if (session.pinned) return false;
  if (!getSessionTitle(session)) return false;
  const runtime = useChatStore.getState().getRuntime(session.session_id);
  return session.is_processing === true
    || runtime?.isProcessing === true
    || runtime?.isPaused === true;
}

function reconcileVisibleProjectSessions(serverSessions: Session[], visibleSessions: Session[]): Session[] {
  const mergedServerSessions = mergeVisibleSessionTitles(serverSessions, visibleSessions);
  const serverSessionIds = new Set(serverSessions.map((session) => session.session_id));
  const pendingLocalSessions = visibleSessions.filter(
    (session) => !serverSessionIds.has(session.session_id) && shouldKeepPendingLocalSession(session),
  );
  return [...pendingLocalSessions, ...mergedServerSessions];
}

function getReconciledTotal(serverTotal: number | undefined, sessions: Session[]): number {
  const normalizedServerTotal = typeof serverTotal === 'number' && Number.isFinite(serverTotal)
    ? serverTotal
    : 0;
  return Math.max(normalizedServerTotal, sessions.length);
}

function getProjectSessionListsAfterPin(
  projectSessions: Record<string, Session[]>,
  projectId: string,
  session: Session,
  pinned: boolean,
): Record<string, Session[]> {
  if (pinned) {
    return projectSessions;
  }
  return {
    ...projectSessions,
    [projectId]: upsertSessionByActivity(projectSessions[projectId] || [], session),
  };
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workMode: readStoredWorkMode(),
  projects: [],
  projectSessions: {},
  projectSessionTotals: {},
  sessionVisibility: {},
  pinnedSessions: [],
  selectedProject: null,
  expandedProjectIds: {},
  isLoadingProjects: false,
  error: null,

  setWorkMode: async (workMode) => {
    if (get().workMode === workMode) return;
    persistWorkMode(workMode);
    set({
      workMode,
      projects: [],
      projectSessions: {},
      projectSessionTotals: {},
      sessionVisibility: {},
      pinnedSessions: [],
      selectedProject: null,
      expandedProjectIds: {},
      error: null,
    });
    await get().loadProjects();
  },

  loadProjects: async () => {
    const requestedWorkMode = get().workMode;
    set({ isLoadingProjects: true, error: null });
    try {
      const payload = await projectRegistryClient.list('all', requestedWorkMode);
      if (get().workMode !== requestedWorkMode) return;
      const projects = (payload.projects || []).map((project) => (
        normalizeProject(project, requestedWorkMode)
      ));
      set((state) => {
        // 项目会话需要经过 Gateway → AgentServer，并且后端每次查询都会扫描
        // 当前用户的会话元数据。首屏若把所有历史项目默认展开，会同时触发 N 次
        // 全量扫描；仅默认项目初始展开，其余项目在用户展开时再加载。
        const expandedProjectIds = { ...state.expandedProjectIds };
        for (const project of projects) {
          if (expandedProjectIds[project.project_id] === undefined) {
            expandedProjectIds[project.project_id] = isDefaultProject(project);
          }
        }
        return {
          projects,
          expandedProjectIds,
          selectedProject: state.selectedProject
            ? findProject(projects, state.selectedProject.project_id)
            : null,
          isLoadingProjects: false,
        };
      });
      await get().loadPinnedSessions();
    } catch (error) {
      set({ isLoadingProjects: false, error: error instanceof Error ? error.message : String(error) });
    }
  },

  loadProjectSessions: async (projectId, limit) => {
    const requestedLimit = limit ?? getVisibleCount(get(), projectId);
    try {
      const payload = await projectRegistryClient.getSessions(projectId, requestedLimit);
      const sessions = reconcileVisibleProjectSessions(
        payload.sessions || [],
        get().projectSessions[projectId] || [],
      );
      const total = getReconciledTotal(payload.total, sessions);
      set((state) => ({
        projectSessions: {
          ...state.projectSessions,
          [projectId]: sessions,
        },
        projectSessionTotals: {
          ...state.projectSessionTotals,
          [projectId]: total,
        },
        sessionVisibility: {
          ...state.sessionVisibility,
          [projectId]: { visibleCount: requestedLimit },
        },
      }));
    } catch (error) {
      console.error('Failed to load project sessions', error);
      set({ error: error instanceof Error ? error.message : String(error) });
    }
  },

  showMoreSessions: async (projectId) => {
    const state = get();
    const currentVisibleCount = getVisibleCount(state, projectId);
    const nextVisibleCount = currentVisibleCount + PROJECT_SESSION_PAGE_SIZE;
    await get().loadProjectSessions(projectId, nextVisibleCount);
  },

  collapseSessions: async (projectId) => {
    await get().loadProjectSessions(projectId, PROJECT_SESSION_PAGE_SIZE);
  },

  loadPinnedSessions: async () => {
    const payload = await projectRegistryClient.pinnedSessions();
    const { workMode, projects } = get();
    const visibleProjectIds = new Set(projects.map((project) => project.project_id));
    set({
      pinnedSessions: mergeLocalSessionTitles(payload.sessions || [])
        .filter((session) => {
          if (session.work_mode) return session.work_mode === workMode;
          if (session.project_id) return visibleProjectIds.has(session.project_id);
          return workMode === 'work';
        }),
    });
  },

  setSelectedProject: (project) => set({ selectedProject: project }),
  toggleProjectExpanded: (projectId) => set((state) => ({
    expandedProjectIds: {
      ...state.expandedProjectIds,
      [projectId]: !state.expandedProjectIds[projectId],
    },
  })),

  createProject: async (name, projectDir) => {
    const { project_id: projectId } = await projectRegistryClient.create(
      name,
      projectDir,
      get().workMode,
    );
    await get().loadProjects();
    const project = findProject(get().projects, projectId);
    if (!project) throw new Error('project.create returned a project that is missing from project.list');
    set((state) => ({
      selectedProject: project,
      expandedProjectIds: { ...state.expandedProjectIds, [project.project_id]: true },
    }));
    return project;
  },

  renameProject: async (projectId, name) => {
    await projectRegistryClient.rename(projectId, name);
    await get().loadProjects();
  },

  pinProject: async (projectId, pinned) => {
    await projectRegistryClient.pin(projectId, pinned);
    await get().loadProjects();
  },

  removeProject: async (projectId) => {
    await projectRegistryClient.remove(projectId);
    await get().loadProjects();
    const state = get();
    await Promise.all(state.projects
      .filter((project) => isDefaultProject(project) || Boolean(state.expandedProjectIds[project.project_id]))
      .map((project) => state.loadProjectSessions(project.project_id)));
  },

  upsertSession: (session, options = {}) => {
    const state = get();
    const isCronSession = Boolean(session.cron_id?.trim());
    set((current) => {
      // cron 会话由 cronStore 在对应定时任务节点展示。WebSocket / metadata
      // 的增量更新不能把它临时插入项目普通会话列表，否则在下一次全量刷新前
      // 会同时出现在两个位置。已置顶的会话沿用现有规则，展示在置顶区。
      if (isCronSession && !session.pinned) {
        return {
          projectSessions: patchSessionLists(
            current.projectSessions,
            session.session_id,
            {},
            { removeFromProjectLists: true },
          ),
          pinnedSessions: current.pinnedSessions.filter(
            (item) => item.session_id !== session.session_id,
          ),
        };
      }
      const projectId = findProjectIdForSession(state.projects, session);
      if (!projectId) return current;
      const currentProjectSessions = current.projectSessions[projectId] || [];
      const sessionWasVisible = currentProjectSessions.some((item) => item.session_id === session.session_id);
      const pinnedSessions = session.pinned
        ? upsertSessionByActivity(current.pinnedSessions, session)
        : current.pinnedSessions.filter((item) => item.session_id !== session.session_id);
      const visibleCount = getVisibleCount(current, projectId);
      const projectSessions = session.pinned
        ? currentProjectSessions.filter((item) => item.session_id !== session.session_id)
        : upsertSessionByActivity(currentProjectSessions, session).slice(0, visibleCount);
      const currentTotal = current.projectSessionTotals[projectId] ?? currentProjectSessions.length;
      const nextTotal = options.isNew && !sessionWasVisible
        ? currentTotal + 1
        : currentTotal;

      return {
        pinnedSessions,
        projectSessions: {
          ...current.projectSessions,
          [projectId]: projectSessions,
        },
        projectSessionTotals: {
          ...current.projectSessionTotals,
          [projectId]: nextTotal,
        },
      };
    });
  },

  pinSession: async (sessionId, pinned) => {
    const sessionState = useSessionStore.getState();
    const previousSession = findWorkspaceSession(get(), sessionId);
    const result = await projectRegistryClient.pinSession(sessionId, pinned);
    const latestSession = await projectRegistryClient.getSessionMetadata(sessionId);
    const patch = {
      ...mergeSessionProjectContext(latestSession, previousSession),
      pinned: result.pinned,
      pin_order: result.pin_order,
    };
    const projectId = findProjectIdForSession(get().projects, patch);
    sessionState.updateSession(sessionId, patch);
    set((state) => ({
      projectSessions: patchSessionLists(
        getProjectSessionListsAfterPin(state.projectSessions, projectId, patch, pinned),
        sessionId,
        patch,
        { removeFromProjectLists: pinned },
      ),
      pinnedSessions: pinned
        ? state.pinnedSessions
        : state.pinnedSessions.filter((item) => item.session_id !== sessionId),
    }));
    await get().loadPinnedSessions();
    await get().loadProjectSessions(projectId);
  },

  renameSession: async (sessionId, title) => {
    const result = await projectRegistryClient.renameSession(sessionId, title);
    const renamedAt = new Date().toISOString();
    const patch: Partial<Session> = {
      title: result.title,
      display_title: result.title,
      is_custom_title: true,
      title_source: 'user',
      renamed_at: renamedAt,
    };
    useSessionStore.getState().updateSession(sessionId, patch);
    set((state) => ({
      projectSessions: patchSessionLists(state.projectSessions, sessionId, patch),
      pinnedSessions: state.pinnedSessions.map((session) => (
        session.session_id === sessionId ? { ...session, ...patch } : session
      )),
    }));
    await get().loadPinnedSessions();
    const sessionState = useSessionStore.getState();
    const session =
      sessionState.currentSession?.session_id === sessionId
        ? sessionState.currentSession
        : sessionState.sessions.find((item) => item.session_id === sessionId);
    const projectId = session ? findProjectIdForSession(get().projects, session) : null;
    if (projectId) await get().loadProjectSessions(projectId);
  },

  patchSession: (sessionId, patch) => {
    set((state) => ({
      projectSessions: patchSessionLists(state.projectSessions, sessionId, patch),
      pinnedSessions: state.pinnedSessions.map((session) => (
        session.session_id === sessionId ? { ...session, ...patch } : session
      )),
    }));
  },

  refreshSessionWorkspace: async (session) => {
    await get().loadProjects();
    const projectId = session ? findProjectIdForSession(get().projects, session) : null;
    if (projectId) {
      await get().loadProjectSessions(projectId);
    }
  },
}));

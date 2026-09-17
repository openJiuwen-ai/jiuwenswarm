import { webRequest } from '../../services/webClient';
import type { Session } from '../../types';
import type { ProjectInfo, WorkMode } from './projectTypes';

export const projectRegistryClient = {
  list: (filter: 'all' | 'pinned' | 'unpinned' = 'all', workMode?: WorkMode) =>
    webRequest<{ projects: ProjectInfo[] }>('project.list', {
      filter,
      ...(workMode ? { work_mode: workMode } : {}),
    }),
  getSessions: (projectId: string, limit?: number) =>
    webRequest<{ sessions: Session[]; total: number }>('project.get_sessions', {
      project_id: projectId,
      ...(limit !== undefined ? { limit } : {}),
    }),
  getCronSessions: (projectId: string, cronId?: string) =>
    webRequest<{ sessions: Session[]; total: number }>('project.get_cron_sessions', {
      project_id: projectId,
      ...(cronId ? { cron_id: cronId } : {}),
    }),
  create: (name: string, projectDir: string, workMode: WorkMode) =>
    webRequest<{
      project_id: string;
      project_dir: string;
      restored: boolean;
      work_mode: WorkMode;
      git: ProjectInfo['git'];
      project: ProjectInfo;
    }>('project.create', { name, project_dir: projectDir, work_mode: workMode }),
  rename: (projectId: string, name: string) => webRequest<Record<string, never>>('project.rename', { project_id: projectId, name }),
  pin: (projectId: string, pinned: boolean) => webRequest<{ pinned: boolean; pin_order: number }>('project.pin', { project_id: projectId, pinned }),
  remove: (projectId: string) => webRequest<{ affected_sessions: number }>('project.remove', { project_id: projectId }),
  pinnedSessions: () => webRequest<{ sessions: Session[] }>('project.pinned_sessions'),
  getSessionMetadata: (sessionId: string) => webRequest<Session>('session.get_metadata', { session_id: sessionId }),
  pinSession: (sessionId: string, pinned: boolean) => webRequest<{ pinned: boolean; pin_order: number }>('session.pin', { session_id: sessionId, pinned }),
  renameSession: (sessionId: string, title: string) => webRequest<{ session_id: string; title: string }>('session.rename', { session_id: sessionId, title }),
};

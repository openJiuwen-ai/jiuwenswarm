import { createElement, useRef, useState, type ReactNode } from 'react';
import { flushSync } from 'react-dom';
import { Archive } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from '../../components/ui';
import {
  getArchiveErrorCode,
  archivedTaskClient,
  findBatchSessionResult,
} from '../../features/workspace/archivedTaskClient';
import { projectRegistryClient } from '../../features/workspace/projectRegistryClient';
import { requestSettingsModule } from '../../features/settings/settingsNavigation';
import {
  useChatStore,
  useCronStore,
  useGoalStore,
  useHarnessStore,
  useSessionStore,
  useSubagentStore,
  useTodoStore,
  useWorkspaceStore,
} from '../../stores';
import type { ProjectInfo, Session } from '../../types';
import { toDisplaySessionTitle } from '../../utils/documentMessage';
import { forgetCreatedConversation } from '../state/newConversationLifecycle';
import { DeleteDialog } from '../dialogs/Dialogs';
import { ProjectArchiveDialog, resolveProjectArchiveSessionCount } from './ProjectArchiveDialog';
import type { SidebarMenuItem } from './SidebarMenu.types';
import { getProjectMenuItems, getSessionMenuItems } from './sidebarMenuSchema';
import DeleteIcon from '../../assets/work-mode/delete.svg?react';
import EditIcon from '../../assets/work-mode/edit.svg?react';
import FolderIcon from '../../assets/work-mode/folder.svg?react';
import PinIcon from '../../assets/work-mode/pin.svg?react';
import UnpinIcon from '../../assets/work-mode/unpin.svg?react';

export type SidebarMenuOptions =
  | {
      type: 'session';
      session: Session;
      /** 项目内会话用不同置顶文案；cron 会话会自动禁用归档 */
      scope?: 'project' | 'conversation';
      activeSessionId: string | null;
      onLeaveActiveSession: () => void;
    }
  | {
      type: 'project';
      project: ProjectInfo;
      archiveSessionsDisabled?: boolean;
    };

type RenameTarget =
  | { kind: 'project'; id: string; value: string }
  | { kind: 'session'; id: string; value: string };

type Translate = (key: string, options?: Record<string, unknown>) => string;

function isDefaultProject(project: ProjectInfo): boolean {
  return project.is_default || project.project_id === 'default' || project.project_id === 'default_code';
}

function isCronSession(session: Session): boolean {
  return Boolean(session.cron_id) || session.session_id.startsWith('cron_');
}

function getSessionTitle(session: Session, fallback: string): string {
  const raw = session.display_title?.trim() || session.title?.trim() || fallback;
  return toDisplaySessionTitle(raw) || fallback;
}

function archiveErrorKey(error: unknown): string {
  const code = getArchiveErrorCode(error);
  if (code === 'SESSION_BUSY') return 'multiSession.project.errors.archiveSessionBusy';
  if (code === 'FORBIDDEN') return 'multiSession.project.errors.archiveForbidden';
  if (code === 'NOT_FOUND') return 'multiSession.project.errors.archiveNotFound';
  return 'multiSession.project.errors.archiveFailed';
}

function refreshExpandedCronSessions() {
  const cronStore = useCronStore.getState();
  for (const [groupId, isOpen] of Object.entries(cronStore.expandedCronGroups)) {
    if (!isOpen) continue;
    const cronId = groupId.startsWith('cron-') ? groupId.slice(5) : groupId;
    const job = cronStore.jobs.find((j) => j.id === cronId);
    if (!job) continue;
    void cronStore.loadCronSessions(job.project_id || 'default', cronId);
  }
}

/** 会话置顶/取消置顶（菜单项与行内置顶按钮共用） */
export async function toggleSessionPin(session: Session, translate: Translate): Promise<void> {
  try {
    await useWorkspaceStore.getState().pinSession(session.session_id, !session.pinned);
  } catch (error) {
    toast.open({
      content: `${translate('multiSession.project.pinFailed')}: ${error instanceof Error ? error.message : String(error)}`,
      variant: 'error',
      wide: true,
    });
  } finally {
    refreshExpandedCronSessions();
  }
}

function openArchiveFailureToast(content: string) {
  toast.open({
    content,
    variant: 'error',
    duration: 5,
    wide: true,
  });
}

function projectActionErrorText(error: unknown, translate: Translate): string {
  const code = getArchiveErrorCode(error);
  if (code === 'PROJECT_NAME_CONFLICT') {
    return translate('multiSession.project.errors.projectNameConflict');
  }
  if (code === 'CRON_STOP_FAILED') {
    return translate('multiSession.project.errors.cronStopFailed');
  }
  return error instanceof Error ? error.message : String(error);
}

function openArchiveSuccessToast(
  translate: Translate,
  options: { content: string; onUndo: () => Promise<void>; replaceExisting?: boolean },
) {
  // 单会话归档替换旧提示；批量归档可能与失败 toast 并存
  if (options.replaceExisting !== false) {
    toast.closeAll();
  }
  toast.open({
    content: options.content,
    icon: createElement(Archive, { 'aria-hidden': true, size: 16, strokeWidth: 1.8 }),
    duration: 5,
    wide: true,
    actions: [
      {
        label: translate('multiSession.project.archiveView'),
        onClick: () => requestSettingsModule('archivedTasks'),
      },
      {
        label: translate('multiSession.project.archiveUndo'),
        onClick: () => {
          void (async () => {
            try {
              await options.onUndo();
            } catch (error) {
              openArchiveFailureToast(translate(archiveErrorKey(error)));
            }
          })();
        },
      },
    ],
  });
}

function RenameDialog({
  title,
  initialValue,
  placeholder,
  error,
  onCancel,
  onSubmit,
}: {
  title: string;
  initialValue: string;
  placeholder: string;
  error?: string | null;
  onCancel: () => void;
  onSubmit: (value: string) => void;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState(initialValue);

  return (
    <div className="conversation-path-dialog-backdrop" role="presentation" data-testid="multi-session-path-dialog-backdrop">
      <form
        className="conversation-path-dialog"
        data-testid="multi-session-path-dialog"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = value.trim();
          if (trimmed) onSubmit(trimmed);
        }}
      >
        <div className="conversation-path-dialog__title" data-testid="multi-session-path-dialog-title">{title}</div>
        <input
          className="conversation-path-dialog__input"
          data-testid="multi-session-path-dialog-input"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={placeholder}
          maxLength={200}
          autoFocus
        />
        {error ? <div className="conversation-path-dialog__error" data-testid="multi-session-path-dialog-error">{error}</div> : null}
        <div className="conversation-path-dialog__actions" data-testid="multi-session-path-dialog-actions">
          <button type="button" onClick={onCancel} data-testid="multi-session-path-dialog-cancel">{t('multiSession.project.cancel')}</button>
          <button type="submit" disabled={!value.trim()} data-testid="multi-session-path-dialog-confirm">{t('multiSession.project.confirm')}</button>
        </div>
      </form>
    </div>
  );
}

function ProjectRemoveDialog({
  project,
  error,
  notice,
  deleting,
  onCancel,
  onDelete,
}: {
  project: ProjectInfo;
  error?: string | null;
  notice?: string | null;
  deleting: boolean;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  return (
    <DeleteDialog
      title={project.name}
      dialogTitle={t('multiSession.project.removeProject')}
      confirmLabel={t('multiSession.project.removeProjectConfirm')}
      descriptionKey="multiSession.project.removeProjectDescription"
      descriptionValues={{ projectName: project.name }}
      deleting={deleting}
      error={error ?? null}
      notice={notice ?? null}
      onCancel={onCancel}
      onDelete={onDelete}
    />
  );
}

function menuIcon(key: string, pinned: boolean): ReactNode {
  if (key === 'pin') {
    return pinned
      ? createElement(UnpinIcon, { 'aria-hidden': true })
      : createElement(PinIcon, { 'aria-hidden': true });
  }
  if (key === 'rename') return createElement(EditIcon, { 'aria-hidden': true });
  if (key === 'archive' || key === 'archive-sessions') {
    return key === 'archive'
      ? createElement(Archive, { 'aria-hidden': true, size: 16, strokeWidth: 1.8 })
      : createElement(FolderIcon, { 'aria-hidden': true });
  }
  if (key === 'delete') return createElement(DeleteIcon, { 'aria-hidden': true });
  return null;
}

/** 侧边栏菜单业务 Hook：生成 items schema + 弹框；不负责列表布局 */
export function useSidebarMenu(options: SidebarMenuOptions): {
  items: SidebarMenuItem[];
  dialogs: ReactNode;
} {
  const { t } = useTranslation();
  const [renameTarget, setRenameTarget] = useState<RenameTarget | null>(null);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [deleteProjectTarget, setDeleteProjectTarget] = useState<ProjectInfo | null>(null);
  const [projectAction, setProjectAction] = useState<'delete' | 'archive'>('delete');
  const [deleteProjectBusy, setDeleteProjectBusy] = useState(false);
  const [deleteProjectError, setDeleteProjectError] = useState<string | null>(null);
  const [deleteProjectNotice, setDeleteProjectNotice] = useState<string | null>(null);
  const [deleteSessionTarget, setDeleteSessionTarget] = useState<Session | null>(null);
  const [deleteSessionBusy, setDeleteSessionBusy] = useState(false);
  const [deleteSessionError, setDeleteSessionError] = useState<string | null>(null);
  /** 打开归档确认框时快照会话数，避免提交过程中标题随列表刷新变化 */
  const [archiveDialogSessionCount, setArchiveDialogSessionCount] = useState<number | null>(null);
  const archiveInFlightRef = useRef(new Set<string>());

  const {
    renameSession,
    renameProject,
    pinProject,
    removeProject,
    archiveSession,
    removeSessionLocally,
  } = useWorkspaceStore();
  const loadCronJobs = useCronStore((s) => s.loadJobs);
  const loadCronSessions = useCronStore((s) => s.loadCronSessions);

  const activeSessionId = options.type === 'session' ? options.activeSessionId : null;
  const onLeaveActiveSession = options.type === 'session' ? options.onLeaveActiveSession : () => {};

  function leaveIfActive(sessionId: string) {
    if (sessionId === activeSessionId) onLeaveActiveSession();
  }

  function purgeSessionRuntimes(sessionId: string) {
    forgetCreatedConversation(sessionId);
    useSessionStore.getState().removeSession(sessionId);
    useSessionStore.getState().removeRuntime(sessionId);
    useChatStore.getState().removeRuntime(sessionId);
    useSubagentStore.getState().removeRuntime(sessionId);
    useTodoStore.getState().removeRuntime(sessionId);
    useHarnessStore.getState().removeRuntime(sessionId);
    useGoalStore.getState().removeRuntime(sessionId);
  }

  async function archiveSessionAction(session: Session) {
    const opKey = `session:${session.session_id}`;
    if (archiveInFlightRef.current.has(opKey)) return;
    archiveInFlightRef.current.add(opKey);
    try {
      await archiveSession(session.session_id);
      flushSync(() => {
        removeSessionLocally(session.session_id);
        openArchiveSuccessToast(t, {
          content: t('multiSession.project.conversationArchived'),
          onUndo: async () => {
            const response = await archivedTaskClient.unarchiveSession(session.session_id);
            const entry = findBatchSessionResult(response, session.session_id);
            if (!entry?.ok) {
              const error = new Error(entry?.error || 'Failed to unarchive session');
              Object.assign(error, { code: entry?.code });
              throw error;
            }
            await useWorkspaceStore.getState().refreshWorkspaceAndCron();
          },
        });
      });
      leaveIfActive(session.session_id);
    } catch (error) {
      openArchiveFailureToast(t(archiveErrorKey(error)));
      await useWorkspaceStore.getState().refreshWorkspaceData();
    } finally {
      archiveInFlightRef.current.delete(opKey);
    }
  }

  async function deleteSession(session: Session) {
    const runtime = useChatStore.getState().getRuntime(session.session_id);
    if (runtime?.isProcessing || runtime?.pendingQuestions[0]) {
      throw Object.assign(new Error(t('multiSession.deleteRunningDisabled')), { code: 'SESSION_BUSY' });
    }
    await archivedTaskClient.deleteSession(session.session_id);
    purgeSessionRuntimes(session.session_id);
    removeSessionLocally(session.session_id);
    if (session.cron_id) {
      void loadCronSessions(session.project_id || 'default', session.cron_id);
    } else {
      const cronStore = useCronStore.getState();
      for (const [jobId, sessions] of Object.entries(cronStore.cronSessions)) {
        if (sessions.some((item) => item.session_id === session.session_id)) {
          const job = cronStore.jobs.find((j) => j.id === jobId);
          void cronStore.loadCronSessions(job?.project_id || 'default', jobId);
        }
      }
    }
    await useWorkspaceStore.getState().refreshWorkspaceData();
    leaveIfActive(session.session_id);
  }

  async function submitRename(value: string) {
    if (!renameTarget) return;
    setRenameError(null);
    try {
      if (renameTarget.kind === 'project') {
        await renameProject(renameTarget.id, value);
      } else {
        await renameSession(renameTarget.id, value);
        refreshExpandedCronSessions();
      }
      setRenameTarget(null);
    } catch (error) {
      setRenameError(error instanceof Error ? error.message : String(error));
    }
  }

  async function submitDeleteProject() {
    if (!deleteProjectTarget || (projectAction === 'delete' && isDefaultProject(deleteProjectTarget))) return;
    // 运行中会话已按需求「略过」：确定键只关闭对话框，不重试归档操作
    if (deleteProjectNotice) {
      setDeleteProjectNotice(null);
      setDeleteProjectError(null);
      setDeleteProjectTarget(null);
      setArchiveDialogSessionCount(null);
      return;
    }
    setDeleteProjectBusy(true);
    setDeleteProjectError(null);
    try {
      const projectId = deleteProjectTarget.project_id;
      if (projectAction === 'delete') {
        await removeProject(projectId);
        await loadCronJobs();
        toast.open({
          content: t('multiSession.project.projectRemovedSummary'),
          variant: 'success',
          actions: [{
            label: t('multiSession.project.archiveUndo'),
            onClick: () => {
              void useWorkspaceStore.getState().restoreProject(projectId).catch((error) => {
                toast.open({ content: projectActionErrorText(error, t), variant: 'error' });
              });
            },
          }],
        });
      } else {
        const result = await projectRegistryClient.archiveSessions(projectId);
        const succeededIds = result.results.filter((item) => item.ok).map((item) => item.session_id);
        const failedItems = result.results.filter((item) => !item.ok);
        const workspace = useWorkspaceStore.getState();
        workspace.removeSessions(succeededIds);
        await Promise.all([workspace.loadProjects(), workspace.loadProjectSessions(projectId), workspace.loadPinnedSessions()]);
        setDeleteProjectTarget(null);
        setArchiveDialogSessionCount(null);
        if (activeSessionId && succeededIds.includes(activeSessionId)) {
          onLeaveActiveSession();
        }
        if (succeededIds.length > 0) {
          openArchiveSuccessToast(t, {
            content: t('multiSession.project.sessionsArchived', { count: succeededIds.length }),
            replaceExisting: failedItems.length === 0,
            onUndo: async () => {
              const response = await archivedTaskClient.unarchiveSessions(succeededIds);
              const failed = response.results.filter((item) => !item.ok);
              if (failed.length) {
                const error = new Error(failed[0]?.error || 'Failed to unarchive sessions');
                Object.assign(error, { code: failed[0]?.code });
                throw error;
              }
              // 撤销归档若命中"项目已移除"的会话，会连带恢复项目，其定时任务
              // 重新可见（默认停用），cron 列表要一起刷新。
              await useWorkspaceStore.getState().refreshWorkspaceAndCron();
            },
          });
        }
        if (failedItems.length > 0) {
          const runningItems = failedItems.filter((item) => item.code === 'SESSION_BUSY');
          const failureContent = runningItems.length === failedItems.length
            ? t('multiSession.project.archiveBatchFailedRunning', { count: runningItems.length })
            : t('multiSession.project.archiveBatchFailed', { count: failedItems.length });
          openArchiveFailureToast(failureContent);
        }
        return;
      }
      setDeleteProjectTarget(null);
      setArchiveDialogSessionCount(null);
    } catch (error) {
      if (projectAction === 'archive') {
        setDeleteProjectTarget(null);
        setArchiveDialogSessionCount(null);
        openArchiveFailureToast(t(archiveErrorKey(error)));
        return;
      }
      setDeleteProjectError(projectActionErrorText(error, t));
    } finally {
      setDeleteProjectBusy(false);
    }
  }

  async function submitDeleteSession() {
    if (!deleteSessionTarget) return;
    setDeleteSessionBusy(true);
    setDeleteSessionError(null);
    try {
      await deleteSession(deleteSessionTarget);
      setDeleteSessionTarget(null);
      toast.open({ content: t('multiSession.project.conversationDeleted'), variant: 'success' });
    } catch (error) {
      const code = getArchiveErrorCode(error);
      setDeleteSessionError(
        error instanceof Error && error.message
          ? error.message
          : code === 'SESSION_BUSY'
            ? t('multiSession.project.errors.deleteSessionBusy')
            : t('multiSession.errors.delete'),
      );
    } finally {
      setDeleteSessionBusy(false);
    }
  }

  const dialogs: ReactNode = (
    <>
      {renameTarget ? (
        <RenameDialog
          title={t('multiSession.project.rename')}
          initialValue={renameTarget.value}
          placeholder={t('multiSession.project.renamePlaceholder')}
          error={renameError}
          onCancel={() => {
            setRenameError(null);
            setRenameTarget(null);
          }}
          onSubmit={(value) => void submitRename(value)}
        />
      ) : null}
      {deleteProjectTarget && projectAction === 'archive' ? (
        <ProjectArchiveDialog
          open
          sessionCount={archiveDialogSessionCount}
          archiving={deleteProjectBusy}
          onCancel={() => {
            if (deleteProjectBusy) return;
            setArchiveDialogSessionCount(null);
            setDeleteProjectTarget(null);
          }}
          onConfirm={() => { void submitDeleteProject(); }}
        />
      ) : null}
      {deleteProjectTarget && projectAction === 'delete' ? (
        <ProjectRemoveDialog
          project={deleteProjectTarget}
          deleting={deleteProjectBusy}
          error={deleteProjectError}
          notice={deleteProjectNotice}
          onCancel={() => {
            if (deleteProjectBusy) return;
            setDeleteProjectError(null);
            setDeleteProjectNotice(null);
            setDeleteProjectTarget(null);
          }}
          onDelete={() => { void submitDeleteProject(); }}
        />
      ) : null}
      {deleteSessionTarget ? (
        <DeleteDialog
          title={getSessionTitle(deleteSessionTarget, t('multiSession.untitled'))}
          dialogTitle={t('multiSession.deleteDialog.attention')}
          descriptionKey="multiSession.deleteDialog.permanentConfirm"
          descriptionValues={{}}
          confirmLabel={t('multiSession.project.confirm')}
          deleting={deleteSessionBusy}
          error={deleteSessionError}
          onCancel={() => {
            if (deleteSessionBusy) return;
            setDeleteSessionError(null);
            setDeleteSessionTarget(null);
          }}
          onDelete={() => { void submitDeleteSession(); }}
        />
      ) : null}
    </>
  );

  let items: SidebarMenuItem[] = [];

  if (options.type === 'session') {
    const { session, scope = 'conversation' } = options;
    const pinned = Boolean(session.pinned);
    const cron = isCronSession(session);
    const handlers: Record<string, () => void> = {
      pin: () => { void toggleSessionPin(session, t); },
      rename: () => {
        setRenameError(null);
        setRenameTarget({
          kind: 'session',
          id: session.session_id,
          value: getSessionTitle(session, t('multiSession.untitled')),
        });
      },
      archive: () => { void archiveSessionAction(session); },
      delete: () => {
        setDeleteSessionError(null);
        setDeleteSessionTarget(session);
      },
    };
    items = getSessionMenuItems(pinned, t, {
      scope,
      archivable: !cron,
    }).map((def) => ({
      ...def,
      icon: menuIcon(def.key, pinned),
      onSelect: handlers[def.key] ?? (() => {}),
    }));
    return { items, dialogs };
  }

  const { project, archiveSessionsDisabled } = options;
  const pinned = Boolean(project.pinned);
  const handlers: Record<string, () => void> = {
    pin: () => { void pinProject(project.project_id, !project.pinned); },
    rename: () => {
      setRenameError(null);
      setRenameTarget({ kind: 'project', id: project.project_id, value: project.name });
    },
    delete: () => {
      setProjectAction('delete');
      setDeleteProjectError(null);
      setDeleteProjectNotice(null);
      setArchiveDialogSessionCount(null);
      setDeleteProjectTarget(project);
    },
    'archive-sessions': () => {
      setProjectAction('archive');
      setDeleteProjectError(null);
      setDeleteProjectNotice(null);
      const workspace = useWorkspaceStore.getState();
      setArchiveDialogSessionCount(resolveProjectArchiveSessionCount(
        project,
        workspace.projectSessionTotals,
        workspace.pinnedSessions,
      ));
      setDeleteProjectTarget(project);
    },
  };
  items = getProjectMenuItems(pinned, t, {
    isDefault: isDefaultProject(project),
    archiveSessionsDisabled,
  }).map((def) => ({
    ...def,
    icon: menuIcon(def.key, pinned),
    onSelect: handlers[def.key] ?? (() => {}),
  }));

  return { items, dialogs };
}

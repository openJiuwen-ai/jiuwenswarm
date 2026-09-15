import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Archive, CircleAlert, Folder, Loader2, RotateCcw, Search, Trash2 } from 'lucide-react';
import { Button, Input, toast } from '../../../../components/ui';
import { SettingsConfirmDialog } from '../../components';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useCronStore, useWorkspaceStore } from '../../../../stores';
import {
  archivedTaskClient,
  findBatchSessionResult,
  getArchiveErrorCode,
  parseProjectOperationFailure,
  type ArchivedProject,
  type ArchivedSession,
  type ProjectOperationFailure,
} from '../../../../features/workspace/archivedTaskClient';
import {
  buildArchivedTaskGroups,
  formatArchivedAt,
  getArchivedSessionTitle,
} from '../../../../features/workspace/archivedTaskGrouping';
import { ProjectGroupHeader } from './ProjectGroupHeader';
import { useArchivedTaskLists } from './useArchivedTaskLists';
import './ArchivedTasksSettings.css';

const SEARCH_DEBOUNCE_MS = 300;

type PendingAction = 'restore' | 'delete';

type DeleteTarget =
  | { kind: 'session'; session: ArchivedSession }
  | { kind: 'project'; project: ArchivedProject };

/** 错误码只用于分支判断，用户看到的始终是可翻译文案。 */
function actionErrorKey(error: unknown): string {
  const code = getArchiveErrorCode(error);
  if (code === 'FORBIDDEN') return 'settingsPanel.archivedTasks.errors.forbidden';
  if (code === 'NOT_FOUND') return 'settingsPanel.archivedTasks.errors.notFound';
  return 'settingsPanel.archivedTasks.errors.requestFailed';
}

export function ArchivedTasksSettingsModule() {
  const { isConnected } = useSettingsServices();
  return <ArchivedTasksSettingsPanel isConnected={isConnected} />;
}

function ArchivedTasksSettingsPanel({ isConnected }: { isConnected: boolean }) {
  const { t, i18n } = useTranslation();
  const workMode = useWorkspaceStore((state) => state.workMode);
  const [searchInput, setSearchInput] = useState('');
  const [keyword, setKeyword] = useState('');
  const [pendingActions, setPendingActions] = useState<Record<string, PendingAction>>({});
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [projectFailures, setProjectFailures] = useState<Record<string, ProjectOperationFailure>>({});

  const {
    projectsState,
    sessionsState,
    fetchResource,
    refreshLists,
    removeLocalProjectRow,
    removeLocalProjectCascade,
    removeLocalSession,
  } = useArchivedTaskLists({ isConnected, keyword, workMode });

  // 单一搜索框同时驱动两个接口；新搜索由数据层的 replace 模式重置两种资源的 offset。
  useEffect(() => {
    const timerId = window.setTimeout(() => setKeyword(searchInput.trim()), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timerId);
  }, [searchInput]);

  // 操作结果走全局 toast（屏幕顶部居中，默认 3 秒自动消失）；成功/失败分别用 success/error 变体。
  const showToast = useCallback((kind: 'success' | 'error', message: string) => {
    toast.open({ content: message, variant: kind });
  }, []);

  const clearPendingAction = useCallback((actionKey: string) => {
    setPendingActions((prev) => {
      if (!(actionKey in prev)) return prev;
      const next = { ...prev };
      delete next[actionKey];
      return next;
    });
  }, []);

  const isResourcePending = (kind: 'project' | 'session', id: string) => (
    pendingActions[`${kind}:${id}`] !== undefined
  );

  const clearProjectFailure = useCallback((projectId: string) => {
    setProjectFailures((prev) => {
      if (!(projectId in prev)) return prev;
      const next = { ...prev };
      delete next[projectId];
      return next;
    });
  }, []);

  const handleRestoreSession = async (session: ArchivedSession) => {
    const actionKey = `session:${session.session_id}`;
    setPendingActions((prev) => ({ ...prev, [actionKey]: 'restore' }));
    try {
      const response = await archivedTaskClient.unarchiveSession(session.session_id);
      const entry = findBatchSessionResult(response, session.session_id);
      if (!entry || !entry.ok) {
        showToast('error', t('settingsPanel.archivedTasks.errors.sessionRestoreFailed'));
      } else {
        removeLocalSession(session.session_id);
        showToast('success', t(session.project_archived
          ? 'settingsPanel.archivedTasks.sessionRestoredProjectArchived'
          : 'settingsPanel.archivedTasks.sessionRestored'));
      }
    } catch (error) {
      if (getArchiveErrorCode(error) === 'NOT_FOUND') {
        removeLocalSession(session.session_id);
        showToast('success', t('settingsPanel.archivedTasks.sessionRestored'));
      } else {
        showToast('error', t(actionErrorKey(error)));
      }
    } finally {
      clearPendingAction(actionKey);
    }
    refreshLists();
    void useWorkspaceStore.getState().refreshWorkspaceData();
  };

  const handleRestoreProject = async (project: ArchivedProject) => {
    const actionKey = `project:${project.project_id}`;
    setPendingActions((prev) => ({ ...prev, [actionKey]: 'restore' }));
    try {
      await archivedTaskClient.unarchiveProject(project.project_id);
      clearProjectFailure(project.project_id);
      removeLocalProjectRow(project.project_id);
      showToast('success', t('settingsPanel.archivedTasks.projectRestoredCronPaused'));
    } catch (error) {
      if (getArchiveErrorCode(error) === 'NOT_FOUND') {
        clearProjectFailure(project.project_id);
        removeLocalProjectRow(project.project_id);
        showToast('success', t('settingsPanel.archivedTasks.projectRestoredCronPaused'));
      } else {
        showToast('error', t(actionErrorKey(error)));
      }
    } finally {
      clearPendingAction(actionKey);
    }
    refreshLists();
    void useWorkspaceStore.getState().refreshWorkspaceData();
    void useCronStore.getState().loadJobs();
  };

  const runDeleteProject = async (project: ArchivedProject): Promise<'ok' | 'partial' | 'error'> => {
    const actionKey = `project:${project.project_id}`;
    setPendingActions((prev) => ({ ...prev, [actionKey]: 'delete' }));
    try {
      await archivedTaskClient.deleteArchivedProject(project.project_id);
      clearProjectFailure(project.project_id);
      removeLocalProjectCascade(project.project_id);
      showToast('success', t('settingsPanel.archivedTasks.projectDeleted'));
      return 'ok';
    } catch (error) {
      if (getArchiveErrorCode(error) === 'NOT_FOUND') {
        clearProjectFailure(project.project_id);
        removeLocalProjectCascade(project.project_id);
        showToast('success', t('settingsPanel.archivedTasks.projectDeleted'));
        return 'ok';
      }
      const partial = parseProjectOperationFailure(error);
      if (partial) {
        setProjectFailures((prev) => ({ ...prev, [project.project_id]: partial }));
        showToast('error', t('settingsPanel.archivedTasks.partialFailure'));
        return 'partial';
      }
      showToast('error', t(actionErrorKey(error)));
      return 'error';
    } finally {
      clearPendingAction(actionKey);
      refreshLists();
      void useWorkspaceStore.getState().refreshWorkspaceData();
      void useCronStore.getState().loadJobs();
    }
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      if (deleteTarget.kind === 'session') {
        const session = deleteTarget.session;
        const actionKey = `session:${session.session_id}`;
        setPendingActions((prev) => ({ ...prev, [actionKey]: 'delete' }));
        try {
          await archivedTaskClient.deleteSession(session.session_id);
          removeLocalSession(session.session_id);
          showToast('success', t('settingsPanel.archivedTasks.sessionDeleted'));
          setDeleteTarget(null);
        } catch (error) {
          if (getArchiveErrorCode(error) === 'NOT_FOUND') {
            removeLocalSession(session.session_id);
            showToast('success', t('settingsPanel.archivedTasks.sessionDeleted'));
            setDeleteTarget(null);
          } else {
            setDeleteError(t(actionErrorKey(error)));
          }
        } finally {
          clearPendingAction(actionKey);
          refreshLists();
          void useWorkspaceStore.getState().refreshWorkspaceData();
        }
      } else {
        const result = await runDeleteProject(deleteTarget.project);
        if (result === 'ok' || result === 'partial') {
          setDeleteTarget(null);
        } else {
          setDeleteError(t('settingsPanel.archivedTasks.errors.requestFailed'));
        }
      }
    } finally {
      setDeleteBusy(false);
    }
  };

  const handleRetryProjectDelete = (project: ArchivedProject) => {
    void runDeleteProject(project);
  };

  const clearSearch = () => {
    setSearchInput('');
    setKeyword('');
  };

  const groups = useMemo(
    () => buildArchivedTaskGroups(projectsState.items, sessionsState.items),
    [projectsState.items, sessionsState.items],
  );

  const groupsEmpty = groups.length === 0;
  const anyError = projectsState.error || sessionsState.error;
  const bothError = projectsState.error && sessionsState.error;
  const anyLoading = projectsState.loading || sessionsState.loading;
  const anyHasMore = projectsState.hasMore || sessionsState.hasMore;
  const anyLoadingMore = projectsState.loadingMore || sessionsState.loadingMore;
  const showFullError = isConnected && bothError;
  const showSkeleton = isConnected && !anyError && anyLoading && groupsEmpty;
  const showEmptyState = isConnected && !anyError && !anyLoading && groupsEmpty;
  const showSearchEmptyState = showEmptyState && keyword !== '';

  const renderErrorState = (message: string, onRetry: () => void, testId: string) => (
    <div className="archived-tasks__error" role="alert" data-testid={testId}>
      <CircleAlert className="archived-tasks__error-icon" aria-hidden="true" size={16} />
      <span>{message}</span>
      <Button size="sm" onClick={onRetry} data-testid={`${testId}-retry`}>
        {t('settingsPanel.feedback.retry')}
      </Button>
    </div>
  );

  const renderSessionActions = (session: ArchivedSession) => {
    const pending = isResourcePending('session', session.session_id);
    const actionsDisabled = pending || session.stop_pending;
    const restoreTitle = t('settingsPanel.archivedTasks.restoreSession');
    const deleteTitle = t('settingsPanel.archivedTasks.deletePermanently');
    return (
      <span className="archived-tasks__row-actions">
        <Button
          variant="quiet"
          size="sm"
          icon={<RotateCcw aria-hidden="true" size={15} />}
          aria-label={restoreTitle}
          title={session.stop_pending ? t('settingsPanel.archivedTasks.stopPendingTooltip') : restoreTitle}
          disabled={actionsDisabled}
          aria-disabled={actionsDisabled || undefined}
          onClick={() => { void handleRestoreSession(session); }}
          data-testid="archived-tasks-session-restore"
          data-variant={session.session_id}
        />
        <Button
          variant="quiet"
          size="sm"
          className="archived-tasks__delete-button"
          icon={<Trash2 aria-hidden="true" size={15} />}
          aria-label={deleteTitle}
          title={session.stop_pending ? t('settingsPanel.archivedTasks.stopPendingTooltip') : deleteTitle}
          disabled={actionsDisabled}
          aria-disabled={actionsDisabled || undefined}
          onClick={() => {
            setDeleteError(null);
            setDeleteTarget({ kind: 'session', session });
          }}
          data-testid="archived-tasks-session-delete"
          data-variant={session.session_id}
        />
      </span>
    );
  };

  const renderSessionRow = (session: ArchivedSession) => (
    <li
      key={session.session_id}
      className="archived-tasks__row archived-tasks__row--session"
      data-testid="archived-tasks-session-row"
      data-variant={session.session_id}
    >
      <div className="archived-tasks__session-main">
        <div className="archived-tasks__session-title-line">
          <span
            className="archived-tasks__row-title"
            title={getArchivedSessionTitle(session, t('multiSession.untitled'))}
          >
            {getArchivedSessionTitle(session, t('multiSession.untitled'))}
          </span>
          {session.stop_pending ? (
            <span className="archived-tasks__stop-pending" data-testid="archived-tasks-stop-pending">
              {t('settingsPanel.archivedTasks.stopPending')}
            </span>
          ) : null}
        </div>
        <span className="archived-tasks__row-time">
          {formatArchivedAt(session.archived_at, i18n.language)}
        </span>
      </div>
      {renderSessionActions(session)}
    </li>
  );

  const deleteDialogTitle = deleteTarget?.kind === 'project'
    ? t('settingsPanel.archivedTasks.deleteProjectTitle')
    : t('settingsPanel.archivedTasks.deleteSessionTitle');

  const deleteDialogMessage = deleteTarget?.kind === 'project' ? (
    <>
      <p className="archived-tasks__dialog-line">
        {t('settingsPanel.archivedTasks.deleteProjectRecord', { projectName: deleteTarget.project.name })}
      </p>
      <p className="archived-tasks__dialog-line archived-tasks__dialog-line--danger">
        {t('settingsPanel.archivedTasks.deleteProjectScope')}
      </p>
      {deleteTarget.project.project_dir ? (
        <p className="archived-tasks__dialog-line">
          {t('settingsPanel.archivedTasks.deleteProjectDirKept')}
          <span className="archived-tasks__dialog-dir" title={deleteTarget.project.project_dir}>
            {deleteTarget.project.project_dir}
          </span>
        </p>
      ) : null}
    </>
  ) : deleteTarget?.kind === 'session' ? (
    <>
      <p className="archived-tasks__dialog-line">
        {t('settingsPanel.archivedTasks.deleteSessionRecord', {
          sessionTitle: getArchivedSessionTitle(deleteTarget.session, t('multiSession.untitled')),
        })}
      </p>
      <p className="archived-tasks__dialog-line archived-tasks__dialog-line--danger">
        {t('settingsPanel.archivedTasks.deleteSessionIrreversible')}
      </p>
    </>
  ) : null;

  return (
    <div className="archived-tasks" data-testid="settings-archived-tasks">
      <p className="archived-tasks__description" data-testid="archived-tasks-description">
        {t('settingsPanel.archivedTasks.description')}
      </p>
      <div className="archived-tasks__search">
        <Input
          value={searchInput}
          onChange={setSearchInput}
          placeholder={t('settingsPanel.archivedTasks.searchPlaceholder')}
          prefix={<Search aria-hidden="true" size={14} />}
          allowClear
          clearLabel={t('settingsPanel.archivedTasks.clearSearch')}
          onClear={clearSearch}
          data-testid="archived-tasks-search-input"
        />
      </div>

      {!isConnected ? (
        renderErrorState(t('settingsPanel.archivedTasks.loadFailed'), refreshLists, 'archived-tasks-error')
      ) : showFullError ? (
        renderErrorState(t('settingsPanel.archivedTasks.loadFailed'), refreshLists, 'archived-tasks-error')
      ) : showSkeleton ? (
        <div className="archived-tasks__loading" aria-busy="true" role="status" data-testid="archived-tasks-loading">
          <Loader2 className="archived-tasks__loading-icon" aria-hidden="true" size={16} />
          {t('common.loading')}
        </div>
      ) : showSearchEmptyState ? (
        <div className="archived-tasks__empty" data-testid="archived-tasks-empty-search">
          <p>{t('settingsPanel.archivedTasks.emptySearchTitle')}</p>
          <Button size="sm" onClick={clearSearch} data-testid="archived-tasks-empty-search-clear">
            {t('settingsPanel.archivedTasks.clearSearch')}
          </Button>
        </div>
      ) : showEmptyState ? (
        <div className="archived-tasks__empty" data-testid="archived-tasks-empty">
          <Archive className="archived-tasks__empty-icon" aria-hidden="true" size={20} />
          <p>{t('settingsPanel.archivedTasks.emptyTitle')}</p>
          <p className="archived-tasks__empty-hint">
            {t('settingsPanel.archivedTasks.emptyDescription')}
          </p>
        </div>
      ) : (
        <div className="archived-tasks__groups" data-testid="archived-tasks-list">
          {projectsState.error ? (
            renderErrorState(
              t('settingsPanel.archivedTasks.loadFailed'),
              () => fetchResource('projects', 'replace'),
              'archived-tasks-projects-error',
            )
          ) : null}
          {sessionsState.error ? (
            renderErrorState(
              t('settingsPanel.archivedTasks.loadFailed'),
              () => fetchResource('sessions', 'replace'),
              'archived-tasks-sessions-error',
            )
          ) : null}
          {groups.map((group) => {
            const { project } = group;
            const displayName = group.projectName ?? t('settingsPanel.archivedTasks.unassignedProject');
            const failure = project ? projectFailures[project.project_id] : undefined;
            return (
              <section
                key={group.key}
                className="archived-tasks__group"
                data-testid="archived-tasks-group"
                data-variant={group.key}
              >
                {project ? (
                  <ProjectGroupHeader
                    project={project}
                    pending={isResourcePending('project', project.project_id)}
                    onRestore={() => { void handleRestoreProject(project); }}
                    onDelete={() => {
                      setDeleteError(null);
                      setDeleteTarget({ kind: 'project', project });
                    }}
                  />
                ) : (
                  <div className="archived-tasks__group-header" data-testid="archived-tasks-group-header">
                    <Folder className="archived-tasks__row-icon" aria-hidden="true" size={16} />
                    <span className="archived-tasks__group-name" title={displayName}>{displayName}</span>
                    {group.sessions.length > 0 ? (
                      <span className="archived-tasks__group-count">({group.sessions.length})</span>
                    ) : null}
                  </div>
                )}
                {failure && project ? (
                  <div
                    className="archived-tasks__row-failure"
                    role="alert"
                    data-testid="archived-tasks-project-partial-failure"
                    data-variant={project.project_id}
                  >
                    <span>{t('settingsPanel.archivedTasks.partialFailure')}</span>
                    {failure.detail ? (
                      <span className="archived-tasks__row-failure-detail" title={failure.detail}>
                        {failure.detail}
                      </span>
                    ) : null}
                    {failure.retryable ? (
                      <Button
                        size="sm"
                        onClick={() => handleRetryProjectDelete(project)}
                        disabled={isResourcePending('project', project.project_id)}
                        data-testid="archived-tasks-project-partial-retry"
                        data-variant={project.project_id}
                      >
                        {t('settingsPanel.feedback.retry')}
                      </Button>
                    ) : null}
                  </div>
                ) : null}
                {group.sessions.length > 0 ? (
                  <ul className="archived-tasks__session-list" data-testid="archived-tasks-session-list">
                    {group.sessions.map(renderSessionRow)}
                  </ul>
                ) : null}
              </section>
            );
          })}
          {anyHasMore ? (
            <div className="archived-tasks__more">
              <Button
                size="sm"
                loading={anyLoadingMore}
                disabled={anyLoading}
                onClick={() => {
                  // 只对还有下一页的资源翻页，避免对已取尽的资源重复发请求
                  if (projectsState.hasMore) fetchResource('projects', 'more');
                  if (sessionsState.hasMore) fetchResource('sessions', 'more');
                }}
                data-testid="archived-tasks-load-more"
              >
                {t('settingsPanel.archivedTasks.loadMore')}
              </Button>
            </div>
          ) : null}
        </div>
      )}

      <SettingsConfirmDialog
        open={deleteTarget !== null}
        title={deleteDialogTitle}
        message={deleteDialogMessage}
        confirming={deleteBusy}
        error={deleteError ?? undefined}
        confirmLabel={t('settingsPanel.archivedTasks.deletePermanently')}
        confirmVariant="danger"
        onConfirm={() => { void handleConfirmDelete(); }}
        onCancel={() => {
          if (deleteBusy) return;
          setDeleteError(null);
          setDeleteTarget(null);
        }}
      />
    </div>
  );
}

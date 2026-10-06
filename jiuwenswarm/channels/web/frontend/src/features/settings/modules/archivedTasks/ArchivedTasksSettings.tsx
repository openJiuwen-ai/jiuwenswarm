import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Archive, CircleAlert, Folder, Loader2, RotateCcw, Search, Trash2 } from 'lucide-react';
import { Button, Input, toast } from '../../../../components/ui';
import { SettingsConfirmDialog } from '../../components';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useWorkspaceStore } from '../../../../stores';
import {
  archivedTaskClient,
  findBatchSessionResult,
  getArchiveErrorCode,
  type ArchivedSession,
} from '../../../../features/workspace/archivedTaskClient';
import {
  buildArchivedTaskGroups,
  formatArchivedAt,
  getArchivedSessionTitle,
} from '../../../../features/workspace/archivedTaskGrouping';
import { projectRegistryClient } from '../../../../features/workspace/projectRegistryClient';
import { useArchivedTaskLists } from './useArchivedTaskLists';
import './ArchivedTasksSettings.css';

const SEARCH_DEBOUNCE_MS = 300;

interface DeleteTarget {
  session: ArchivedSession;
}

/** 错误码只用于分支判断，用户看到的始终是可翻译文案。 */
function actionErrorKey(error: unknown): string {
  const code = getArchiveErrorCode(error);
  if (code === 'FORBIDDEN') return 'settingsPanel.archivedTasks.errors.forbidden';
  if (code === 'NOT_FOUND') return 'settingsPanel.archivedTasks.errors.notFound';
  // 撤销归档会连带恢复被移除的项目；项目名被占用时给出可操作的提示，
  // 而不是笼统的"操作失败"。
  if (code === 'PROJECT_NAME_CONFLICT') return 'settingsPanel.archivedTasks.errors.projectNameConflict';
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
  const [pendingActions, setPendingActions] = useState<Record<string, 'restore' | 'delete'>>({});
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // 项目分组级操作：删除该项目下全部已归档会话（项目无归档态，操作只针对会话集合）。
  const [deleteArchivedTarget, setDeleteArchivedTarget] = useState<{ projectId: string; projectName: string } | null>(null);
  const [deleteArchivedBusy, setDeleteArchivedBusy] = useState(false);
  const [deleteArchivedError, setDeleteArchivedError] = useState<string | null>(null);

  const {
    sessionsState,
    fetchResource,
    refreshLists,
    removeLocalSession,
  } = useArchivedTaskLists({ isConnected, keyword, workMode });

  // 单一搜索框驱动归档会话列表；新搜索由数据层的 replace 模式重置 offset。
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

  const isResourcePending = (id: string) => pendingActions[`session:${id}`] !== undefined;

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
        showToast('success', t('settingsPanel.archivedTasks.sessionRestored'));
      }
    } catch (error) {
      if (getArchiveErrorCode(error) === 'NOT_FOUND') {
        // NOT_FOUND 表示会话已被永久删除（如其他端删除后本端列表未同步），不存在"恢复成功"。
        removeLocalSession(session.session_id);
        showToast('error', t('settingsPanel.archivedTasks.errors.sessionGone'));
      } else {
        showToast('error', t(actionErrorKey(error)));
      }
    } finally {
      clearPendingAction(actionKey);
    }
    refreshLists();
    // 撤销一个属于已移除项目的归档会连带恢复该项目，其定时任务随之重新可见
    // （默认保持停用），cron 列表必须一起刷，否则停留在隐藏前的状态。
    void (session.project_hidden
      ? useWorkspaceStore.getState().refreshWorkspaceAndCron()
      : useWorkspaceStore.getState().refreshWorkspaceData());
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    const session = deleteTarget.session;
    setDeleteBusy(true);
    setDeleteError(null);
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
      setDeleteBusy(false);
      refreshLists();
      void useWorkspaceStore.getState().refreshWorkspaceData();
    }
  };

  const clearSearch = () => {
    setSearchInput('');
    setKeyword('');
  };

  // 项目分组级“删除已归档会话”：服务端枚举该项目归档区全部会话（含未加载分页），
  // 成功项本地移除；部分失败保留失败项并提示成功/失败数量。
  const handleConfirmDeleteArchivedSessions = async () => {
    if (!deleteArchivedTarget) return;
    setDeleteArchivedBusy(true);
    setDeleteArchivedError(null);
    try {
      const result = await projectRegistryClient.deleteArchivedSessions(deleteArchivedTarget.projectId);
      result.results.filter((item) => item.ok).forEach((item) => removeLocalSession(item.session_id));
      if (result.failed_count) {
        setDeleteArchivedError(t('settingsPanel.archivedTasks.batchPartialFailure', {
          succeeded: result.succeeded_count,
          failed: result.failed_count,
        }));
      } else {
        setDeleteArchivedTarget(null);
        showToast('success', t('settingsPanel.archivedTasks.archivedSessionsDeleted', { count: result.succeeded_count }));
      }
    } catch (error) {
      setDeleteArchivedError(t(actionErrorKey(error)));
    } finally {
      setDeleteArchivedBusy(false);
      refreshLists();
      void useWorkspaceStore.getState().refreshWorkspaceData();
    }
  };

  const groups = useMemo(
    () => buildArchivedTaskGroups(sessionsState.items),
    [sessionsState.items],
  );

  const groupsEmpty = groups.length === 0;
  const hasError = sessionsState.error !== null;
  const isLoading = sessionsState.loading;
  const hasMore = sessionsState.hasMore;
  const loadingMore = sessionsState.loadingMore;
  const showSkeleton = isConnected && !hasError && isLoading && groupsEmpty;
  const showEmptyState = isConnected && !hasError && !isLoading && groupsEmpty;
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
    const pending = isResourcePending(session.session_id);
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
            setDeleteTarget({ session });
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

  const deleteDialogMessage = deleteTarget ? (
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
      ) : hasError ? (
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
          {groups.map((group) => {
            const displayName = group.projectName ?? t('settingsPanel.archivedTasks.unassignedProject');
            return (
              <section
                key={group.key}
                className="archived-tasks__group"
                data-testid="archived-tasks-group"
                data-variant={group.key}
              >
                <div className="archived-tasks__group-header" data-testid="archived-tasks-group-header">
                  <Folder className="archived-tasks__row-icon" aria-hidden="true" size={16} />
                  <span className="archived-tasks__group-name" title={displayName}>{displayName}</span>
                  {group.sessions.length > 0 ? (
                    <span className="archived-tasks__group-count">({group.sessions.length})</span>
                  ) : null}
                  {/* 仅项目分组提供“删除已归档会话”；未归属分组没有可操作的项目 */}
                  {group.projectId ? (
                    <span className="archived-tasks__row-actions">
                      <Button
                        variant="quiet"
                        size="sm"
                        className="archived-tasks__delete-button"
                        icon={<Trash2 aria-hidden="true" size={15} />}
                        aria-label={t('settingsPanel.archivedTasks.deleteArchivedSessions')}
                        title={t('settingsPanel.archivedTasks.deleteArchivedSessions')}
                        onClick={() => {
                          setDeleteArchivedError(null);
                          setDeleteArchivedTarget({ projectId: group.projectId, projectName: group.projectName ?? displayName });
                        }}
                        data-testid="archived-tasks-group-delete-archived"
                        data-variant={group.key}
                      />
                    </span>
                  ) : null}
                </div>
                {group.projectHidden ? (
                  <p
                    className="archived-tasks__removed-project-note"
                    data-testid="archived-tasks-removed-project-note"
                    data-variant={group.key}
                  >
                    {t('settingsPanel.archivedTasks.removedProjectNote')}
                  </p>
                ) : null}
                <ul className="archived-tasks__session-list" data-testid="archived-tasks-session-list">
                  {group.sessions.map(renderSessionRow)}
                </ul>
              </section>
            );
          })}
          {hasMore ? (
            <div className="archived-tasks__more">
              <Button
                size="sm"
                loading={loadingMore}
                disabled={isLoading}
                onClick={() => fetchResource('more')}
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
        title={t('settingsPanel.archivedTasks.deleteSessionTitle')}
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

      <SettingsConfirmDialog
        open={deleteArchivedTarget !== null}
        title={t('settingsPanel.archivedTasks.deleteArchivedSessionsTitle')}
        message={deleteArchivedTarget ? (
          <p className="archived-tasks__dialog-line">
            {t('settingsPanel.archivedTasks.deleteArchivedSessionsRecord', { projectName: deleteArchivedTarget.projectName })}
          </p>
        ) : null}
        confirming={deleteArchivedBusy}
        error={deleteArchivedError ?? undefined}
        confirmLabel={t('settingsPanel.archivedTasks.deletePermanently')}
        confirmVariant="danger"
        onConfirm={() => { void handleConfirmDeleteArchivedSessions(); }}
        onCancel={() => {
          if (deleteArchivedBusy) return;
          setDeleteArchivedError(null);
          setDeleteArchivedTarget(null);
        }}
      />
    </div>
  );
}

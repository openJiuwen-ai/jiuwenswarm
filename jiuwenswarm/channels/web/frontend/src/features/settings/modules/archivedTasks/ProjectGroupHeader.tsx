import { Folder, RotateCcw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import type { ArchivedProject } from '../../../../features/workspace/archivedTaskClient';

/** 已归档项目分组头：与会话行一致，直接展示恢复 / 彻底删除。 */
export function ProjectGroupHeader({
  project,
  pending,
  onRestore,
  onDelete,
}: {
  project: ArchivedProject;
  pending: boolean;
  onRestore: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const actionsDisabled = pending || project.stop_pending;
  const restoreTitle = t('settingsPanel.archivedTasks.restoreProject');
  const deleteTitle = t('settingsPanel.archivedTasks.deletePermanently');
  const pendingTitle = t('settingsPanel.archivedTasks.stopPendingTooltip');
  return (
    <div
      className="archived-tasks__group-header"
      data-testid="archived-tasks-group-header"
    >
      <Folder className="archived-tasks__row-icon" aria-hidden="true" size={16} />
      <span className="archived-tasks__group-name" title={project.name}>{project.name}</span>
      <span className="archived-tasks__type-tag">
        {t('settingsPanel.archivedTasks.projectTag')}
      </span>
      {project.stop_pending ? (
        <span className="archived-tasks__stop-pending" data-testid="archived-tasks-stop-pending">
          {t('settingsPanel.archivedTasks.stopPending')}
        </span>
      ) : null}
      <span className="archived-tasks__row-actions">
        <Button
          variant="quiet"
          size="sm"
          icon={<RotateCcw aria-hidden="true" size={15} />}
          aria-label={restoreTitle}
          title={project.stop_pending ? pendingTitle : restoreTitle}
          disabled={actionsDisabled}
          aria-disabled={actionsDisabled || undefined}
          onClick={onRestore}
          data-testid="archived-tasks-project-restore"
          data-variant={project.project_id}
        />
        <Button
          variant="quiet"
          size="sm"
          className="archived-tasks__delete-button"
          icon={<Trash2 aria-hidden="true" size={15} />}
          aria-label={deleteTitle}
          title={project.stop_pending ? pendingTitle : deleteTitle}
          disabled={actionsDisabled}
          aria-disabled={actionsDisabled || undefined}
          onClick={onDelete}
          data-testid="archived-tasks-project-delete"
          data-variant={project.project_id}
        />
      </span>
    </div>
  );
}

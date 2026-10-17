import { useId } from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button, Dialog } from '../../components/ui';
import './ProjectArchiveDialog.css';

export { resolveProjectArchiveSessionCount } from './projectArchiveModel';

interface ProjectArchiveDialogProps {
  open: boolean;
  sessionCount: number | null;
  archiving: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ProjectArchiveDialog({
  open,
  sessionCount,
  archiving,
  onCancel,
  onConfirm,
}: ProjectArchiveDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const title = sessionCount !== null
    ? t('multiSession.project.archiveConfirmTitle', { count: sessionCount })
    : t('multiSession.project.archiveConfirmTitleFallback');

  return (
    <Dialog
      open={open}
      titleId={titleId}
      className="project-archive-dialog"
      closeDisabled={archiving}
      onCancel={onCancel}
      onBackdropClick={onCancel}
    >
      <div className="project-archive-dialog__panel" data-testid="multi-session-project-archive-dialog">
        <header className="project-archive-dialog__header">
          <h2 id={titleId} data-testid="multi-session-project-archive-dialog-title">{title}</h2>
          <button
            type="button"
            className="project-archive-dialog__close"
            aria-label={t('common.close')}
            disabled={archiving}
            onClick={onCancel}
            data-testid="multi-session-project-archive-dialog-close"
          >
            <X aria-hidden size={20} />
          </button>
        </header>
        <p className="project-archive-dialog__description" data-testid="multi-session-project-archive-dialog-description">
          {t('multiSession.project.archiveConfirmDescription')}
        </p>
        <footer className="project-archive-dialog__actions" data-testid="multi-session-project-archive-dialog-actions">
          <Button size="sm" disabled={archiving} onClick={onCancel} data-testid="multi-session-project-archive-dialog-cancel">
            {t('common.cancel')}
          </Button>
          <Button
            variant="primary"
            size="sm"
            loading={archiving}
            onClick={onConfirm}
            data-testid="multi-session-project-archive-dialog-confirm"
          >
            {t('multiSession.project.archiveConfirmAll')}
          </Button>
        </footer>
      </div>
    </Dialog>
  );
}

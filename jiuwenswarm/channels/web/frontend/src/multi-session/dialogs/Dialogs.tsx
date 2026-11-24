import { type ReactNode } from 'react';
import { X } from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';
import './dialogs.css';

interface DialogShellProps { title: string; children: ReactNode; onCancel: () => void }
interface DeleteDialogProps {
  title: string;
  dialogTitle?: string;
  confirmLabel?: string;
  descriptionKey?: string;
  descriptionValues?: Record<string, string>;
  deleting: boolean;
  error: string | null;
  /** 仅提示信息（如运行中会话已略过）；展示后确定按钮只关闭对话框。 */
  notice?: string | null;
  onCancel: () => void;
  onDelete: () => void;
}

function DialogShell({ title, children, onCancel }: DialogShellProps) {
  const { t } = useTranslation();
  return (
    <div className="conversation-dialog" role="dialog" aria-modal="true" aria-label={title} data-testid="multi-session-dialog" data-variant="delete">
      <button type="button" className="conversation-dialog__backdrop" onClick={onCancel} aria-label={t('common.cancel')} data-testid="multi-session-dialog-backdrop" />
      <div className="conversation-dialog__panel" data-testid="multi-session-dialog-panel">
        <button type="button" className="conversation-dialog__close" onClick={onCancel} aria-label={t('common.close')} data-testid="multi-session-dialog-close"><X size={20} /></button>
        <h2 data-testid="multi-session-dialog-title">{title}</h2>
        {children}
      </div>
    </div>
  );
}

function DialogActions({ busy, danger = false, confirmLabel, onCancel, onConfirm }: { busy?: boolean; danger?: boolean; confirmLabel: string; onCancel: () => void; onConfirm: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="conversation-dialog__actions" data-testid="multi-session-dialog-actions">
      <button type="button" onClick={onCancel} data-testid="multi-session-dialog-cancel">{t('common.cancel')}</button>
      <button type="button" className={danger ? 'is-danger' : 'is-primary'} disabled={busy} onClick={onConfirm} data-testid="multi-session-dialog-confirm" data-variant={danger ? 'danger' : 'primary'}>
        {confirmLabel}
      </button>
    </div>
  );
}


interface NoticeDialogProps {
  title: string;
  message?: string;
  /** 使用 Trans 渲染文案（如配额满：空间 / 申请扩容可点击）。 */
  messageKey?: string;
  confirmLabel?: string;
  /** 次要/确定按钮旁的主操作（如「申请扩容」）。 */
  actionLabel?: string;
  onAction?: () => void;
  onNavigateSpace?: () => void;
  onRequestExpand?: () => void;
  onClose: () => void;
}

/** 提示框（配额已满等），样式与 DeleteDialog 同壳；配额场景带超链接与「申请扩容」按钮。 */
export function NoticeDialog({
  title,
  message,
  messageKey,
  confirmLabel,
  actionLabel,
  onAction,
  onNavigateSpace,
  onRequestExpand,
  onClose,
}: NoticeDialogProps) {
  const { t } = useTranslation();
  const showExpandAction = Boolean(actionLabel && onAction);
  return (
    <div
      className="conversation-dialog"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-testid="multi-session-notice-dialog"
    >
      <button
        type="button"
        className="conversation-dialog__backdrop"
        onClick={onClose}
        aria-label={t('common.cancel')}
        data-testid="multi-session-notice-dialog-backdrop"
      />
      <div className="conversation-dialog__panel" data-testid="multi-session-notice-dialog-panel">
        <button
          type="button"
          className="conversation-dialog__close"
          onClick={onClose}
          aria-label={t('common.close')}
          data-testid="multi-session-notice-dialog-close"
        >
          <X size={20} />
        </button>
        <h2 data-testid="multi-session-notice-dialog-title">{title}</h2>
        {messageKey ? (
          <p data-testid="multi-session-notice-dialog-message">
            <Trans
              i18nKey={messageKey}
              components={{
                space: (
                  <button
                    type="button"
                    className="conversation-dialog__link"
                    onClick={onNavigateSpace}
                    data-testid="multi-session-notice-dialog-space-link"
                  />
                ),
                expand: (
                  <button
                    type="button"
                    className="conversation-dialog__link"
                    onClick={onRequestExpand ?? onAction}
                    data-testid="multi-session-notice-dialog-expand-link"
                  />
                ),
              }}
            />
          </p>
        ) : message ? (
          <p data-testid="multi-session-notice-dialog-message">{message}</p>
        ) : null}
        <div className="conversation-dialog__actions" data-testid="multi-session-notice-dialog-actions">
          {showExpandAction ? (
            <button
              type="button"
              onClick={onClose}
              data-testid="multi-session-notice-dialog-dismiss"
            >
              {confirmLabel || t('common.ok')}
            </button>
          ) : null}
          <button
            type="button"
            className="is-primary"
            onClick={showExpandAction ? onAction : onClose}
            data-testid="multi-session-notice-dialog-confirm"
          >
            {showExpandAction ? actionLabel : (confirmLabel || t('common.ok'))}
          </button>
        </div>
      </div>
    </div>
  );
}

export function DeleteDialog({
  title,
  dialogTitle,
  confirmLabel,
  descriptionKey = 'multiSession.deleteDialog.description',
  descriptionValues = { title },
  deleting,
  error,
  notice,
  onCancel,
  onDelete,
}: DeleteDialogProps) {
  const { t } = useTranslation();
  return (
    <DialogShell title={dialogTitle ?? t('multiSession.deleteDialog.title')} onCancel={onCancel}>
      <p data-testid="multi-session-dialog-description">
        <Trans
          i18nKey={descriptionKey}
          values={descriptionValues}
          components={{
            name: <span className="conversation-dialog__subject" title={title} />,
          }}
        />
      </p>
      {error && <div className="conversation-dialog__error" data-testid="multi-session-dialog-error">{error}</div>}
      {notice && <div className="conversation-dialog__notice" data-testid="multi-session-dialog-notice">{notice}</div>}
      <DialogActions busy={deleting} danger confirmLabel={confirmLabel ?? t('multiSession.delete')} onCancel={onCancel} onConfirm={onDelete} />
    </DialogShell>
  );
}

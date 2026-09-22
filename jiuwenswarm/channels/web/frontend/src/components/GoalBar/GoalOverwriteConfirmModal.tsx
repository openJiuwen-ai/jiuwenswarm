/**
 * 覆盖目标确认弹窗 —— 独立弹窗
 *
 * /goal set 或工具栏编辑在"已有未完成目标"时需要用户确认替换。之前用 window.confirm
 * （浏览器原生对话框，样式与产品完全脱节，issue #4682 反馈"页面提示有点丑"），
 * 这里改成与 EditGoalModal 同一套视觉语言的 portal 弹窗。
 * 点遮罩不关闭，只能通过右上角 × /"取消"/"确认替换"退出，避免误触把确认结果默认掉。
 */

import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Target, X } from 'lucide-react';

interface GoalOverwriteConfirmModalProps {
  currentObjective: string;
  requestedObjective: string;
  onCancel: () => void;
  onConfirm: () => void;
}

export function GoalOverwriteConfirmModal({
  currentObjective,
  requestedObjective,
  onCancel,
  onConfirm,
}: GoalOverwriteConfirmModalProps) {
  const { t } = useTranslation();

  return createPortal(
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center bg-black/40"
      data-testid="goal-overwrite-confirm-modal"
    >
      <div
        className="relative w-[420px] rounded-2xl border border-border bg-card p-5 shadow-lg"
        data-testid="goal-overwrite-confirm-modal-card"
      >
        <div className="mb-3 flex items-center justify-between">
          <div
            className="flex h-7 w-7 items-center justify-center rounded-full bg-secondary text-accent"
            data-testid="goal-overwrite-confirm-modal-icon"
          >
            <Target size={15} strokeWidth={2} />
          </div>
          <button
            type="button"
            onClick={onCancel}
            aria-label="close"
            className="rounded-md p-1 text-text-muted hover:bg-secondary hover:text-text"
            data-testid="goal-overwrite-confirm-modal-close-button"
          >
            <X size={16} strokeWidth={2} />
          </button>
        </div>
        <h3
          className="mb-3 text-[15px] font-semibold text-text-strong"
          data-testid="goal-overwrite-confirm-modal-title"
        >
          {t('goal.overwriteTitle')}
        </h3>
        <p className="text-[13px] leading-5 text-text-muted" data-testid="goal-overwrite-confirm-modal-body">
          {t('goal.overwriteConfirm', { currentObjective, requestedObjective })}
        </p>
        <div className="mt-4 flex justify-end gap-2" data-testid="goal-overwrite-confirm-modal-actions">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-border px-4 py-1.5 text-[13px] text-text-muted hover:bg-secondary"
            data-testid="goal-overwrite-confirm-modal-cancel-button"
          >
            {t('goal.formCancel')}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-lg bg-text-strong px-4 py-1.5 text-[13px] text-card"
            data-testid="goal-overwrite-confirm-modal-confirm-button"
          >
            {t('goal.overwriteSubmit')}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

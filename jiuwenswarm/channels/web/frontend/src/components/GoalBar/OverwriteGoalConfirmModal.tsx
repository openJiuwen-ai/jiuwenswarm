/**
 * 覆盖目标确认 —— 独立弹窗
 *
 * bugfix 2026092201 bug001 子问题 1：已有未完成目标时再设置新目标，原来用的是
 * 浏览器原生 `window.confirm()`，样式很丑。这里照抄同目录下 `EditGoalModal.tsx`
 * 的壳子（createPortal 到 body、项目自己的 design token），替换掉原生弹窗。
 *
 * 点击遮罩不关闭，只能通过右上角 × 或"取消"退出——覆盖目标是有后果的操作
 * （会丢弃当前未完成目标），避免误触。
 */

import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Target, X } from 'lucide-react';

interface OverwriteGoalConfirmModalProps {
  currentObjective: string;
  requestedObjective: string;
  onCancel: () => void;
  onConfirm: () => void;
}

export function OverwriteGoalConfirmModal({
  currentObjective,
  requestedObjective,
  onCancel,
  onConfirm,
}: OverwriteGoalConfirmModalProps) {
  const { t } = useTranslation();

  return createPortal(
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center bg-black/40"
      data-testid="goal-bar-overwrite-confirm-modal"
    >
      <div
        className="relative w-[420px] rounded-2xl border border-border bg-card p-5 shadow-lg"
        data-testid="goal-bar-overwrite-confirm-modal-card"
      >
        <div className="mb-3 flex items-center justify-between">
          <div
            className="flex h-7 w-7 items-center justify-center rounded-full bg-secondary text-accent"
            data-testid="goal-bar-overwrite-confirm-modal-icon"
          >
            <Target size={15} strokeWidth={2} />
          </div>
          <button
            type="button"
            onClick={onCancel}
            aria-label="close"
            className="rounded-md p-1 text-text-muted hover:bg-secondary hover:text-text"
            data-testid="goal-bar-overwrite-confirm-modal-close-button"
          >
            <X size={16} strokeWidth={2} />
          </button>
        </div>
        <p
          className="mb-4 text-[13px] leading-6 text-text"
          data-testid="goal-bar-overwrite-confirm-modal-message"
        >
          {t('goal.overwriteConfirm', { currentObjective, requestedObjective })}
        </p>
        <div className="flex justify-end gap-2" data-testid="goal-bar-overwrite-confirm-modal-actions">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-border px-4 py-1.5 text-[13px] text-text-muted hover:bg-secondary"
            data-testid="goal-bar-overwrite-confirm-modal-cancel-button"
          >
            {t('goal.formCancel')}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-lg bg-text-strong px-4 py-1.5 text-[13px] text-card"
            data-testid="goal-bar-overwrite-confirm-modal-confirm-button"
          >
            {t('goal.formSubmit')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/**
 * 编辑目标 —— 独立弹窗
 *
 * 点击遮罩不关闭，只能通过右上角 × 或"取消"退出（避免误触丢失编辑内容）。
 * 保存复用已有的 command.goal set（overwrite_confirmed: true）语义，不需要新接口。
 */

import { useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Target, X } from 'lucide-react';

interface EditGoalModalProps {
  initialObjective: string;
  onCancel: () => void;
  onSave: (objective: string) => void;
}

export function EditGoalModal({ initialObjective, onCancel, onSave }: EditGoalModalProps) {
  const { t } = useTranslation();
  const [value, setValue] = useState(initialObjective);
  const canSave = Boolean(value.trim()) && value.trim() !== initialObjective.trim();

  return createPortal(
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/40">
      <div className="relative w-[420px] rounded-2xl border border-border bg-card p-5 shadow-lg">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex h-7 w-7 items-center justify-center rounded-full bg-secondary text-accent">
            <Target size={15} strokeWidth={2} />
          </div>
          <button
            type="button"
            onClick={onCancel}
            aria-label="close"
            className="rounded-md p-1 text-text-muted hover:bg-secondary hover:text-text"
          >
            <X size={16} strokeWidth={2} />
          </button>
        </div>
        <h3 className="mb-3 text-[15px] font-semibold text-text-strong">{t('goal.editTitle')}</h3>
        <textarea
          autoFocus
          className="w-full resize-none rounded-lg border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
          rows={5}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-border px-4 py-1.5 text-[13px] text-text-muted hover:bg-secondary"
          >
            {t('goal.formCancel')}
          </button>
          <button
            type="button"
            disabled={!canSave}
            onClick={() => onSave(value.trim())}
            className="rounded-lg bg-text-strong px-4 py-1.5 text-[13px] text-card disabled:opacity-50"
          >
            {t('goal.formSubmit')}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

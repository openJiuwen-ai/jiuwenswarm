/**
 * 上传本地技能弹窗（.zip）
 *
 * 从 index.tsx 抽取；路径/文件选择为弹窗内部状态（关闭即重置，与原实现一致）。
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import TipIcon from '../../assets/tip.svg?react';
import UpFileIcon from '../../assets/upFile.svg?react';
import { ModalCloseButton } from './SkillPanelWidgets';

interface UploadSkillModalProps {
  actionTarget: string | null;
  onUpload: (file: File) => void;
  onClose: () => void;
}

export function UploadSkillModal({ actionTarget, onUpload, onClose }: UploadSkillModalProps) {
  const { t } = useTranslation();
  const [uploadSkillPath, setUploadSkillPath] = useState('');
  const [uploadSkillFile, setUploadSkillFile] = useState<File | null>(null);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t('skills.uploadSkillModal.cancel')}
      />
      <div
        className="relative overflow-hidden rounded-[8px] border border-border bg-card shadow-2xl animate-rise flex flex-col"
        style={{ width: '550px' }}
        data-testid="skill-panel-upload-skill-modal"
      >
        {/* 头部 */}
        <div className="flex items-center justify-between gap-3 px-5 py-3 bg-panel">
          <span data-testid="skill-panel-upload-skill-modal-title" className="text-lg font-semibold text-text-strong">
            {t('skills.uploadSkillModal.title')}
          </span>
          <ModalCloseButton
            onClick={onClose}
            label={t('skills.uploadSkillModal.cancel')}
            testId="skill-panel-upload-skill-modal-close-btn"
          />
        </div>
        {/* 提示行 */}
        <div className="px-5 pt-3">
          <div
            className="flex items-start gap-1.5 rounded-[8px] px-3 py-2 text-xs text-text bg-[var(--color-skill-notice-surface)]"
            style={{ width: '502px' }}
          >
            <TipIcon className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span className="leading-4">{t('skills.uploadSkillModal.notice')}</span>
          </div>
        </div>
        {/* 文件上传拖动框 */}
        <div className="px-5 pt-3 pb-5">
          <label
            onDragOver={(e) => {
              e.preventDefault();
            }}
            onDrop={(e) => {
              e.preventDefault();
              const file = e.dataTransfer.files[0];
              if (file && file.name.endsWith('.zip')) {
                setUploadSkillPath(file.name);
                setUploadSkillFile(file);
              }
            }}
            className="flex flex-col items-center justify-center gap-2 rounded-[12px] border border-dashed border-border cursor-pointer bg-[var(--color-skill-dropzone-surface)] hover:bg-[var(--color-skill-dropzone-hover-surface)]"
            style={{ width: '502px', height: '160px' }}
          >
            <UpFileIcon className="w-6 h-6 text-text-muted" />
            <span className="text-sm text-text-muted">
              {uploadSkillPath.trim() ? uploadSkillPath : t('skills.uploadSkillModal.dropHint')}
            </span>
            <input
              type="file"
              accept=".zip"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  setUploadSkillPath(file.name);
                  setUploadSkillFile(file);
                }
              }}
            />
          </label>
        </div>
        {/* 底部按钮 */}
        <div className="flex items-center justify-end gap-3 px-5 py-3 bg-panel">
          <button
            type="button"
            onClick={onClose}
            className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
            style={{ height: '32px', padding: '0 32px' }}
          >
            {t('skills.uploadSkillModal.cancel')}
          </button>
          <button
            type="button"
            disabled={!uploadSkillFile || actionTarget === 'import_local'}
            onClick={() => {
              const file = uploadSkillFile;
              onClose();
              if (file) onUpload(file);
            }}
            className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
              !uploadSkillFile || actionTarget === 'import_local'
                ? 'bg-secondary text-text-muted cursor-not-allowed'
                : 'text-text-inverse bg-control-emphasis hover:opacity-80'
            }`}
            style={{ height: '32px', padding: '0 32px' }}
          >
            {t('skills.uploadSkillModal.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}

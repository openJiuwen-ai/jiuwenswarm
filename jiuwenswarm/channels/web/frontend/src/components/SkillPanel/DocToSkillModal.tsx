/**
 * 知识转技能弹窗（本地文档 / 链接）
 *
 * 从 index.tsx 抽取；表单与 tooltip 为弹窗内部状态（关闭即重置，与原实现一致）。
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { HelpCircle, Upload } from 'lucide-react';
import { ModalCloseButton, TopAnchorTooltip } from './SkillPanelWidgets';

interface DocToSkillModalProps {
  onCreateFromKnowledge: (params: { file?: File | null; link?: string; skillDescription?: string }) => void;
  onClose: () => void;
}

export function DocToSkillModal({ onCreateFromKnowledge, onClose }: DocToSkillModalProps) {
  const { t } = useTranslation();
  const [docToSkillSource, setDocToSkillSource] = useState<'local' | 'link'>('local');
  const [docToSkillPath, setDocToSkillPath] = useState('');
  const [docToSkillFile, setDocToSkillFile] = useState<File | null>(null);
  const [docToSkillLink, setDocToSkillLink] = useState('');
  const [docToSkillDesc, setDocToSkillDesc] = useState('');
  const [docToSkillTooltip, setDocToSkillTooltip] = useState<{ left: number; top: number } | null>(null);

  const isDocConfirmDisabled = docToSkillSource === 'local' ? !docToSkillFile : !docToSkillLink.trim();
  return (
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <button
          type="button"
          className="absolute inset-0 bg-black/60"
          onClick={onClose}
          aria-label={t('skills.docToSkillModal.cancel')}
        />
        <div
          className="relative overflow-hidden rounded-[8px] border border-border bg-card shadow-2xl animate-rise flex flex-col"
          style={{ width: '550px' }}
        >
          {/* 头部 */}
          <div className="flex items-center justify-between gap-3 px-5 pt-3 pb-0 bg-panel">
            <span className="text-lg font-semibold text-text-strong">{t('skills.docToSkillModal.title')}</span>
            <ModalCloseButton onClick={onClose} label={t('skills.docToSkillModal.cancel')} />
          </div>
          {/* 副标题 */}
          <div className="px-5">
            <span className="text-xs text-text-muted">{t('skills.docToSkillModal.subtitle')}</span>
          </div>
          {/* 来源 */}
          <div className="px-5 pt-4">
            <span className="block text-sm font-medium text-text mb-2">{t('skills.docToSkillModal.sourceLabel')}</span>
            <div className="flex items-center gap-4">
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="radio"
                  checked={docToSkillSource === 'local'}
                  onChange={() => setDocToSkillSource('local')}
                  className="w-3.5 h-3.5 accent-[var(--color-chat-accent)]"
                />
                <span className="text-sm text-text">{t('skills.docToSkillModal.sourceLocal')}</span>
              </label>
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="radio"
                  checked={docToSkillSource === 'link'}
                  onChange={() => setDocToSkillSource('link')}
                  className="w-3.5 h-3.5 accent-[var(--color-chat-accent)]"
                />
                <span className="text-sm text-text">{t('skills.docToSkillModal.sourceLink')}</span>
              </label>
            </div>
          </div>
          {/* 本地上传 */}
          {docToSkillSource === 'local' && (
            <div className="px-5 pt-3">
              <label
                onDragOver={(e) => {
                  e.preventDefault();
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  const file = e.dataTransfer.files[0];
                  if (file) {
                    setDocToSkillPath(file.name);
                    setDocToSkillFile(file);
                  }
                }}
                className="flex flex-col items-center justify-center gap-2 rounded-[12px] border border-dashed border-border cursor-pointer bg-[var(--color-skill-dropzone-surface)] hover:bg-[var(--color-skill-dropzone-hover-surface)]"
                style={{ width: '502px', height: '160px' }}
              >
                <Upload className="w-6 h-6 text-text-muted" />
                <span className="text-sm text-text-muted whitespace-pre-line text-center">
                  {docToSkillPath.trim() ? docToSkillPath : t('skills.docToSkillModal.dropHint')}
                </span>
                <input
                  type="file"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) {
                      setDocToSkillPath(file.name);
                      setDocToSkillFile(file);
                    }
                  }}
                />
              </label>
            </div>
          )}
          {/* 链接 */}
          {docToSkillSource === 'link' && (
            <div className="px-5 pt-3">
              <div className="flex items-center gap-1.5 mb-2">
                <span className="text-sm font-medium text-text">{t('skills.docToSkillModal.linkLabel')}</span>
                <span
                  onMouseEnter={(e) => {
                    const rect = e.currentTarget.getBoundingClientRect();
                    setDocToSkillTooltip({ left: rect.left + rect.width / 2, top: rect.top });
                  }}
                  onMouseLeave={() => setDocToSkillTooltip(null)}
                  className="w-4 h-4 flex items-center justify-center text-text-muted cursor-help"
                >
                  <HelpCircle className="h-4 w-4" />
                </span>
              </div>
              <input
                type="text"
                value={docToSkillLink}
                onChange={(e) => setDocToSkillLink(e.target.value)}
                placeholder={t('skills.docToSkillModal.linkPlaceholder')}
                className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text"
                style={{ maxWidth: '502px' }}
              />
            </div>
          )}
          {/* 技能描述 */}
          <div className="px-5 pt-4">
            <span className="block text-sm font-medium text-text mb-1.5">{t('skills.docToSkillModal.descLabel')}</span>
            <input
              type="text"
              value={docToSkillDesc}
              onChange={(e) => setDocToSkillDesc(e.target.value)}
              placeholder={t('skills.docToSkillModal.descPlaceholder')}
              className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text"
              style={{ maxWidth: '502px' }}
            />
          </div>
          {/* 底部按钮 */}
          <div className="flex items-center justify-end gap-3 px-5 pt-4 pb-4 bg-panel">
            <button
              type="button"
              onClick={onClose}
              className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
              style={{ height: '32px', padding: '0 32px' }}
            >
              {t('skills.docToSkillModal.cancel')}
            </button>
            <button
              type="button"
              disabled={isDocConfirmDisabled}
              onClick={() => {
                const file = docToSkillFile;
                const link = docToSkillLink;
                const desc = docToSkillDesc;
                onClose();
                onCreateFromKnowledge({
                  file: docToSkillSource === 'local' ? file : null,
                  link: docToSkillSource === 'link' ? link : undefined,
                  skillDescription: desc,
                });
              }}
              className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
                isDocConfirmDisabled
                  ? 'bg-secondary text-text-muted cursor-not-allowed'
                  : 'text-text-inverse bg-control-emphasis hover:opacity-80'
              }`}
              style={{ height: '32px', padding: '0 32px' }}
            >
              {t('skills.docToSkillModal.confirm')}
            </button>
          </div>
        </div>
      </div>
      {docToSkillTooltip && <TopAnchorTooltip pos={docToSkillTooltip} text={t('skills.docToSkillModal.linkTooltip')} />}
    </>
  );
}

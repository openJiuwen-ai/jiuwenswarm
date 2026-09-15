/**
 * 技能发布右侧抽屉
 *
 * 从 index.tsx 抽取，结构与样式保持不变；表单状态来自 usePublishSkill。
 */
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import TipIcon from '../../assets/tip.svg?react';
import UpImgIcon from '../../assets/upImg.svg?react';
import LinkIcon from '../../assets/link.svg?react';
import { ModalCloseButton, FormFieldTooltip } from './SkillPanelWidgets';
import { coerceStringList } from './skillPanelUtils';
import type { PublishSkillState } from './usePublishSkill';
import type { SkillDetail } from './types';

interface PublishDrawerProps {
  selectedSkill: SkillDetail;
  publish: PublishSkillState;
  actionTarget: string | null;
}

export function PublishDrawer({ selectedSkill, publish, actionTarget }: PublishDrawerProps) {
  const { t } = useTranslation();
  const isPublishDisabled = !publish.publishSkillName || !publish.publishVersion || !publish.publishDisplayName;
  return (
    <>
      <div className="fixed inset-0 z-[9998] bg-black/30" onClick={() => publish.setPublishDrawerOpen(false)} />
      <div
        className="fixed top-0 right-0 bottom-0 z-[9999] bg-panel border-l border-border shadow-2xl flex flex-col"
        style={{ width: '550px' }}
        data-testid="skill-panel-publish-drawer"
      >
        {/* 头部（无分割线） */}
        <div className="flex items-center justify-between px-6 pt-4 pb-2 flex-shrink-0">
          <span data-testid="skill-panel-publish-drawer-title" className="text-base font-semibold text-text-strong">
            {t('skills.publishForm.title')}
          </span>
          <ModalCloseButton
            onClick={() => publish.setPublishDrawerOpen(false)}
            label={t('skills.publishForm.cancel')}
            testId="skill-panel-publish-drawer-close-btn"
          />
        </div>
        {/* 提示行（标题下方，可关闭） */}
        {publish.publishNoticeVisible && (
          <div
            className="mx-6 mb-2 flex items-center gap-1.5 rounded-[6px] px-3 text-xs text-text flex-shrink-0 bg-[var(--color-skill-notice-surface)]"
            style={{ height: '34px' }}
          >
            <TipIcon className="w-3.5 h-3.5 shrink-0" />
            <span>{t('skills.publishForm.noticeText')}</span>
            <a
              href={t('skills.publishForm.noticeUrl')}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-0.5 text-chat-accent hover:underline"
            >
              {t('skills.publishForm.noticeView')}
              <LinkIcon className="w-3 h-3" />
            </a>
            <button
              type="button"
              onClick={() => publish.setPublishNoticeVisible(false)}
              className="ml-auto w-5 h-5 flex items-center justify-center rounded hover:bg-accent/10 text-text-muted hover:text-text"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        )}
        {/* 发布错误提示 */}
        {publish.publishError && (
          <div className="mx-6 mt-2 px-3 py-2 rounded-[6px] border border-danger bg-danger/10 text-xs text-danger">
            {publish.publishError}
          </div>
        )}
        {/* 表单内容 */}
        <div className="flex-1 overflow-y-auto px-6 py-2 space-y-4">
          {/* 技能名 */}
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">
              {t('skills.publishForm.skillName')} <span className="text-danger">*</span>
              <FormFieldTooltip text={t('skills.publishForm.skillNameTooltip')} />
            </label>
            <input
              type="text"
              value={publish.publishSkillName}
              onChange={(e) => {
                publish.setPublishSkillName(e.target.value);
                const err = publish.validatePublishSkillName(e.target.value);
                publish.setPublishFieldErrors((prev) => ({ ...prev, skillName: err || '' }));
              }}
              placeholder="my-demo-skill"
              className={`w-full px-3 py-2 rounded-[6px] border bg-panel text-sm text-text ${publish.publishFieldErrors.skillName ? 'border-danger' : 'border-border'}`}
            />
            {publish.publishFieldErrors.skillName && (
              <p className="mt-1 text-xs text-danger">{publish.publishFieldErrors.skillName}</p>
            )}
          </div>
          {/* 版本号 */}
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">
              {t('skills.publishForm.version')} <span className="text-danger">*</span>
            </label>
            <input
              type="text"
              value={publish.publishVersion}
              onChange={(e) => {
                publish.setPublishVersion(e.target.value);
                const err = publish.validatePublishVersion(e.target.value);
                publish.setPublishFieldErrors((prev) => ({ ...prev, version: err || '' }));
              }}
              placeholder="1.0.0"
              className={`w-full px-3 py-2 rounded-[6px] border bg-panel text-sm text-text ${publish.publishFieldErrors.version ? 'border-danger' : 'border-border'}`}
            />
            {publish.publishFieldErrors.version && (
              <p className="mt-1 text-xs text-danger">{publish.publishFieldErrors.version}</p>
            )}
          </div>
          {/* 显示名 */}
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">
              {t('skills.publishForm.displayName')} <span className="text-danger">*</span>
              <FormFieldTooltip text={t('skills.publishForm.displayNameTooltip')} />
            </label>
            <input
              type="text"
              value={publish.publishDisplayName}
              onChange={(e) => {
                publish.setPublishDisplayName(e.target.value);
                const err = publish.validatePublishDisplayName(e.target.value);
                publish.setPublishFieldErrors((prev) => ({ ...prev, displayName: err || '' }));
              }}
              placeholder={t('skills.publishForm.placeholderSelect')}
              className={`w-full px-3 py-2 rounded-[6px] border bg-panel text-sm text-text ${publish.publishFieldErrors.displayName ? 'border-danger' : 'border-border'}`}
            />
            {publish.publishFieldErrors.displayName && (
              <p className="mt-1 text-xs text-danger">{publish.publishFieldErrors.displayName}</p>
            )}
          </div>
          {/* 描述（可选） */}
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">
              {t('skills.publishForm.descriptionOptional')}
              <FormFieldTooltip text={t('skills.publishForm.descriptionTooltip')} />
            </label>
            <textarea
              defaultValue={selectedSkill.description || ''}
              placeholder={t('skills.publishForm.placeholderSelect')}
              className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text min-h-[72px]"
            />
          </div>
          {/* 标签（可选） */}
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">
              {t('skills.publishForm.tagsOptional')}
              <FormFieldTooltip text={t('skills.publishForm.tagsTooltip')} />
            </label>
            <input
              type="text"
              defaultValue={coerceStringList(selectedSkill.tags).join(', ')}
              className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text"
            />
          </div>
          {/* Skill图标（可选）- 图片上传框 */}
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">
              {t('skills.publishForm.skillIconOptional')}
            </label>
            <div
              className="flex items-center justify-center rounded-[6px] border border-dashed border-border bg-secondary/30 cursor-pointer hover:bg-secondary/50"
              style={{ width: '100px', height: '100px' }}
            >
              <UpImgIcon className="w-8 h-8 text-text-muted" />
            </div>
            <span className="block mt-1.5 text-xs text-text-muted">{t('skills.publishForm.skillIconHint')}</span>
          </div>
          {/* SHA-256 校验和 */}
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">{t('skills.publishForm.sha256')}</label>
            <input
              type="text"
              value=""
              readOnly
              placeholder={t('skills.publishForm.placeholderSha256')}
              className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text font-mono opacity-60 cursor-not-allowed"
            />
          </div>
          {/* 版本说明（Swarm Skill，可选） */}
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">
              {t('skills.publishForm.versionNoteOptional')}
            </label>
            <textarea
              value={publish.publishVersionDesc}
              onChange={(e) => publish.setPublishVersionDesc(e.target.value)}
              className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text min-h-[72px]"
            />
          </div>
          {/* 强制覆盖 */}
          <div>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={publish.publishForce}
                onChange={(e) => publish.setPublishForce(e.target.checked)}
                className="cursor-pointer"
              />
              <span className="text-sm text-text">{t('skills.publishForm.forceOverwrite')}</span>
            </label>
          </div>
        </div>
        {/* 底部按钮 */}
        <div className="flex items-center justify-end gap-3 px-6 pb-4 pt-2 flex-shrink-0">
          <button
            type="button"
            onClick={() => publish.setPublishDrawerOpen(false)}
            className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
            style={{ height: '32px', padding: '0 32px' }}
          >
            {t('skills.publishForm.cancel')}
          </button>
          <button
            type="button"
            disabled={isPublishDisabled || actionTarget === 'publish'}
            onClick={() => publish.handlePublish()}
            className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
              isPublishDisabled || actionTarget === 'publish'
                ? 'bg-secondary text-text-muted cursor-not-allowed'
                : 'text-text-inverse bg-control-emphasis hover:opacity-80'
            }`}
            style={{ height: '32px', padding: '0 32px' }}
          >
            {actionTarget === 'publish' ? t('common.processing') : t('skills.publishForm.publish')}
          </button>
        </div>
      </div>
    </>
  );
}

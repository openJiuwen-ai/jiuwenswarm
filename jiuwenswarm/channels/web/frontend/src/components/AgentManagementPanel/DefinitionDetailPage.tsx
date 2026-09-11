import { useTranslation } from 'react-i18next';
import {
  getAgentAvatarUrl,
  type AgentDetail,
  type DefinitionFileEntry,
  type RequestStatus,
} from '../../features/agentManagement';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import PromptSendIcon from '../../assets/agent-management/prompt-send.svg?react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import { DetailPromptChip, DetailSection, EntityHeader, MarkdownPane, PageToolbar, Tabs } from '../ui';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { DefinitionFilePreview } from './DefinitionFilePreview';

type DefinitionDetailPageProps = {
  detail: AgentDetail | null;
  detailStatus: RequestStatus;
  detailError: string | null;
  detailTab: 'content' | 'files';
  files: DefinitionFileEntry[];
  filesStatus: RequestStatus;
  filesError: string | null;
  selectedFilePath: string | null;
  fileContent: { relativePath: string; content: string } | null;
  fileStatus: RequestStatus;
  fileError: string | null;
  actionError: string | null;
  actionNotice: string | null;
  busy: boolean;
  onBack: () => void;
  onRetry: () => void;
  onTabChange: (tab: 'content' | 'files') => void;
  onRetryFiles: () => void;
  onSelectFile: (path: string) => void;
  onUse: (id: string) => void;
  onUsePrompt?: (id: string, prompt: string) => void;
  onReconnect: (id: string) => void;
  onInstall: (id: string) => void;
  onUninstall: (id: string) => void;
};

export function DefinitionDetailPage({
  detail,
  detailStatus,
  detailError,
  detailTab,
  files,
  filesStatus,
  filesError,
  selectedFilePath,
  fileContent,
  fileStatus,
  fileError,
  actionError,
  actionNotice,
  busy,
  onBack,
  onRetry,
  onTabChange,
  onRetryFiles,
  onSelectFile,
  onUse,
  onUsePrompt,
  onReconnect,
  onInstall,
  onUninstall,
}: DefinitionDetailPageProps) {
  const { t } = useTranslation();

  if (detailStatus === 'loading' && !detail) {
    return (
      <div
        className="agent-management-detail agent-management-detail--state"
        data-testid="agent-management-detail-state"
        data-variant="loading"
      >
        <button type="button" className="detail-back" onClick={onBack}>
          <BackIcon aria-hidden="true" />
          {t('agentManagement.actions.back')}
        </button>
        <p>{t('common.loading')}</p>
      </div>
    );
  }
  if (!detail) {
    return (
      <div
        className="agent-management-detail agent-management-detail--state agent-management-state--error"
        role="alert"
        data-testid="agent-management-detail-state"
        data-variant="error"
      >
        <button type="button" className="detail-back" onClick={onBack}>
          <BackIcon aria-hidden="true" />
          {t('agentManagement.actions.back')}
        </button>
        <p>{detailError || t('agentManagement.states.detailError')}</p>
        <button type="button" className="agent-management-button agent-management-button--secondary" onClick={onRetry}>
          {t('common.retry')}
        </button>
      </div>
    );
  }

  const avatarUrl = getAgentAvatarUrl(detail);
  const canUse = detail.installed && detail.connectionState === 'connected' && detail.enabled !== false;
  const needsConnection = detail.installed && detail.connectionState !== 'connected';
  const canDelete = detail.source === 'local' && !detail.installed;
  const capabilityGroups = [
    { title: t('agentManagement.detail.tags'), items: detail.tags.map((tag) => ({ id: tag.id, name: tag.label })) },
    { title: t('agentManagement.detail.skills'), items: detail.skills },
    { title: t('agentManagement.detail.tools'), items: detail.tools },
    { title: t('agentManagement.detail.rails'), items: detail.rails },
    { title: t('agentManagement.detail.mcps'), items: detail.mcps },
  ].filter((group) => group.items.length > 0);
  return (
    <div className="agent-management-detail" data-testid="agent-management-detail">
      <button type="button" className="detail-back" onClick={onBack} data-testid="agent-management-detail-back-btn">
        <BackIcon aria-hidden="true" />
        {t('agentManagement.actions.back')}
      </button>
      <div className="detail-body flex-1 min-h-0 overflow-y-auto">
        <EntityHeader
          testId="agent-management-detail-header"
          avatar={avatarUrl ? <img src={avatarUrl} alt="" /> : getSkillAvatar(detail.displayName)}
          title={detail.displayName}
          titleTestId="agent-management-detail-name"
          tags={[
            t(`agentManagement.categories.${detail.category}`, {
              defaultValue: detail.category || t('agentManagement.categoryOther'),
            }),
            t('agentManagement.detail.sourcePrefix', {
              source: t(`agentManagement.source.${detail.source}`),
            }),
            ...(detail.installed ? [t('agentManagement.states.installed')] : []),
          ]}
          actions={
            <div className="agent-management-detail__actions">
              {detail.installed ? (
                <>
                  {needsConnection ? (
                    <button
                      type="button"
                      className="agent-management-button agent-management-button--secondary"
                      disabled={busy}
                      aria-busy={busy}
                      onClick={() => onReconnect(detail.id)}
                      data-testid="agent-management-detail-connect-btn"
                    >
                      {busy ? t('agentManagement.actions.connecting') : t('agentManagement.actions.connect')}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="agent-management-detail-action agent-management-detail-action--uninstall"
                    disabled={busy}
                    aria-busy={busy}
                    onClick={() => onUninstall(detail.id)}
                    data-testid="agent-management-detail-uninstall-btn"
                  >
                    <UninstallIcon aria-hidden="true" />
                    {busy ? t('agentManagement.actions.uninstalling') : t('agentManagement.actions.uninstall')}
                  </button>
                  <button
                    type="button"
                    className="agent-management-button agent-management-button--secondary agent-management-detail-action--use"
                    disabled={!canUse || busy}
                    aria-disabled={!canUse}
                    onClick={() => onUse(detail.id)}
                    data-testid="agent-management-detail-use-btn"
                  >
                    {t('agentManagement.actions.use')}
                  </button>
                </>
              ) : (
                <>
                  {canDelete ? (
                    <button
                      type="button"
                      className="agent-management-detail-action agent-management-detail-action--uninstall"
                      disabled={busy}
                      aria-busy={busy}
                      onClick={() => onUninstall(detail.id)}
                      data-testid="agent-management-detail-delete-btn"
                    >
                      <UninstallIcon aria-hidden="true" />
                      {busy ? t('agentManagement.actions.deleting') : t('agentManagement.actions.delete')}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="agent-management-button agent-management-button--primary agent-management-detail-action--install"
                    disabled={busy}
                    aria-busy={busy}
                    onClick={() => onInstall(detail.id)}
                    data-testid="agent-management-detail-install-btn"
                  >
                    {busy ? t('agentManagement.actions.installing') : t('agentManagement.actions.install')}
                  </button>
                </>
              )}
            </div>
          }
        />
        {actionError ? (
          <div
            className="agent-management-inline-error"
            role="alert"
            data-testid="agent-management-detail-action-error"
          >
            {actionError}
          </div>
        ) : null}

        {actionNotice ? (
          <div
            className="agent-management-inline-notice"
            role="status"
            data-testid="agent-management-detail-action-notice"
          >
            {actionNotice}
          </div>
        ) : null}

        {detailStatus === 'error' ? (
          /* 详情加载失败：与 skill 详情一致的内联灰字条（不加边框底色） */
          <div
            className="text-sm text-text-muted"
            role="status"
            data-testid="agent-management-detail-state"
            data-variant="error"
          >
            {detailError || t('agentManagement.states.detailError')}
          </div>
        ) : null}

        {needsConnection ? (
          <div
            className="agent-management-connection-warning"
            role="status"
            data-testid="agent-management-detail-connection-warning"
          >
            {t('agentManagement.states.connectionUnavailable')}
          </div>
        ) : null}

        <DetailSection testId="agent-management-detail-ability" title={t('agentManagement.detail.ability')}>
          <p>{detail.description || t('agentManagement.unknownDescription')}</p>
        </DetailSection>

        {capabilityGroups.map((group) => (
          <DetailSection key={group.title} title={group.title}>
            <div className="detail-chip-row">
              {group.items.map((item) => (
                <span key={item.id} className="detail-chip">
                  {item.name}
                </span>
              ))}
            </div>
          </DetailSection>
        ))}

        {detail.suggestedPrompts.length > 0 ? (
          <DetailSection testId="agent-management-detail-prompts" title={t('agentManagement.detail.quickInputs')}>
            {/* 共享组件 ui/DetailPromptChip：文案+发送图标两端对齐、每项独占一行（容器
                .detail-prompt-list），与连接器详情"试试这样用"示例同款。整行是一个 button：
                点击行内任意位置（含文案）都跳会话预填 prompt。图标降级为纯装饰 span——
                button 内不允许嵌套 button。testid/variant 保持不变。 */}
            <div className="detail-prompt-list">
              {detail.suggestedPrompts.map((prompt, index) => (
                <DetailPromptChip
                  key={prompt}
                  text={prompt}
                  icon={<PromptSendIcon width={16} height={16} />}
                  disabled={!canUse || busy || !onUsePrompt}
                  onClick={() => onUsePrompt?.(detail.runtimePackageName, prompt)}
                  testId="agent-management-detail-prompt-send"
                  variant={index}
                />
              ))}
            </div>
          </DetailSection>
        ) : null}

        <div data-testid="agent-management-detail-tabs-section" className="flex flex-col min-h-0">
          <PageToolbar style={{ marginTop: 0, flexShrink: 0 }}>
            <Tabs
              role="tablist"
              ariaLabel={t('agentManagement.detail.tabsLabel')}
              wrapperTestId="agent-management-detail-tabs"
              itemTestId="agent-management-detail-tab"
              className="text-base"
              value={detailTab}
              onChange={onTabChange}
              items={[
                { value: 'content', label: t('agentManagement.detail.contentTab') },
                { value: 'files', label: t('agentManagement.detail.filesTab') },
              ]}
            />
          </PageToolbar>
          {detailTab === 'content' ? (
            <MarkdownPane
              testId="agent-management-detail-content"
              content={detail.details || null}
              emptyText={t('skills.noContent')}
            />
          ) : (
            <DefinitionFilePreview
              files={files}
              filesStatus={filesStatus}
              filesError={filesError}
              selectedFilePath={selectedFilePath}
              fileContent={fileContent}
              fileStatus={fileStatus}
              fileError={fileError}
              onRetryFiles={onRetryFiles}
              onSelectFile={onSelectFile}
            />
          )}
        </div>
      </div>
    </div>
  );
}

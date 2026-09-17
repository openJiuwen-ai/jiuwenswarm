import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { AgentGroupDetail, DefinitionFileEntry, RequestStatus } from '../../features/agentManagement';
import { DefinitionFilePreview } from './DefinitionFilePreview';
import { getAvatarTone, GroupAvatar } from './GroupCard';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import PromptSendIcon from '../../assets/agent-management/prompt-send.svg?react';
import { DetailPromptChip, DetailSection, EntityHeader, MarkdownPane, PageToolbar, Tabs } from '../ui';

type AgentGroupDetailPageProps = {
  detail: AgentGroupDetail | null;
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
  onInstall: (id: string) => void;
  onUninstall: (id: string) => void;
};

export function AgentGroupDetailPage({
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
  onInstall,
  onUninstall,
}: AgentGroupDetailPageProps) {
  const { t } = useTranslation();
  const [imageFailed, setImageFailed] = useState<Record<string, boolean>>({});
  if (detailStatus === 'loading')
    return (
      <div className="agent-management-detail agent-management-detail--state" data-testid="agent-group-detail">
        <button type="button" className="detail-back" data-testid="agent-group-detail-back" onClick={onBack}>
          <BackIcon aria-hidden="true" />
          {t('agentManagement.actions.back')}
        </button>
        <p>{t('common.loading')}</p>
      </div>
    );
  if (detailStatus === 'error' || !detail)
    return (
      <div
        className="agent-management-detail agent-management-detail--state agent-management-state--error"
        role="alert"
        data-testid="agent-group-detail"
      >
        <button type="button" className="detail-back" data-testid="agent-group-detail-back" onClick={onBack}>
          <BackIcon aria-hidden="true" />
          {t('agentManagement.actions.back')}
        </button>
        <p>{detailError || t('agentManagement.group.states.detailError')}</p>
        <button
          type="button"
          className="agent-management-button agent-management-button--secondary"
          data-testid="agent-group-detail-retry"
          onClick={onRetry}
        >
          {t('common.retry')}
        </button>
      </div>
    );

  const canUse = detail.installed && detail.capabilities.canUse;
  const canDelete = detail.source === 'local' && !detail.installed;
  const canPreviewFiles = detail.capabilities.canPreviewFiles && (detail.source === 'local' || detail.installed);
  return (
    <div className="agent-management-detail agent-group-detail" data-testid="agent-group-detail">
      <button type="button" className="detail-back" data-testid="agent-group-detail-back" onClick={onBack}>
        <BackIcon aria-hidden="true" />
        {t('agentManagement.actions.back')}
      </button>
      <div className="detail-body flex-1 min-h-0 overflow-y-auto">
        <EntityHeader
          testId="agent-management-detail-header"
          avatar={<GroupAvatar item={detail} size="detail" />}
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
              {detail.installed && detail.capabilities.canUninstall ? (
                <button
                  type="button"
                  className="agent-management-detail-action agent-management-detail-action--uninstall"
                  data-testid="agent-group-detail-action"
                  data-variant="uninstall"
                  disabled={busy}
                  aria-busy={busy}
                  onClick={() => onUninstall(detail.id)}
                >
                  <UninstallIcon aria-hidden="true" />
                  {busy
                    ? t('agentManagement.group.actions.uninstalling')
                    : t('agentManagement.group.actions.uninstall')}
                </button>
              ) : null}
              {detail.installed ? (
                <button
                  type="button"
                  className="agent-management-button agent-management-button--secondary agent-management-detail-action--use"
                  data-testid="agent-group-detail-action"
                  data-variant="use"
                  disabled={!canUse || busy}
                  aria-disabled={!canUse}
                  onClick={() => onUse(detail.id)}
                >
                  {t('agentManagement.group.actions.use')}
                </button>
              ) : (
                <>
                  {canDelete ? (
                    <button
                      type="button"
                      className="agent-management-detail-action agent-management-detail-action--uninstall"
                      data-testid="agent-group-detail-action"
                      data-variant="delete"
                      disabled={busy}
                      aria-busy={busy}
                      onClick={() => onUninstall(detail.id)}
                    >
                      <UninstallIcon aria-hidden="true" />
                      {busy ? t('agentManagement.actions.deleting') : t('agentManagement.actions.delete')}
                    </button>
                  ) : null}
                  {detail.capabilities.canInstall ? (
                    <button
                      type="button"
                      className="agent-management-button agent-management-button--primary agent-management-detail-action--install"
                      data-testid="agent-group-detail-action"
                      data-variant="install"
                      disabled={busy}
                      aria-busy={busy}
                      onClick={() => onInstall(detail.id)}
                    >
                      {busy
                        ? t('agentManagement.group.actions.installing')
                        : t('agentManagement.group.actions.install')}
                    </button>
                  ) : null}
                </>
              )}
            </div>
          }
        />
        {actionError ? (
          <div className="agent-management-inline-error" role="alert">
            {actionError}
          </div>
        ) : null}
        {actionNotice ? (
          <div className="agent-management-inline-notice" role="status">
            {actionNotice}
          </div>
        ) : null}
        <DetailSection testId="agent-management-detail-ability" title={t('agentManagement.detail.ability')}>
          <p>{detail.description || t('agentManagement.unknownDescription')}</p>
        </DetailSection>
        <DetailSection
          className="agent-group-detail__members-section"
          title={t('agentManagement.group.detail.membersTitle')}
        >
          <div className="agent-group-detail__members" data-testid="agent-group-detail-members">
            {detail.members.map((member) => (
              <article
                className="agent-group-member-card"
                data-testid="agent-group-detail-member"
                data-variant={member.id}
                key={member.id}
              >
                <div className="agent-group-member-card__avatar-wrap">
                  <span
                    className={`agent-group-member-avatar agent-group-member-avatar--${getAvatarTone(member.displayName)}${imageFailed[member.id] ? ' is-fallback' : ''}`}
                  >
                    {member.avatarUrl && !imageFailed[member.id] ? (
                      <img
                        src={member.avatarUrl}
                        alt=""
                        onError={() => setImageFailed((current) => ({ ...current, [member.id]: true }))}
                      />
                    ) : (
                      member.displayName.slice(0, 1).toUpperCase()
                    )}
                  </span>
                </div>
                <div className="agent-group-member-card__identity">
                  <strong title={member.displayName}>{member.displayName}</strong>
                  {member.role === 'leader' ? (
                    <span
                      className="agent-group-member-card__badge"
                      data-testid="agent-management-group-member-leader-badge"
                    >
                      {t('agentManagement.group.detail.leader')}
                    </span>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
        </DetailSection>
        {detail.tags.length > 0 || detail.skills.length > 0 ? (
          <>
            {detail.tags.length > 0 ? (
              <DetailSection key="tags" title={t('agentManagement.detail.tags')}>
                <div className="detail-chip-row">
                  {detail.tags.map((tag) => (
                    <span key={tag.id} className="detail-chip">
                      {tag.label}
                    </span>
                  ))}
                </div>
              </DetailSection>
            ) : null}
            {detail.skills.length > 0 ? (
              <DetailSection key="skills" title={t('agentManagement.detail.skills')}>
                <div className="detail-chip-row">
                  {detail.skills.map((skill) => (
                    <span key={skill.id} className="detail-chip">
                      {skill.name}
                    </span>
                  ))}
                </div>
              </DetailSection>
            ) : null}
          </>
        ) : null}
        {detail.quickInputs.length > 0 ? (
          <DetailSection title={t('agentManagement.detail.quickInputs')}>
            <div className="detail-prompt-list">
              {detail.quickInputs.map((prompt, index) => (
                <DetailPromptChip
                  key={`${index}-${prompt}`}
                  text={prompt}
                  icon={<PromptSendIcon width={16} height={16} />}
                  disabled={!canUse || busy || !onUsePrompt}
                  onClick={() => onUsePrompt?.(detail.id, prompt)}
                  testId="agent-group-detail-prompt-send"
                  variant={prompt}
                />
              ))}
            </div>
          </DetailSection>
        ) : null}
        <div data-testid="agent-group-detail-tabs-section" className="flex flex-col min-h-0">
          <PageToolbar style={{ marginTop: 0, flexShrink: 0 }}>
            <Tabs
              role="tablist"
              ariaLabel={t('agentManagement.group.detail.tabsLabel')}
              wrapperTestId="agent-group-detail-tabs"
              itemTestId="agent-group-detail-tab"
              className="text-base"
              value={detailTab}
              onChange={onTabChange}
              items={[
                {
                  value: 'content',
                  label: t('agentManagement.group.detail.contentTab'),
                  testId: 'agent-group-detail-content-tab',
                },
                {
                  value: 'files',
                  label: t('agentManagement.group.detail.filesTab'),
                  testId: 'agent-group-detail-files-tab',
                },
              ]}
            />
          </PageToolbar>
          {detailTab === 'content' ? (
            <MarkdownPane
              testId="agent-group-detail-content"
              content={detail.details || null}
              emptyText={t('agentManagement.group.detail.noDetails')}
            />
          ) : canPreviewFiles ? (
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
          ) : (
            <div className="agent-management-file-preview agent-management-file-preview--unavailable">
              <div className="agent-management-file-state">{t('agentManagement.group.detail.filesUnavailable')}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

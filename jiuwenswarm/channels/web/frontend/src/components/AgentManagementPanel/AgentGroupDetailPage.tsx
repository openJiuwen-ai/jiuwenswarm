import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { AgentGroupDetail, DefinitionFileEntry, RequestStatus } from '../../features/agentManagement';
import { DefinitionFilePreview } from './DefinitionFilePreview';
import { getAvatarTone, GroupAvatar } from './GroupCard';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import PromptSendIcon from '../../assets/agent-management/prompt-send.svg?react';
import { MarkdownPane } from '../ui';

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
      <button type="button" className="detail-back mb-[35px]" data-testid="agent-group-detail-back" onClick={onBack}>
        <BackIcon aria-hidden="true" />
        {t('agentManagement.actions.back')}
      </button>
      <div className="detail-body flex-1 min-h-0 overflow-y-auto pb-[72px]">
        <header className="agent-management-detail__header">
          <div className="agent-management-detail__identity">
            <GroupAvatar item={detail} size="detail" />
            <div>
              <h1 title={detail.displayName}>{detail.displayName}</h1>
              <div className="agent-management-detail__badges">
                <span className="agent-management-tag">
                  {t(`agentManagement.categories.${detail.category}`, {
                    defaultValue: detail.category || t('agentManagement.categoryOther'),
                  })}
                </span>
                <span className="agent-management-source">
                  {t('agentManagement.detail.sourcePrefix', {
                    source:
                      detail.source === 'builtin'
                        ? t('agentManagement.source.builtin')
                        : t('agentManagement.source.local'),
                  })}
                </span>
                {detail.installed ? (
                  <span className="agent-management-installed">{t('agentManagement.states.installed')}</span>
                ) : null}
              </div>
            </div>
          </div>
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
                {busy ? t('agentManagement.group.actions.uninstalling') : t('agentManagement.group.actions.uninstall')}
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
                    {busy ? t('agentManagement.group.actions.installing') : t('agentManagement.group.actions.install')}
                  </button>
                ) : null}
              </>
            )}
          </div>
        </header>
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
        <section className="agent-management-detail-section">
          <h2>{t('agentManagement.detail.ability')}</h2>
          <p className="agent-management-detail-description">
            {detail.description || t('agentManagement.unknownDescription')}
          </p>
        </section>
        <section className="agent-management-detail-section agent-group-detail__members-section">
          <h2>{t('agentManagement.group.detail.membersTitle')}</h2>
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
        </section>
        {detail.tags.length > 0 || detail.skills.length > 0 ? (
          <div className="agent-management-detail-capabilities agent-group-detail__capabilities">
            {detail.tags.length > 0 ? (
              <section className="agent-management-detail-capability-group">
                <h2>{t('agentManagement.detail.tags')}</h2>
                <div className="agent-management-chip-row">
                  {detail.tags.map((tag) => (
                    <span key={tag.id} className="agent-management-chip">
                      {tag.label}
                    </span>
                  ))}
                </div>
              </section>
            ) : null}
            {detail.skills.length > 0 ? (
              <section className="agent-management-detail-capability-group">
                <h2>{t('agentManagement.detail.skills')}</h2>
                <div className="agent-management-chip-row">
                  {detail.skills.map((skill) => (
                    <span key={skill.id} className="agent-management-chip">
                      {skill.name}
                    </span>
                  ))}
                </div>
              </section>
            ) : null}
          </div>
        ) : null}
        {detail.quickInputs.length > 0 ? (
          <section className="agent-management-detail-section agent-management-detail-section--prompts">
            <h2>{t('agentManagement.detail.quickInputs')}</h2>
            <div className="agent-management-prompt-list">
              {detail.quickInputs.map((prompt, index) => (
                <div key={`${index}-${prompt}`} className="agent-management-prompt">
                  <span>{prompt}</span>
                  <button
                    type="button"
                    className="agent-management-prompt__send"
                    data-testid="agent-group-detail-prompt-send"
                    data-variant={prompt}
                    aria-label={t('agentManagement.detail.usePrompt', { prompt })}
                    disabled={!canUse || busy || !onUsePrompt}
                    onClick={() => onUsePrompt?.(detail.id, prompt)}
                  >
                    <PromptSendIcon width={16} height={16} aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
          </section>
        ) : null}
        <div
          className="agent-management-detail-tabs"
          role="tablist"
          aria-label={t('agentManagement.group.detail.tabsLabel')}
        >
          <button
            type="button"
            role="tab"
            aria-selected={detailTab === 'content'}
            data-testid="agent-group-detail-content-tab"
            className={detailTab === 'content' ? 'is-active' : ''}
            onClick={() => onTabChange('content')}
          >
            {t('agentManagement.group.detail.contentTab')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={detailTab === 'files'}
            data-testid="agent-group-detail-files-tab"
            className={detailTab === 'files' ? 'is-active' : ''}
            onClick={() => onTabChange('files')}
          >
            {t('agentManagement.group.detail.filesTab')}
          </button>
        </div>
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
  );
}

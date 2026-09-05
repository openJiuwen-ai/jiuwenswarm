import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useTranslation } from 'react-i18next';
import { getAgentAvatarUrl, type AgentCapability, type AgentDetail, type DefinitionFileEntry, type RequestStatus } from '../../features/agentManagement';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import PromptSendIcon from '../../assets/agent-management/prompt-send.svg?react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
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

function Avatar({ name, avatarUrl }: { name: string; avatarUrl: string | null }) {
  return (
    <span className="agent-management-avatar agent-management-avatar--detail" aria-hidden="true">
      {avatarUrl ? <img src={avatarUrl} alt="" /> : <span className="agent-management-avatar__letter">{name.trim().slice(0, 1).toUpperCase() || '?'}</span>}
    </span>
  );
}

function ChipList({ title, items }: { title: string; items: Array<{ id: string; name: string }> }) {
  if (items.length === 0) return null;
  return (
    <section className="agent-management-detail-capability-group">
      <h2>{title}</h2>
      <div className="agent-management-chip-row">
        {items.map(item => (
          <span key={item.id} className="agent-management-chip">
            {item.name}
          </span>
        ))}
      </div>
    </section>
  );
}

function CapabilityList({ title, items }: { title: string; items: AgentCapability[] }) {
  if (items.length === 0) return null;
  return (
    <section className="agent-management-detail-capability-group">
      <h2>{title}</h2>
      <div className="agent-management-chip-row">
        {items.map(item => (
          <span key={item.id} className="agent-management-chip">
            {item.name}
          </span>
        ))}
      </div>
    </section>
  );
}

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

  if (detailStatus === 'loading') {
    return (
      <div className="agent-management-detail agent-management-detail--state">
        <button type="button" className="detail-back" onClick={onBack}>
          <BackIcon aria-hidden="true" />
          {t('agentManagement.actions.back')}
        </button>
        <p>{t('common.loading')}</p>
      </div>
    );
  }
  if (detailStatus === 'error' || !detail) {
    return (
      <div className="agent-management-detail agent-management-detail--state agent-management-state--error" role="alert">
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
  const canPreviewFiles = detail.source === 'local' || detail.installed;
  return (
    <div className="agent-management-detail" data-testid="agent-detail">
      <button type="button" className="detail-back mb-[35px]" onClick={onBack}>
        <BackIcon aria-hidden="true" />
        {t('agentManagement.actions.back')}
      </button>
      <div className="detail-body flex-1 min-h-0 overflow-y-auto pb-[72px]">
      <header className="agent-management-detail__header">
        <div className="agent-management-detail__identity">
          <Avatar name={detail.displayName} avatarUrl={avatarUrl} />
          <div>
            <h1 title={detail.displayName}>{detail.displayName}</h1>
            <div className="agent-management-detail__badges">
              <span className="agent-management-tag">
                {t(`agentManagement.categories.${detail.category}`, { defaultValue: detail.category || t('agentManagement.categoryOther') })}
              </span>
              <span className="agent-management-source">
                {t('agentManagement.detail.sourcePrefix', {
                  source: detail.source === 'builtin' ? t('agentManagement.source.builtin') : t('agentManagement.source.local'),
                })}
              </span>
              {detail.installed ? <span className="agent-management-installed">{t('agentManagement.states.installed')}</span> : null}
            </div>
          </div>
        </div>
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
              >
                {busy ? t('agentManagement.actions.installing') : t('agentManagement.actions.install')}
              </button>
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

        {needsConnection ? (
          <div className="agent-management-connection-warning" role="status">
            {t('agentManagement.states.connectionUnavailable')}
          </div>
        ) : null}

        <section className="agent-management-detail-section">
          <h2>{t('agentManagement.detail.ability')}</h2>
          <p className="agent-management-detail-description">{detail.description || t('agentManagement.unknownDescription')}</p>
        </section>

        <div className="agent-management-detail-capabilities">
          <ChipList title={t('agentManagement.detail.tags')} items={detail.tags.map(tag => ({ id: tag.id, name: tag.label }))} />
          <CapabilityList title={t('agentManagement.detail.skills')} items={detail.skills} />
          <CapabilityList title={t('agentManagement.detail.tools')} items={detail.tools} />
          <CapabilityList title={t('agentManagement.detail.rails')} items={detail.rails} />
          <CapabilityList title={t('agentManagement.detail.mcps')} items={detail.mcps} />
        </div>

        {detail.suggestedPrompts.length > 0 ? (
          <section className="agent-management-detail-section agent-management-detail-section--prompts">
            <h2>{t('agentManagement.detail.quickInputs')}</h2>
            <div className="agent-management-prompt-list">
              {detail.suggestedPrompts.map(prompt => (
                <div key={prompt} className="agent-management-prompt">
                  <span>{prompt}</span>
                  <button
                    type="button"
                    className="agent-management-prompt__send"
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

        <div className="agent-management-detail-tabs" role="tablist" aria-label={t('agentManagement.detail.tabsLabel')}>
          <button
            type="button"
            role="tab"
            aria-selected={detailTab === 'content'}
            className={detailTab === 'content' ? 'is-active' : ''}
            onClick={() => onTabChange('content')}
          >
            {t('agentManagement.detail.contentTab')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={detailTab === 'files'}
            className={detailTab === 'files' ? 'is-active' : ''}
            onClick={() => onTabChange('files')}
          >
            {t('agentManagement.detail.filesTab')}
          </button>
        </div>
        {detailTab === 'content' ? (
          <article className="agent-management-detail-content prose prose-sm">
            {detail.details ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{detail.details}</ReactMarkdown> : null}
          </article>
        ) : !canPreviewFiles ? (
          <div className="agent-management-file-preview agent-management-file-preview--unavailable">
            <div className="agent-management-file-state">{t('agentManagement.detail.filesUnavailable')}</div>
          </div>
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
  );
}

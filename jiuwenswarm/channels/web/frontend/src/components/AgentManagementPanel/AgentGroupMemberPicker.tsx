import { createPortal } from 'react-dom';
import { useMemo, useRef, useState, type RefObject } from 'react';
import { Check, LoaderCircle, Search, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  getAgentAvatarUrl,
  isAgentGroupAgentCompatibilityLoading,
  isAgentGroupAgentSelectable,
  resolveAgentGroupSelectionId,
  sortAgentGroupOptions,
  type AgentCatalogItem,
  type RequestStatus,
} from '../../features/agentManagement';
import { PageCard, Tabs } from '../ui';
import { SelectionPagination, useSelectionPagination } from './SelectionPagination';
import { useDialogFocusTrap } from './useDialogFocusTrap';

export function AgentOptionAvatar({ agent }: { agent: AgentCatalogItem }) {
  const [imageFailed, setImageFailed] = useState(false);
  const avatarUrl = agent.avatarUrl && !imageFailed ? agent.avatarUrl : null;
  return (
    <span className="agent-management-capability-card__icon agent-management-selection-avatar" aria-hidden="true">
      {avatarUrl ? (
        <img src={avatarUrl} alt="" onError={() => setImageFailed(true)} />
      ) : (
        <span>{agent.displayName.trim().slice(0, 1).toUpperCase() || '?'}</span>
      )}
    </span>
  );
}

type AgentGroupMemberPickerProps = {
  mode: 'leader' | 'member';
  agents: AgentCatalogItem[];
  agentsStatus: RequestStatus;
  agentsError: string | null;
  selectedLeaderId: string;
  selectedMemberIds: string[];
  onCancel: () => void;
  onConfirm: (ids: string[]) => void;
  onReloadAgents: () => void;
  selectionError?: string | null;
  onInstallAgent?: (id: string) => void | Promise<void>;
  installingAgentId?: string | null;
  restoreFocusRef?: RefObject<HTMLElement | null>;
};

export function AgentGroupMemberPicker({
  mode,
  agents,
  agentsStatus,
  agentsError,
  selectedLeaderId,
  selectedMemberIds,
  onCancel,
  onConfirm,
  onReloadAgents,
  selectionError,
  onInstallAgent,
  installingAgentId,
  restoreFocusRef,
}: AgentGroupMemberPickerProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [sourceTab, setSourceTab] = useState<'local' | 'market'>('market');
  const [selection, setSelection] = useState<string[]>(
    mode === 'leader' ? (selectedLeaderId ? [selectedLeaderId] : []) : selectedMemberIds,
  );
  const dialogRef = useRef<HTMLElement>(null);
  const filteredAgents = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    const sourceAgents = agents.filter((agent) =>
      sourceTab === 'market' ? agent.source !== 'local' : agent.source === 'local' || agent.installed === true,
    );
    return sortAgentGroupOptions(sourceAgents, agentsStatus).filter((agent) => {
      if (!normalized) return true;
      return `${agent.id} ${agent.runtimePackageName} ${agent.displayName} ${agent.description} ${agent.category} ${agent.tags.map((tag) => tag.label).join(' ')}`
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [agents, agentsStatus, query, sourceTab]);
  const {
    pageItems: pageAgents,
    page,
    totalPages,
    setPage,
  } = useSelectionPagination(filteredAgents, `${sourceTab}\0${query}`);

  useDialogFocusTrap({ dialogRef, restoreFocusRef, onEscape: onCancel });

  const toggle = (id: string, legacyId?: string) => {
    if (mode === 'leader') {
      setSelection([id]);
      return;
    }
    setSelection((current) => {
      const selected = current.includes(id) || (legacyId ? current.includes(legacyId) : false);
      if (selected) {
        return current.filter((item) => item !== id && item !== legacyId);
      }
      return [...current, id];
    });
  };

  const title =
    mode === 'leader' ? t('agentManagement.group.picker.leaderTitle') : t('agentManagement.group.picker.memberTitle');
  const selectedCount = selection.length;

  return createPortal(
    <div
      className="agent-management-selection-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <section
        ref={dialogRef}
        className="agent-management-selection-dialog"
        data-testid="agent-group-member-picker"
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-group-picker-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <h2 id="agent-group-picker-title">{title}</h2>
          <button
            type="button"
            aria-label={t('common.close')}
            data-testid="agent-group-member-picker-close"
            onClick={onCancel}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <label className="agent-management-selection-search">
          <Search size={16} aria-hidden="true" />
          <input
            type="search"
            data-testid="agent-group-member-picker-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
            autoFocus
          />
        </label>
        <Tabs
          className="agent-management-selection-source-tabs"
          items={[
            {
              value: 'market',
              label: t('agentManagement.tabs.catalog'),
              testId: 'agent-group-member-picker-tab-market',
            },
            {
              value: 'local',
              label: t('agentManagement.tabs.mine'),
              testId: 'agent-group-member-picker-tab-local',
            },
          ]}
          value={sourceTab}
          onChange={(value) => {
            setSourceTab(value);
            setPage(1);
          }}
          wrapperTestId="agent-group-member-picker-tabs"
          role="tablist"
          ariaLabel={t('agentManagement.group.picker.sourceTabsLabel')}
        />
        {selectionError ? (
          <div
            className="agent-management-form-error"
            role="alert"
            data-testid="agent-group-member-picker-selection-error"
          >
            {selectionError}
          </div>
        ) : null}
        <div
          className={`agent-management-selection-dialog__body${filteredAgents.length === 0 ? ' is-empty' : ''}`}
          role="group"
          aria-label={title}
        >
          {agentsStatus === 'error' ? (
            <div className="agent-management-form-error" role="alert" data-testid="agent-group-member-picker-error">
              <span>{agentsError || t('agentManagement.states.loadError')}</span>
              <button type="button" onClick={onReloadAgents}>
                {t('common.retry')}
              </button>
            </div>
          ) : agentsStatus === 'loading' && agents.length === 0 ? (
            <div className="agent-management-selection-empty-state is-loading" role="status" aria-live="polite">
              <LoaderCircle className="agent-management-selection-card__loading-icon" size={20} aria-hidden="true" />
              <p>{t('agentManagement.group.picker.loading')}</p>
            </div>
          ) : filteredAgents.length === 0 ? (
            <div className="agent-management-selection-empty-state">
              <p>{t('agentManagement.group.picker.empty')}</p>
            </div>
          ) : (
            <div className="agent-management-selection-grid">
              {pageAgents.map((agent) => {
                const selectionId = resolveAgentGroupSelectionId(agent);
                const selected = selection.includes(selectionId) || selection.includes(agent.id);
                const compatibilityLoading = isAgentGroupAgentCompatibilityLoading(agent, agentsStatus);
                const selectable = !compatibilityLoading && isAgentGroupAgentSelectable(agent, mode);
                const disabled =
                  !selectable ||
                  (mode === 'member'
                    ? selectionId === selectedLeaderId || agent.id === selectedLeaderId
                    : selectedMemberIds.includes(selectionId) || selectedMemberIds.includes(agent.id));
                const installed = agent.installed === true;
                const categoryTags = agent.tags.length > 0 ? agent.tags.map((tag) => tag.label) : undefined;
                const description = agent.description || t('agentManagement.unknownDescription');
                const cardDisabled = !compatibilityLoading && disabled;
                return (
                  <PageCard
                    key={agent.id}
                    className={`agent-management-selection-card agent-management-selection-card--expert${selected ? ' is-selected' : ''}${compatibilityLoading ? ' is-loading' : cardDisabled ? ' is-disabled' : ''}`}
                    testId="agent-group-member-picker-item"
                    variant={selectionId}
                    interactive={installed && !compatibilityLoading}
                    selected={selected}
                    disabled={cardDisabled && installed}
                    ariaLabel={agent.displayName}
                    onClick={installed && !compatibilityLoading && !disabled ? () => toggle(selectionId, agent.id) : undefined}
                    avatar={{
                      name: agent.displayName,
                      iconUrl: getAgentAvatarUrl(agent),
                      testId: 'agent-group-member-picker-avatar',
                    }}
                    title={agent.displayName}
                    label={categoryTags}
                    description={description}
                    actionSlot={
                      compatibilityLoading ? (
                        <span
                          className="agent-management-selection-card__loading"
                          role="status"
                          aria-label={t('agentManagement.group.picker.loading')}
                        >
                          <LoaderCircle
                            className="agent-management-selection-card__loading-icon"
                            size={16}
                            aria-hidden="true"
                          />
                        </span>
                      ) : !agent.installed && onInstallAgent ? (
                        <button
                          type="button"
                          className="agent-management-inline-action agent-management-selection-card__install"
                          data-testid="agent-group-member-picker-install"
                          data-variant={agent.id}
                          disabled={installingAgentId === agent.id}
                          aria-busy={installingAgentId === agent.id}
                          onClick={(event) => {
                            event.stopPropagation();
                            void onInstallAgent(agent.id);
                          }}
                        >
                          {installingAgentId === agent.id
                            ? t('agentManagement.group.picker.installing')
                            : t('agentManagement.group.picker.install')}
                        </button>
                      ) : (
                        <span className="agent-management-selection-card__action" aria-hidden="true">
                          {selected ? <Check size={12} strokeWidth={2.5} /> : null}
                        </span>
                      )
                    }
                  />
                );
              })}
            </div>
          )}
          <SelectionPagination
            page={page}
            totalPages={totalPages}
            totalItems={filteredAgents.length}
            onPageChange={setPage}
            testId="agent-group-member-picker-pagination"
            previousTestId="agent-group-member-picker-page-previous"
            nextTestId="agent-group-member-picker-page-next"
          />
        </div>
        <footer>
          <span>{t('agentManagement.group.picker.selectedCount', { count: selectedCount })}</span>
          <div>
            <button
              type="button"
              data-testid="agent-group-member-picker-cancel"
              className="agent-management-button agent-management-button--secondary"
              onClick={onCancel}
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              data-testid="agent-group-member-picker-confirm"
              className="agent-management-button agent-management-button--primary"
              disabled={mode === 'leader' && selection.length === 0}
              onClick={() =>
                onConfirm(
                  Array.from(
                    new Set(
                      selection.map((id) => {
                        const agent = agents.find(
                          (item) => resolveAgentGroupSelectionId(item) === id || item.id === id,
                        );
                        return agent ? resolveAgentGroupSelectionId(agent) : id;
                      }),
                    ),
                  ),
                )
              }
            >
              {t('common.confirm')}
            </button>
          </div>
        </footer>
      </section>
    </div>,
    document.body,
  );
}

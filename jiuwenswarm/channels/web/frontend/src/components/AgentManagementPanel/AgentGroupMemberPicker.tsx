import { useMemo, useState } from 'react';
import { Check, LoaderCircle, Plus } from 'lucide-react';
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
import { FormDrawer, PageCard, Tabs } from '../ui';

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
  installingAgentIds?: ReadonlySet<string>;
  restoreFocusRef?: { current: HTMLElement | null };
};

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
  installingAgentIds,
}: AgentGroupMemberPickerProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [sourceTab, setSourceTab] = useState<'local' | 'market'>('market');
  const [selection, setSelection] = useState<string[]>(
    mode === 'leader' ? (selectedLeaderId ? [selectedLeaderId] : []) : selectedMemberIds,
  );
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

  const normalizeSelection = () =>
    Array.from(
      new Set(
        selection.map((id) => {
          const agent = agents.find((item) => resolveAgentGroupSelectionId(item) === id || item.id === id);
          return agent ? resolveAgentGroupSelectionId(agent) : id;
        }),
      ),
    );

  return (
    <FormDrawer
      title={title}
      onClose={onCancel}
      onConfirm={() => onConfirm(normalizeSelection())}
      confirmDisabled={mode === 'leader' && selection.length === 0}
      testId="agent-group-member-picker"
      width={900}
    >
      <div className="relative mb-4 shrink-0">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('agentManagement.form.selectionSearchPlaceholder')}
          className="h-8 w-full rounded-lg border border-border bg-bg pl-8 pr-3 text-[12px] leading-[18px] text-text outline-none focus:border-border-hover"
          data-testid="agent-group-member-picker-search"
        />
      </div>
      <Tabs
        className="mb-4"
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
        }}
        wrapperTestId="agent-group-member-picker-tabs"
        role="tablist"
        ariaLabel={t('agentManagement.group.picker.sourceTabsLabel')}
      />
      {selectionError ? (
        <div
          className="agent-management-form-error mb-4"
          role="alert"
          data-testid="agent-group-member-picker-selection-error"
        >
          {selectionError}
        </div>
      ) : null}
      <div className="flex-1 overflow-y-auto">
        {agentsStatus === 'error' ? (
          <div className="agent-management-form-error" role="alert" data-testid="agent-group-member-picker-error">
            <span>{agentsError || t('agentManagement.states.loadError')}</span>
            <button type="button" onClick={onReloadAgents}>
              {t('common.retry')}
            </button>
          </div>
        ) : agentsStatus === 'loading' && agents.length === 0 ? (
          <div className="py-10 text-center text-[13px] text-text-muted">
            {t('agentManagement.group.picker.loading')}
          </div>
        ) : filteredAgents.length === 0 ? (
          <div className="py-10 text-center text-[13px] text-text-muted">
            <p>{t('agentManagement.group.picker.empty')}</p>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4" data-testid="agent-group-member-picker-list">
            {filteredAgents.map((agent) => {
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
                  className={`${selected ? ' is-selected' : ''}${compatibilityLoading ? ' is-loading' : cardDisabled ? ' is-disabled' : ''}`}
                  testId="agent-group-member-picker-item"
                  variant={selectionId}
                  interactive={installed && !compatibilityLoading}
                  selected={selected}
                  disabled={cardDisabled && installed}
                  onClick={
                    installed && !compatibilityLoading && !disabled ? () => toggle(selectionId, agent.id) : undefined
                  }
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
                      <span className="animate-spin" aria-label={t('agentManagement.group.picker.loading')}>
                        <LoaderCircle size={16} />
                      </span>
                    ) : !agent.installed && onInstallAgent ? (
                      <button
                        type="button"
                        className="agent-management-inline-action"
                        data-testid="agent-group-member-picker-install"
                        data-variant={agent.id}
                        disabled={installingAgentIds?.has(agent.id)}
                        aria-busy={installingAgentIds?.has(agent.id)}
                        onClick={(event) => {
                          event.stopPropagation();
                          void onInstallAgent(agent.id);
                        }}
                      >
                        {installingAgentIds?.has(agent.id)
                          ? t('agentManagement.group.picker.installing')
                          : t('agentManagement.group.picker.install')}
                      </button>
                    ) : (
                      <span className="shrink-0" aria-hidden="true">
                        {selected ? (
                          <Check size={14} className="text-[color:var(--color-chat-accent)]" />
                        ) : (
                          <Plus size={14} className="text-text-muted" />
                        )}
                      </span>
                    )
                  }
                />
              );
            })}
          </div>
        )}
      </div>
      <div className="mt-4 flex items-center justify-between text-[13px] text-text-muted">
        <span>{t('agentManagement.group.picker.selectedCount', { count: selection.length })}</span>
      </div>
    </FormDrawer>
  );
}

import { useMemo, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import EntityAddIcon from '../../assets/agent-management/add.svg?react';
import EntityRemoveIcon from '../../assets/agent-management/remove.svg?react';
import {
  getAgentAvatarUrl,
  isAgentGroupAgentCompatibilityLoading,
  isAgentGroupAgentSelectable,
  resolveAgentGroupSelectionId,
  sortAgentGroupOptions,
  type AgentCatalogItem,
  type RequestStatus,
} from '../../features/agentManagement';
import { PickerDrawer, PickerListRegion, PageCard, SelectedCount } from '../ui';
import type { PageCardDefaultButton } from '../ui';

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
  onInstallAgent?: (id: string) => void | Promise<void>;
  installingAgentIds?: ReadonlySet<string>;
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
      // 单选但可反选：再点已选中的即取消，确认按钮会因 selection 为空而置灰
      setSelection((current) =>
        current.includes(id) || (legacyId ? current.includes(legacyId) : false) ? [] : [id],
      );
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
    <PickerDrawer
      title={title}
      onClose={onCancel}
      onConfirm={() => onConfirm(normalizeSelection())}
      confirmDisabled={mode === 'leader' && selection.length === 0}
      testId="agent-group-member-picker"
      footerLeading={<SelectedCount count={selection.length} testId="agent-group-member-picker-selected-count" />}
      search={query}
      onSearchChange={setQuery}
      searchPlaceholder={t('agentManagement.form.selectionSearchPlaceholder')}
      tabs={{
        value: sourceTab,
        onChange: setSourceTab,
        ariaLabel: t('agentManagement.group.picker.sourceTabsLabel'),
        items: [
          { value: 'market', label: t('agentManagement.tabs.catalog') },
          { value: 'local', label: t('agentManagement.tabs.mine') },
        ],
      }}
    >
      <PickerListRegion
        status={
          agentsStatus === 'error' ? 'error' : agentsStatus === 'loading' && agents.length === 0 ? 'loading' : 'success'
        }
        items={filteredAgents}
        getItemKey={(agent) => resolveAgentGroupSelectionId(agent)}
        testId="agent-group-member-picker"
        loadingMessage={t('agentManagement.group.picker.loading')}
        errorMessage={agentsError || t('agentManagement.states.loadError')}
        emptyMessage={t('agentManagement.group.picker.empty')}
        retryLabel={t('common.retry')}
        onRetry={onReloadAgents}
        loadMoreLabel={t('agentManagement.loadMore')}
        renderItem={(agent) => {
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
          const installing = installingAgentIds?.has(agent.id) === true;
          const categoryTags = agent.tags.length > 0 ? agent.tags.map((tag) => tag.label) : undefined;
          const description = agent.description || t('agentManagement.unknownDescription');
          const cardDisabled = !compatibilityLoading && disabled;
          const defaultButton: PageCardDefaultButton | undefined =
            !compatibilityLoading && !installed && onInstallAgent
              ? {
                  text: installing
                    ? t('agentManagement.group.picker.installing')
                    : t('agentManagement.group.picker.install'),
                  testId: 'agent-group-member-picker-install',
                  variant: agent.id,
                  disabled: installing,
                  busy: installing,
                  onClick: () => void onInstallAgent(agent.id),
                }
              : undefined;
          return (
            <PageCard
              className={`agent-management-selection-card${selected ? ' is-selected' : ''}${compatibilityLoading ? ' is-loading' : cardDisabled ? ' is-disabled' : ''}`}
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
              defaultButton={defaultButton}
              actionSlot={
                compatibilityLoading ? (
                  <span className="animate-spin" aria-label={t('agentManagement.group.picker.loading')}>
                    <LoaderCircle size={16} />
                  </span>
                ) : installed || !onInstallAgent ? (
                  <span className="shrink-0" aria-hidden="true">
                    {selected ? (
                      <EntityRemoveIcon className="text-[color:var(--color-chat-accent)]" />
                    ) : (
                      <EntityAddIcon className="text-text-muted" />
                    )}
                  </span>
                ) : null
              }
            />
          );
        }}
      />
    </PickerDrawer>
  );
}
